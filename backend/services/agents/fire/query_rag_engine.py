"""
File    : backend/services/agents/fire/query_rag_engine.py
Description : 소방 일반 법령 Q&A 전용 RAG 엔진.
              FireRagEngine(도면 검토 전용)과 분리된 독립 엔진.
              사용자 질문 기반 동적 쿼리 확장 — LLM 호출 없음.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.services import vector_service
from backend.models.schema import TempDocumentChunk

log = logging.getLogger(__name__)

_FIRE_EQUIPMENT_KEYWORDS: dict[str, list[str]] = {
    "sprinkler":        ["스프링클러", "스프링클러헤드", "스프링클러 헤드", "SPK", "sprinkler"],
    "detector":         ["감지기", "화재감지기", "detector", "DET"],
    "hydrant":          ["소화전", "옥내소화전", "hydrant"],
    "extinguisher":     ["소화기", "extinguisher"],
    "pump":             ["소방펌프", "펌프", "fire pump"],
    "alarm":            ["경보설비", "비상방송", "수신기", "발신기"],
    "fire_door":        ["방화문"],
    "fire_compartment": ["방화구획"],
}
# "헤드" 단독은 제외 — 소방 외 맥락에서 오탐 가능성 있음

_EQUIPMENT_LABELS: dict[str, str] = {
    "sprinkler":        "스프링클러",
    "detector":         "감지기",
    "hydrant":          "소화전",
    "extinguisher":     "소화기",
    "pump":             "소방펌프",
    "alarm":            "경보설비",
    "fire_door":        "방화문",
    "fire_compartment": "방화구획",
}

_STATIC_SUFFIXES: list[str] = [
    "NFSC",
    "소방시설 설치 기준",
    "소방시설법 시행령 시행규칙",
]

_EQUIPMENT_TEMPLATES: list[str] = [
    "{label} 설치 기준 NFSC",
    "{label} 설치 간격 기준",
    "{label} 유지관리 점검 기준",
]


class QueryRagEngine:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def retrieve(
        self,
        main_query: str,
        spec_guid: str | None = None,
        org_id: str | None = None,
        domain: str = "fire",
        candidate_limit: int = 5,
        final_limit: int = 6,
        permanent_category: str | None = None,
    ) -> list[dict]:
        queries        = self._build_queries(main_query)
        search_domains = self._resolve_domains(domain)
        log.info(
            "[QueryRagEngine] queries=%d개, domains=%s, final_limit=%d",
            len(queries), search_domains, final_limit,
        )
        tasks = [
            self._search_one(q, search_domains, spec_guid, org_id, candidate_limit, permanent_category)
            for q in queries
        ]
        results_per_query: list[list] = await asyncio.gather(*tasks)
        pool = self._merge(results_per_query)
        log.info("[QueryRagEngine] 취합 후 후보 %d개", len(pool))
        reranked = await vector_service.rerank_chunks(main_query, pool, limit=final_limit)
        log.info("[QueryRagEngine] Reranker 완료, 최종 %d개 반환", len(reranked))
        return [self._to_dict(chunk) for chunk in reranked]

    def _build_queries(self, user_query: str) -> list[str]:
        queries = [user_query]

        matched = self._match_equipment(user_query)
        for canon in matched[:2]:
            label = _EQUIPMENT_LABELS[canon]
            for tmpl in _EQUIPMENT_TEMPLATES:
                queries.append(tmpl.format(label=label))

        for suffix in _STATIC_SUFFIXES:
            queries.append(f"{user_query} {suffix}")

        seen: set[str] = set()
        deduped: list[str] = []
        for q in queries:
            if q not in seen:
                seen.add(q)
                deduped.append(q)
        return deduped[:6]

    def _match_equipment(self, user_query: str) -> list[str]:
        q_lower = user_query.lower()
        return [
            canon
            for canon, keywords in _FIRE_EQUIPMENT_KEYWORDS.items()
            if any(kw.lower() in q_lower for kw in keywords)
        ]

    def _resolve_domains(self, domain: str) -> list[str]:
        if domain in ("fire", "소방"):
            return ["fire", "소방"]
        return [domain, "fire", "소방"]

    async def _search_one(
        self,
        query: str,
        search_domains: list[str],
        spec_guid: str | None,
        org_id: str | None,
        limit: int,
        permanent_category: str | None,
    ) -> list:
        try:
            tasks_inner = [
                vector_service.hybrid_search_permanent_chunks(
                    self.db, query,
                    document_id=None,
                    domain=domain,
                    category=permanent_category,
                    limit=limit,
                )
                for domain in search_domains
            ]
            if spec_guid and org_id:
                tasks_inner.append(
                    vector_service.hybrid_search_temp_chunks(
                        self.db, query,
                        spec_guid=spec_guid,
                        org_id=org_id or "",
                        limit=limit,
                    )
                )
            results = await asyncio.gather(*tasks_inner, return_exceptions=True)
            chunks: list = []
            for r in results:
                if isinstance(r, Exception):
                    log.warning("[QueryRagEngine] 서브쿼리 검색 오류(건너뜀): %s", r)
                else:
                    chunks.extend(r)
            return chunks
        except Exception as e:
            log.warning("[QueryRagEngine] _search_one 오류(건너뜀): %s", e)
            return []

    @staticmethod
    def _merge(results_per_query: list[list]) -> list:
        seen: set[tuple] = set()
        merged: list = []
        for chunks in results_per_query:
            for chunk in chunks:
                doc_id = (
                    getattr(chunk, "document_id", None)
                    or getattr(chunk, "temp_document_id", None)
                )
                chunk_idx = getattr(chunk, "chunk_index", None)
                if doc_id is None:
                    merged.append(chunk)
                    continue
                key = (
                    (chunk.__class__.__name__, doc_id, chunk_idx)
                    if chunk_idx is not None
                    else (chunk.__class__.__name__, doc_id)
                )
                if key not in seen:
                    seen.add(key)
                    merged.append(chunk)
        return merged

    @staticmethod
    def _to_dict(chunk) -> dict[str, Any]:
        is_temp = isinstance(chunk, TempDocumentChunk)
        return {
            "source":      "temp" if is_temp else "permanent",
            "id":          getattr(chunk, "id", None),
            "document_id": chunk.temp_document_id if is_temp else chunk.document_id,
            "chunk_index": chunk.chunk_index,
            "content":     chunk.content,
            "domain":      chunk.domain,
            "category":    chunk.category,
            "doc_name":    chunk.doc_name,
            "section_id":  chunk.section_id,
        }
