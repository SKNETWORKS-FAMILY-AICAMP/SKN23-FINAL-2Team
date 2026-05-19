"""
File    : backend/services/agents/architecture/rag_engine.py
Author  : 김다빈
WBS     : AI-05 (건축 도메인 에이전트)
Create  : 2026-04-15
Modified: 2026-05-11 (table_markdown 우선 context 적용)

Description :
    건축 도메인 전용 멀티-쿼리 RAG 엔진.
"""

import asyncio
import logging
from typing import Any

from sqlalchemy.orm import Session

from backend.services import vector_service
from backend.models.schema import DocumentChunk, TempDocumentChunk

log = logging.getLogger(__name__)

_CANDIDATE_PER_QUERY = 10
_MAX_RERANK_POOL = 120

_ARCH_DB_DOMAINS = ["건축", "건축 시방서", "arch", "architecture"]

ARCH_SUB_QUERIES: list[str] = [
    "방화구획 면적 3000제곱미터 이하 설치 기준 건축법 시행령",
    "방화문 갑종 을종 차열 차연 성능 기준",
    "내화구조 방화벽 방화구획 관통부 처리 기준",
    "직통계단 피난계단 특별피난계단 설치 기준 폭 단높이 단너비",
    "복도 통로 최소 폭 피난 경로 기준",
    "피난 보행거리 출구 간격 수평 이동 거리 기준",
    "거실 채광창 환기창 면적 바닥면적 비율 건축법",
    "최소 실 면적 거실 침실 주거 기준",
    "층고 반자높이 최솟값 거실 복도 계단실",
    "장애인 편의시설 경사로 접근로 위생시설 설치 기준",
]

_FOCUS_GROUPS: list[tuple[frozenset[str], tuple[int, ...]]] = [
    (frozenset({"방화", "fire", "화재", "방화구획", "fire_zone", "fp"}), (0, 1, 2)),
    (frozenset({"계단", "stair", "str", "특별피난", "피난계단"}), (3, 5)),
    (frozenset({"복도", "corridor", "hall", "corr", "통로", "피난경로"}), (4, 5)),
    (frozenset({"채광", "환기", "면적", "거실", "창문", "window", "opening"}), (6, 7, 8)),
    (frozenset({"층고", "반자", "ceiling", "높이"}), (8,)),
    (frozenset({"접근", "장애", "편의", "경사로", "wheelchair", "barrier"}), (9,)),
]


def _filter_sub_queries(focus_area: str) -> list[str]:
    if not focus_area:
        return ARCH_SUB_QUERIES

    fa_lower = focus_area.lower()
    selected_idx: set[int] = set()

    for keywords, indices in _FOCUS_GROUPS:
        if any(kw in fa_lower for kw in keywords):
            selected_idx.update(indices)

    if not selected_idx:
        return ARCH_SUB_QUERIES

    return [ARCH_SUB_QUERIES[i] for i in sorted(selected_idx)]


class ArchRagEngine:
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
        domain: str = "arch",
        permanent_category: str | None = None,
        target_doc: str | None = None,
        strict_target_doc: bool = False,
    ) -> list[dict]:
        queries = self._build_queries(main_query, extra_queries, focus_area)
        search_domains = self._resolve_domains(domain)
        doc_name_filter = target_doc if strict_target_doc and target_doc else None

        log.info(
            "[ArchRagEngine] queries=%d개 domains=%s focus_area=%r final_limit=%d target_doc=%r strict=%s",
            len(queries),
            search_domains,
            focus_area or "전체",
            final_limit,
            target_doc,
            strict_target_doc,
        )

        tasks = [
            self._search_one(
                q,
                document_id,
                spec_guid,
                org_id,
                candidate_limit,
                search_domains,
                permanent_category,
                doc_name_filter,
            )
            for q in queries
        ]

        results_per_query: list[list] = await asyncio.gather(*tasks)

        if strict_target_doc and target_doc and not any(results_per_query):
            log.warning(
                "[ArchRagEngine] strict target_doc 검색 결과 없음 -> doc_name 필터 완화 재검색 target_doc=%r",
                target_doc,
            )
            relaxed_tasks = [
                self._search_one(
                    q,
                    document_id,
                    spec_guid,
                    org_id,
                    candidate_limit,
                    search_domains,
                    permanent_category,
                    None,
                )
                for q in queries
            ]
            results_per_query = await asyncio.gather(*relaxed_tasks)

        pool = self._merge(results_per_query)

        log.info(
            "[ArchRagEngine] 취합 후 후보 %d개 dedup 전 합계 %d개",
            len(pool),
            sum(len(r) for r in results_per_query),
        )

        if len(pool) > _MAX_RERANK_POOL:
            pool = pool[:_MAX_RERANK_POOL]

        reranked = await vector_service.rerank_chunks(
            main_query,
            pool,
            limit=final_limit,
        )

        log.info("[ArchRagEngine] Reranker 완료 최종 %d개 반환", len(reranked))

        return [self._to_dict(chunk) for chunk in reranked]

    def _build_queries(
        self,
        main_query: str,
        extra_queries: list[str] | None,
        focus_area: str = "",
    ) -> list[str]:
        sub_queries = _filter_sub_queries(focus_area)
        all_queries = [main_query] + sub_queries + (extra_queries or [])

        seen: set[str] = set()
        unique: list[str] = []

        for q in all_queries:
            if q not in seen:
                seen.add(q)
                unique.append(q)

        return unique

    @staticmethod
    def _resolve_domains(domain: str | None) -> list[str]:
        d = (domain or "arch").strip()
        if d in ("arch", "architecture", "건축", "건축 시방서"):
            return list(_ARCH_DB_DOMAINS)
        return [d, *[x for x in _ARCH_DB_DOMAINS if x != d]]

    async def _search_one(
        self,
        query: str,
        document_id: str | None,
        spec_guid: str | None,
        org_id: str | None,
        limit: int,
        search_domains: list[str],
        permanent_category: str | None,
        doc_name_filter: str | None,
    ) -> list:
        tasks_inner = [
            vector_service.hybrid_search_permanent_chunks(
                self.db,
                query,
                document_id=document_id,
                domain=domain,
                category=permanent_category,
                doc_name=doc_name_filter,
                limit=limit,
            )
            for domain in search_domains
        ]

        if spec_guid and org_id:
            tasks_inner.append(
                vector_service.hybrid_search_temp_chunks(
                    self.db,
                    query,
                    spec_guid=spec_guid,
                    org_id=org_id,
                    limit=limit,
                )
            )

        results = await asyncio.gather(*tasks_inner, return_exceptions=True)

        chunks: list = []

        for r in results:
            if isinstance(r, Exception):
                log.warning("[ArchRagEngine] 서브쿼리 검색 오류 건너뜀: %s", r)
            else:
                chunks.extend(r)

        return chunks

    @staticmethod
    def _merge(results_per_query: list[list]) -> list:
        seen: set[tuple[str, str, str]] = set()
        merged: list = []

        for chunks in results_per_query:
            for chunk in chunks:
                doc_id = str(
                    getattr(chunk, "document_id", None)
                    or getattr(chunk, "temp_document_id", None)
                    or ""
                )
                chunk_idx = str(getattr(chunk, "chunk_index", "") or "")
                pk = str(getattr(chunk, "id", "") or "")
                key = (chunk.__class__.__name__, doc_id or pk, chunk_idx or pk)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(chunk)

        return merged

    @staticmethod
    def _get_context_content(chunk) -> str:
        context_content = getattr(chunk, "context_content", None)
        if context_content:
            return context_content

        chunk_type = getattr(chunk, "chunk_type", None)
        table_markdown = getattr(chunk, "table_markdown", None)
        content = getattr(chunk, "content", None)

        if chunk_type == "table" and table_markdown:
            return table_markdown

        return content or ""

    @staticmethod
    def _to_dict(chunk) -> dict[str, Any]:
        is_temp = isinstance(chunk, TempDocumentChunk)

        content = ArchRagEngine._get_context_content(chunk)
        raw_content = getattr(chunk, "content", "") or ""

        return {
            "source": "temp" if is_temp else "permanent",
            "id": getattr(chunk, "id", None),
            "document_id": chunk.temp_document_id if is_temp else chunk.document_id,
            "chunk_index": getattr(chunk, "chunk_index", None),

            # LLM 전달용 content
            "content": content,

            # 원본 flatten content 보존
            "raw_content": raw_content,

            # table 표시용 보조 필드
            "table_markdown": getattr(chunk, "table_markdown", None),
            "chunk_type": getattr(chunk, "chunk_type", None),

            "domain": getattr(chunk, "domain", None),
            "category": getattr(chunk, "category", None),
            "doc_name": getattr(chunk, "doc_name", None),
            "section_id": getattr(chunk, "section_id", None),
            "page_number": getattr(chunk, "page_number", None),
        }
