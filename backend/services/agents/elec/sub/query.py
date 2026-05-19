"""
File    : backend/services/agents/elec/sub/query.py
Author  : 김지우
Date    : 2026-04-23
Description : 전기 시방서 하이브리드 RAG 검색 (Dense + Sparse RRF)
              vector_service의 하이브리드 검색을 통해 임시/영구 시방서를 검색합니다.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.services import vector_service

logger = logging.getLogger(__name__)


class QueryAgent:
    # document_chunks.domain 값 정합
    ELECTRIC_DOMAINS = ("electric", "elec", "전기")

    def __init__(self, db: AsyncSession):
        self.db = db

    @staticmethod
    def _get_context_content(chunk: Any) -> str:
        """
        LLM 전달용 content 선택.

        우선순위:
        1. vector_service가 붙인 context_content
        2. table chunk + table_markdown
        3. 기존 content
        """
        context_content = getattr(chunk, "context_content", None)
        if context_content:
            return str(context_content)

        chunk_type = getattr(chunk, "chunk_type", None)
        table_markdown = getattr(chunk, "table_markdown", None)

        if chunk_type == "table" and table_markdown:
            return str(table_markdown)

        return str(getattr(chunk, "content", "") or "")

    @staticmethod
    def _normalize_limit(limit: Any, default: int = 5) -> int:
        try:
            value = int(limit)
            return max(1, min(value, 50))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _normalize_domain(domain: str | None) -> tuple[str, ...]:
        d = (domain or "elec").strip()
        if d in ("electric", "elec", "전기"):
            return QueryAgent.ELECTRIC_DOMAINS
        return (d,)

    @staticmethod
    def _make_chunk_key(chunk: Any) -> tuple[str, str, str]:
        return (
            str(getattr(chunk, "document_id", "") or getattr(chunk, "temp_document_id", "") or ""),
            str(getattr(chunk, "chunk_index", "") or ""),
            str(getattr(chunk, "id", "") or ""),
        )

    @staticmethod
    def _to_result_dict(chunk: Any, *, source: str) -> dict[str, Any]:
        is_temp = source == "temp"
        content = QueryAgent._get_context_content(chunk)

        return {
            "source": source,
            "id": getattr(chunk, "id", None),
            "document_id": (
                getattr(chunk, "temp_document_id", None)
                if is_temp
                else getattr(chunk, "document_id", None)
            ),
            "chunk_index": getattr(chunk, "chunk_index", None),

            # LLM 전달용 content
            "content": content,

            # 원본 flatten content 보존
            "raw_content": getattr(chunk, "content", "") or "",

            # table 표시/후처리용
            "table_markdown": getattr(chunk, "table_markdown", None),
            "chunk_type": getattr(chunk, "chunk_type", None),

            "domain": getattr(chunk, "domain", None),
            "category": getattr(chunk, "category", None),
            "doc_name": getattr(chunk, "doc_name", None),
            "section_id": getattr(chunk, "section_id", None),
            "page_number": getattr(chunk, "page_number", None),
        }

    async def execute(
        self,
        query: str,
        spec_guid: str | None = None,
        org_id: str | None = None,
        domain: str = "electric",
        limit: int = 5,
        permanent_category: str | None = None,
        target_doc: str | None = None,
        strict_target_doc: bool = False,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """
        전기 시방서/법규를 하이브리드 RAG(Dense + Sparse RRF)로 검색합니다.

        workflow_handler.py 호환 인자:
        - target_doc
        - strict_target_doc

        방어:
        - 이후 workflow_handler에서 추가 keyword argument를 넘겨도 **kwargs로 흡수합니다.
        """

        query = (query or "").strip()
        limit = self._normalize_limit(limit)

        if not query:
            logger.warning("[QueryAgent] 빈 query 입력")
            return []

        results: list[dict[str, Any]] = []

        search_domains = self._normalize_domain(domain)

        # strict_target_doc=True일 때만 doc_name 필터를 강제 적용.
        # False면 target_doc이 있어도 recall 보존을 위해 전체 검색.
        doc_name_filter = target_doc if strict_target_doc and target_doc else None

        logger.info(
            "[QueryAgent] query=%r domains=%s limit=%s target_doc=%r strict=%s category=%r ignored_kwargs=%s",
            query[:120],
            search_domains,
            limit,
            target_doc,
            strict_target_doc,
            permanent_category,
            sorted(kwargs.keys()) if kwargs else [],
        )

        # 1. 영구 시방서/법규 검색
        perm_chunks_all: list[Any] = []

        for d in search_domains:
            try:
                chunks = await vector_service.hybrid_search_permanent_chunks_with_rerank(
                    self.db,
                    query,
                    document_id=None,
                    domain=d,
                    category=permanent_category,
                    doc_name=doc_name_filter,
                    rrf_limit=max(limit * 4, 20),
                    final_limit=limit,
                )
                perm_chunks_all.extend(chunks or [])
            except Exception as exc:
                logger.warning(
                    "[QueryAgent] permanent search failed domain=%r target_doc=%r err=%s",
                    d,
                    doc_name_filter,
                    exc,
                    exc_info=True,
                )

        # strict_target_doc=True인데 결과가 없으면, 너무 강한 doc_name 필터일 수 있으므로 1회 완화 검색.
        if strict_target_doc and target_doc and not perm_chunks_all:
            logger.warning(
                "[QueryAgent] strict target_doc 검색 결과 없음 → doc_name 필터 완화 재검색 target_doc=%r",
                target_doc,
            )

            for d in search_domains:
                try:
                    chunks = await vector_service.hybrid_search_permanent_chunks_with_rerank(
                        self.db,
                        query,
                        document_id=None,
                        domain=d,
                        category=permanent_category,
                        doc_name=None,
                        rrf_limit=max(limit * 4, 20),
                        final_limit=limit,
                    )
                    perm_chunks_all.extend(chunks or [])
                except Exception as exc:
                    logger.warning(
                        "[QueryAgent] relaxed permanent search failed domain=%r err=%s",
                        d,
                        exc,
                        exc_info=True,
                    )

        # 2. 영구 결과 중복 제거
        seen: set[tuple[str, str, str]] = set()
        perm_chunks: list[Any] = []

        for chunk in perm_chunks_all:
            key = self._make_chunk_key(chunk)
            if key in seen:
                continue
            seen.add(key)
            perm_chunks.append(chunk)

        perm_chunks = perm_chunks[:limit]

        for chunk in perm_chunks:
            results.append(self._to_result_dict(chunk, source="permanent"))

        # 3. 임시 시방서 검색
        if spec_guid and org_id:
            try:
                temp_chunks = await vector_service.hybrid_search_temp_chunks_with_rerank(
                    self.db,
                    query,
                    spec_guid=spec_guid,
                    org_id=org_id,
                    rrf_limit=max(limit * 4, 20),
                    final_limit=limit,
                )

                for chunk in temp_chunks or []:
                    results.append(self._to_result_dict(chunk, source="temp"))

            except Exception as exc:
                logger.warning(
                    "[QueryAgent] temp search failed spec_guid=%r org_id=%r err=%s",
                    spec_guid,
                    org_id,
                    exc,
                    exc_info=True,
                )

        logger.info("[QueryAgent] total results=%d", len(results))

        return results