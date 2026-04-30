"""
File    : backend/services/agents/architecture/rag_engine.py
Author  : 김다빈
WBS     : AI-05 (건축 도메인 에이전트)
Create  : 2026-04-15
Modified: 2026-04-25 (focus_area 기반 서브쿼리 필터링 — DB 호출 최대 50% 감소)

Description :
    건축 도메인 전용 멀티-쿼리 RAG 엔진.

    검색 파이프라인:
        1. 서브쿼리 구성
           - 메인 쿼리 + 건축 도메인 고정 서브쿼리 (ARCH_SUB_QUERIES, 10개)
           - focus_area 주어지면 _FOCUS_GROUPS 매핑으로 연관 그룹만 선택 (DB 호출 최대 64% 감소)

        2. 병렬 하이브리드 검색 (asyncio.gather)
           - 각 서브쿼리 → Dense + Sparse → RRF 후보 추출
           - 영구 시방서(document_chunks) + 임시 시방서(temp_document_chunks)

        3. 후보 취합 + 중복 제거
           - 청크 id(PK) 기준 dedup
           - 모든 서브쿼리 결과를 하나의 후보 풀로 합침

        4. Reranker 1회 적용
           - 메인 쿼리 기준 Cross-Encoder 재정렬
           - 최종 final_limit개 반환

Modification History :
    - 2026-04-15 (김다빈) : 초기 구현
    - 2026-04-25 (김다빈) : focus_area 기반 서브쿼리 필터링 추가
                            _FOCUS_GROUPS 키워드-인덱스 매핑, _filter_sub_queries() 추가.
                            방화/방화구획 요청 시 10개 → 3개 서브쿼리로 DB 호출 64% 절감.
                            빈 focus_area는 전체 서브쿼리 실행으로 하위 호환 유지.

Usage:
    engine = ArchRagEngine(db)
    results = await engine.retrieve(
        main_query="방화구획 면적 기준",
        final_limit=8,
        focus_area="방화",          # 선택적 — 주어지면 관련 서브쿼리만 실행
    )
    # results: list[dict] — content, source, domain, doc_name, section_id 포함
"""

import asyncio
import logging
from typing import Any

from sqlalchemy.orm import Session

from backend.services import vector_service
from backend.models.schema import DocumentChunk, TempDocumentChunk

log = logging.getLogger(__name__)

# 서브쿼리당 RRF 후보 수 (많을수록 recall↑, DB 부하↑)
_CANDIDATE_PER_QUERY = 10
# Reranker 최대 입력 후보 수
_MAX_RERANK_POOL = 120

# DB에 저장된 건축 도메인 값 (document_chunks.domain 컬럼 기준)
_ARCH_DB_DOMAINS = ["건축", "건축 시방서"]

# ── 건축 도메인 고정 서브쿼리 ────────────────────────────────────────────────
# 단일 쿼리로 놓치기 쉬운 건축법 법규 카테고리를 보완합니다.
ARCH_SUB_QUERIES: list[str] = [
    # [0] 방화·피난
    "방화구획 면적 3000제곱미터 이하 설치 기준 건축법 시행령",
    # [1]
    "방화문 갑종 을종 차열 차연 성능 기준",
    # [2]
    "내화구조 방화벽 방화구획 관통부 처리 기준",

    # [3] 피난 동선
    "직통계단 피난계단 특별피난계단 설치 기준 폭 단높이 단너비",
    # [4]
    "복도 통로 최소 폭 피난 경로 기준",
    # [5]
    "피난 보행거리 출구 간격 수평 이동 거리 기준",

    # [6] 채광·환기·면적
    "거실 채광창 환기창 면적 바닥면적 비율 건축법",
    # [7]
    "최소 실 면적 거실 침실 주거 기준",
    # [8]
    "층고 반자높이 최솟값 거실 복도 계단실",

    # [9] 접근성
    "장애인 편의시설 경사로 접근로 위생시설 설치 기준",
]

# ── focus_area 키워드 → 서브쿼리 인덱스 그룹 ─────────────────────────────────
# 형식 : (키워드 집합, ARCH_SUB_QUERIES 인덱스 튜플)
# 매칭 : focus_area.lower()가 키워드 집합의 임의 원소를 포함하면 해당 인덱스 선택.
# 복수 그룹 매칭 시 인덱스 합집합 사용 → "복도+계단" 같은 복합 검토도 지원.
# focus_area가 빈 문자열이면 _filter_sub_queries()가 전체 10개 반환 (하위 호환).
_FOCUS_GROUPS: list[tuple[frozenset[str], tuple[int, ...]]] = [
    (frozenset({"방화", "fire", "화재", "방화구획", "fire_zone", "fp"}),    (0, 1, 2)),
    (frozenset({"계단", "stair", "str", "특별피난", "피난계단"}),            (3, 5)),
    (frozenset({"복도", "corridor", "hall", "corr", "통로", "피난경로"}),    (4, 5)),
    (frozenset({"채광", "환기", "면적", "거실", "창문", "window", "opening"}),(6, 7, 8)),
    (frozenset({"층고", "반자", "ceiling", "높이"}),                         (8,)),
    (frozenset({"접근", "장애", "편의", "경사로", "wheelchair", "barrier"}), (9,)),
]


def _filter_sub_queries(focus_area: str) -> list[str]:
    """
    focus_area 키워드에 매칭되는 ARCH_SUB_QUERIES 항목만 반환합니다.

    Parameters
    ----------
    focus_area : str
        검토 집중 영역 힌트 (예: "방화구획", "복도", "계단폭").
        빈 문자열이면 전체 10개 서브쿼리 반환 (하위 호환 보장).

    Returns
    -------
    list[str]
        선택된 서브쿼리 목록. 인덱스 오름차순으로 정렬되어 반환.

    Notes
    -----
    - 매칭 없음(알 수 없는 focus_area) → 안전을 위해 전체 반환 (recall 우선).
    - 방화/방화구획 예시: 10개 → 3개, DB 호출 64% 감소.
    - 복도+피난 예시: 인덱스 {4,5} 선택.
    """
    if not focus_area:
        return ARCH_SUB_QUERIES

    fa_lower = focus_area.lower()
    selected_idx: set[int] = set()
    for keywords, indices in _FOCUS_GROUPS:
        if any(kw in fa_lower for kw in keywords):
            selected_idx.update(indices)

    # 매칭 없으면 fallback: 전체 반환 (안전성 우선)
    if not selected_idx:
        return ARCH_SUB_QUERIES

    return [ARCH_SUB_QUERIES[i] for i in sorted(selected_idx)]


class ArchRagEngine:
    """
    건축 도메인 전용 멀티-쿼리 RAG 엔진.

    Parameters
    ----------
    db : SQLAlchemy Session
    """

    def __init__(self, db: Session):
        self.db = db

    async def retrieve(
        self,
        main_query: str,
        document_id: str | None = None,
        spec_guid: str | None = None,
        org_id: str | None = None,
        extra_queries: list[str] | None = None,
        candidate_limit: int = _CANDIDATE_PER_QUERY,
        final_limit: int = 8,
        focus_area: str = "",
    ) -> list[dict]:
        """
        멀티-쿼리 하이브리드 검색 → 취합 → Reranker 파이프라인 실행.

        Parameters
        ----------
        main_query     : 사용자/에이전트 원본 쿼리
        document_id    : 영구 시방서 특정 문서 ID (None = arch 전체)
        spec_guid      : 임시 시방서 문서 ID
        org_id         : 임시 시방서 조직 ID
        extra_queries  : 호출자가 추가하는 동적 서브쿼리
        candidate_limit: 서브쿼리당 RRF 후보 수
        final_limit    : Reranker 최종 반환 수
        focus_area     : 검토 집중 영역 힌트 — 매칭 서브쿼리만 실행 (빈 문자열 = 전체)

        Returns
        -------
        list[dict] — 각 항목: {source, document_id, content, domain,
                               category, doc_name, section_id, chunk_index}
        """
        # ── 1. 서브쿼리 구성 ───────────────────────────────────────────────
        queries = self._build_queries(main_query, extra_queries, focus_area)
        log.info(
            "[ArchRagEngine] queries=%d개 (focus_area=%r), final_limit=%d",
            len(queries), focus_area or "전체", final_limit,
        )

        # ── 2. 병렬 검색 ──────────────────────────────────────────────────
        tasks = [
            self._search_one(q, document_id, spec_guid, org_id, candidate_limit)
            for q in queries
        ]
        results_per_query: list[list] = await asyncio.gather(*tasks)

        # ── 3. 취합 + 중복 제거 ────────────────────────────────────────────
        pool = self._merge(results_per_query)
        log.info(
            "[ArchRagEngine] 취합 후 후보 %d개 (dedup 전 합계: %d개)",
            len(pool),
            sum(len(r) for r in results_per_query),
        )

        if len(pool) > _MAX_RERANK_POOL:
            pool = pool[:_MAX_RERANK_POOL]

        # ── 4. Reranker (메인 쿼리 기준 1회) ─────────────────────────────
        reranked = await vector_service.rerank_chunks(main_query, pool, limit=final_limit)
        log.info("[ArchRagEngine] Reranker 완료, 최종 %d개 반환", len(reranked))

        return [self._to_dict(chunk) for chunk in reranked]

    # ── 내부 메서드 ─────────────────────────────────────────────────────────

    def _build_queries(
        self,
        main_query: str,
        extra_queries: list[str] | None,
        focus_area: str = "",
    ) -> list[str]:
        """메인 쿼리 + focus_area 필터링된 서브쿼리 + 동적 추가 쿼리 합성."""
        sub_queries = _filter_sub_queries(focus_area)
        all_queries = [main_query] + sub_queries + (extra_queries or [])
        seen: set[str] = set()
        unique = []
        for q in all_queries:
            if q not in seen:
                seen.add(q)
                unique.append(q)
        return unique

    async def _search_one(
        self,
        query: str,
        document_id: str | None,
        spec_guid: str | None,
        org_id: str | None,
        limit: int,
    ) -> list:
        """단일 쿼리 → 영구 + 임시 하이브리드 검색 (Reranker 없이 RRF까지).

        영구 시방서는 _ARCH_DB_DOMAINS(건축, 건축 시방서)를 병렬 검색합니다.
        """
        tasks_inner = [
            vector_service.hybrid_search_permanent_chunks(
                self.db, query,
                document_id=document_id,
                domain=domain,
                limit=limit,
            )
            for domain in _ARCH_DB_DOMAINS
        ]
        if spec_guid and org_id:
            tasks_inner.append(
                vector_service.hybrid_search_temp_chunks(
                    self.db, query,
                    spec_guid=spec_guid,
                    org_id=org_id,
                    limit=limit,
                )
            )

        results = await asyncio.gather(*tasks_inner, return_exceptions=True)
        chunks: list = []
        for r in results:
            if isinstance(r, Exception):
                log.warning("[ArchRagEngine] 서브쿼리 검색 오류 (건너뜀): %s", r)
            else:
                chunks.extend(r)
        return chunks

    @staticmethod
    def _merge(results_per_query: list[list]) -> list:
        """
        여러 서브쿼리 결과를 하나의 후보 풀로 합치고 중복을 제거합니다.
        청크 DB id(PK)를 키로 사용하며, 먼저 등장한 순서를 유지합니다.
        (앞쪽 = 메인 쿼리에 가까우므로 우선순위 높음)
        """
        seen: set[int] = set()
        merged: list = []
        for chunks in results_per_query:
            for chunk in chunks:
                pk = getattr(chunk, "id", None)
                if pk is None or pk not in seen:
                    if pk is not None:
                        seen.add(pk)
                    merged.append(chunk)
        return merged

    @staticmethod
    def _to_dict(chunk) -> dict[str, Any]:
        """DB 모델(DocumentChunk | TempDocumentChunk) → 에이전트 소비용 dict 변환."""
        is_temp = isinstance(chunk, TempDocumentChunk)
        return {
            "source":      "temp" if is_temp else "permanent",
            "document_id": (
                chunk.temp_document_id if is_temp else chunk.document_id
            ),
            "chunk_index": chunk.chunk_index,
            "content":     chunk.content,
            "domain":      chunk.domain,
            "category":    chunk.category,
            "doc_name":    chunk.doc_name,
            "section_id":  chunk.section_id,
        }
