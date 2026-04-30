"""
File    : backend/services/agents/fire/rag_engine.py
Description : 소방 도메인 전용 멀티-쿼리 RAG 엔진.
              건축의 ArchRagEngine 패턴을 따르되 NFSC 카테고리에 맞는 서브쿼리를 사용한다.

검색 파이프라인:
    1. 메인 쿼리 + FIRE_SUB_QUERIES 병렬 하이브리드 검색
    2. 결과 취합 + 중복 제거 (chunk id 기준)
    3. Reranker 1회 적용 후 final_limit개 반환
"""

import asyncio
import logging
from typing import Any

from sqlalchemy.orm import Session

from backend.services import vector_service
from backend.models.schema import DocumentChunk, TempDocumentChunk

log = logging.getLogger(__name__)

_CANDIDATE_PER_QUERY = 8
_MAX_RERANK_POOL = 100
_FIRE_DB_DOMAINS = ["fire", "소방"]

FIRE_SUB_QUERIES: list[str] = [
    # [0] 스프링클러 헤드 간격
    "스프링클러 헤드 간격 설치 반경 살수 반경 기준 NFSC",
    # [1] 스프링클러 헤드 수량·배치
    "스프링클러 헤드 설치 수량 배치 기준 바닥면적",
    # [2] 감지기 설치 높이
    "화재 감지기 설치 높이 부착 높이 기준 NFSC",
    # [3] 감지기 간격·수량
    "감지기 설치 간격 수량 감지 면적 기준",
    # [4] 소화전 이격·방수
    "옥내소화전 이격 거리 방수량 방수압력 기준 NFSC",
    # [5] 소방 펌프 압력
    "소방 펌프 토출 압력 기준 NFSC 소화설비",
    # [6] 배관 재질·구경
    "소방 배관 재질 구경 기준 스프링클러 배관",
    # [7] 제어반·경보
    "수신기 제어반 설치 위치 기준 소방 경보 설비",
]


class FireRagEngine:
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
    ) -> list[dict]:
        queries = self._build_queries(main_query, extra_queries)
        log.info("[FireRagEngine] queries=%d개, final_limit=%d", len(queries), final_limit)

        tasks = [
            self._search_one(q, document_id, spec_guid, org_id, candidate_limit)
            for q in queries
        ]
        results_per_query: list[list] = await asyncio.gather(*tasks)

        pool = self._merge(results_per_query)
        log.info(
            "[FireRagEngine] 취합 후 후보 %d개 (dedup 전 합계: %d개)",
            len(pool),
            sum(len(r) for r in results_per_query),
        )

        if len(pool) > _MAX_RERANK_POOL:
            pool = pool[:_MAX_RERANK_POOL]

        reranked = await vector_service.rerank_chunks(main_query, pool, limit=final_limit)
        log.info("[FireRagEngine] Reranker 완료, 최종 %d개 반환", len(reranked))
        return [self._to_dict(chunk) for chunk in reranked]

    def _build_queries(self, main_query: str, extra_queries: list[str] | None) -> list[str]:
        all_queries = [main_query] + FIRE_SUB_QUERIES + (extra_queries or [])
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
        tasks_inner = [
            vector_service.hybrid_search_permanent_chunks(
                self.db, query,
                document_id=document_id,
                domain=domain,
                limit=limit,
            )
            for domain in _FIRE_DB_DOMAINS
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
                log.warning("[FireRagEngine] 서브쿼리 검색 오류(건너뜀): %s", r)
            else:
                chunks.extend(r)
        return chunks

    @staticmethod
    def _merge(results_per_query: list[list]) -> list:
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
        is_temp = isinstance(chunk, TempDocumentChunk)
        return {
            "source":      "temp" if is_temp else "permanent",
            "document_id": chunk.temp_document_id if is_temp else chunk.document_id,
            "chunk_index": chunk.chunk_index,
            "content":     chunk.content,
            "domain":      chunk.domain,
            "category":    chunk.category,
            "doc_name":    chunk.doc_name,
            "section_id":  chunk.section_id,
        }
