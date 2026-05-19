"""
File    : backend/services/agents/piping/sub/query.py
Author  : 송주엽
Date    : 2026-04-14
Description : 배관 시방서 하이브리드 RAG
              (Dense + Sparse RRF + Qwen3-Reranker)
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import settings

logger = logging.getLogger(__name__)

# -------------------------------------------------------------------
# 기존 heuristic / regex 유지
# -------------------------------------------------------------------

_TOC_KO = re.compile(
    r"(?:목차|차례|장\s*제목|편\s*제목|CONTENTS)",
    re.IGNORECASE,
)

_EXPLICIT_TOC_RE = re.compile(
    r"(?:목차|차례|CONTENTS|TABLE\s+OF\s+CONTENTS)",
    re.IGNORECASE,
)

_SECTION_REF_RE = re.compile(r"^[§\s\|]*\d+(?:\.\d+){1,3}")

_TOC_CHUNK_TYPES = frozenset({
    "toc",
    "table_of_contents",
    "contents",
    "목차",
    "차례",
})

_TOC_EARLY_CHUNK_INDEX = 5

_UNIDENTIFIED_SECTION_TOKENS = frozenset({
    "섹션 미확인",
    "section unknown",
    "unknown",
    "n/a",
    "-",
    "",
})

_COVER_METADATA_RE = re.compile(
    r"(?:KCS|KDS|KS\s*[A-Z]|개정|제정|발행|시행일|판|version|edition|"
    r"http[s]?://|www\.|@kcsc|kcsc\.re\.kr)",
    re.IGNORECASE,
)

_COVER_CONTENT_MAX_CHARS = 200
_COVER_PENALTY = 200.0

_TABLE_QUERY_RE = re.compile(
    r"(?:표|별표|테이블|일람표|기준표|규격표|치수표|재료표|"
    r"관경|구경|호칭|두께|스케줄|SCH|압력|온도|유량|"
    r"지지간격|행거|서포트|보온|단열|밸브|플랜지|"
    r"table|schedule|chart)",
    re.IGNORECASE,
)

_DOC_LOOKUP_QUERY_RE = re.compile(
    r"(?:문서|시방서|기준|규정|조항|절|항|본문|원문|찾|검색|"
    r"KCS|KDS|KS|section|clause|spec)",
    re.IGNORECASE,
)

_TABLE_CHUNK_TYPES = {
    "table",
    "markdown_table",
    "spec_table",
    "table_row",
    "표",
    "별표",
}


def _nfc(value: object) -> str:
    return unicodedata.normalize("NFC", str(value or ""))


def _is_table_lookup_query(query: str) -> bool:
    return bool(_TABLE_QUERY_RE.search(query or ""))


def _is_doc_lookup_query(query: str) -> bool:
    return bool(_DOC_LOOKUP_QUERY_RE.search(query or ""))


def _clean_lookup_query(query: str) -> str:
    q = (query or "").strip()

    for token in (
        "찾아줘",
        "찾아",
        "검색해줘",
        "검색",
        "알려줘",
        "보여줘",
        "정리해줘",
        "요약해줘",
        "해주세요",
        "해줘",
        "주세요",
    ):
        q = q.replace(token, " ")

    return re.sub(r"\s+", " ", q).strip()


def _lexical_supplement_queries(query: str) -> list[str]:
    queries = [query]

    cleaned = _clean_lookup_query(query)

    if cleaned and cleaned != query:
        queries.append(cleaned)

    if _is_table_lookup_query(query):
        queries.append(f"{cleaned or query} 표 별표 일람표 기준표")

    seen = set()
    out = []

    for q in queries:
        q = q.strip()
        if q and q not in seen:
            seen.add(q)
            out.append(q)

    return out[:3]


def _is_probably_toc_only_chunk(row: object) -> bool:
    chunk_type = str(getattr(row, "chunk_type", None) or "").lower()

    if chunk_type in _TOC_CHUNK_TYPES:
        return True

    category = str(getattr(row, "category", None) or "").lower()

    if category in _TOC_CHUNK_TYPES:
        return True

    text = (getattr(row, "content", None) or "").strip()

    if len(text) < 30:
        return False

    if _EXPLICIT_TOC_RE.search(text):
        return True

    return False


# -------------------------------------------------------------------
# QueryAgent
# -------------------------------------------------------------------

class QueryAgent:
    PIPING_DOMAINS = ("pipe",)

    def __init__(self, db: AsyncSession):
        self.db = db

    # ---------------------------------------------------------------

    @staticmethod
    def _get_context_content(chunk: Any) -> str:
        """
        LLM 전달용 content 선택.

        우선순위:
        1. context_content
        2. table_markdown
        3. content
        """
        context_content = getattr(chunk, "context_content", None)

        if context_content:
            return str(context_content)

        chunk_type = getattr(chunk, "chunk_type", None)
        table_markdown = getattr(chunk, "table_markdown", None)

        if chunk_type == "table" and table_markdown:
            return str(table_markdown)

        return str(getattr(chunk, "content", "") or "")

    # ---------------------------------------------------------------

    @staticmethod
    def _to_result_dict(chunk: Any, *, source: str) -> dict[str, Any]:
        is_temp = source == "temp"

        return {
            "source": source,
            "id": getattr(chunk, "id", None),

            "document_id": (
                getattr(chunk, "temp_document_id", None)
                if is_temp
                else getattr(chunk, "document_id", None)
            ),

            "chunk_index": getattr(chunk, "chunk_index", None),

            # LLM 전달용
            "content": QueryAgent._get_context_content(chunk),

            # 원본
            "raw_content": getattr(chunk, "content", "") or "",

            # table
            "table_markdown": getattr(chunk, "table_markdown", None),
            "chunk_type": getattr(chunk, "chunk_type", None),

            "domain": getattr(chunk, "domain", None),
            "category": getattr(chunk, "category", None),
            "doc_name": getattr(chunk, "doc_name", None),
            "section_id": getattr(chunk, "section_id", None),
            "page_number": getattr(chunk, "page_number", None),
        }

    # ---------------------------------------------------------------

    async def execute(
        self,
        query: str,
        spec_guid: str | None = None,
        org_id: str | None = None,
        domain: str = "pipe",
        limit: int = 5,
        permanent_category: str | None = None,
        target_doc: str | None = None,
        strict_target_doc: bool = False,
        **kwargs,
    ) -> list[dict]:

        from backend.services import vector_service

        results: list[dict] = []

        prefetch = max(
            12,
            min(settings.RAG_QUERY_PREFETCH_CAP, limit * 4),
        )

        rrf_limit = max(20, min(48, prefetch))

        search_domains = (
            self.PIPING_DOMAINS
            if domain in ("pipe", "piping")
            else (domain,)
        )

        # strict mode 일 때만 강제 doc_name 필터
        doc_name_filter = (
            target_doc
            if strict_target_doc and target_doc
            else None
        )

        # -----------------------------------------------------------
        # Permanent
        # -----------------------------------------------------------

        perm_chunks_all = []

        for d in search_domains:
            chunks = await vector_service.hybrid_search_permanent_chunks_with_rerank(
                self.db,
                query,
                document_id=None,
                domain=d,
                category=permanent_category,
                doc_name=doc_name_filter,
                rrf_limit=rrf_limit,
                final_limit=prefetch,
            )

            perm_chunks_all.extend(chunks)

        # strict target 실패 시 완화 재검색
        if strict_target_doc and target_doc and not perm_chunks_all:
            logger.warning(
                "[Pipe QueryAgent] strict target_doc 결과 없음 → 완화 재검색: %s",
                target_doc,
            )

            for d in search_domains:
                chunks = await vector_service.hybrid_search_permanent_chunks_with_rerank(
                    self.db,
                    query,
                    document_id=None,
                    domain=d,
                    category=permanent_category,
                    doc_name=None,
                    rrf_limit=rrf_limit,
                    final_limit=prefetch,
                )

                perm_chunks_all.extend(chunks)

        # lexical supplement
        if _is_table_lookup_query(query) or _is_doc_lookup_query(query):

            for q in _lexical_supplement_queries(query):

                for d in search_domains:
                    extra = await vector_service.search_permanent_chunks_lexical(
                        self.db,
                        q,
                        document_id=None,
                        domain=d,
                        category=permanent_category,
                        limit=prefetch,
                    )

                    perm_chunks_all.extend(extra)

        # dedupe
        seen = set()
        perm_chunks = []

        for chunk in perm_chunks_all:
            key = (
                getattr(chunk, "document_id", None),
                getattr(chunk, "chunk_index", None),
                getattr(chunk, "section_id", None),
            )

            if key in seen:
                continue

            seen.add(key)
            perm_chunks.append(chunk)

        # TOC 제거
        if settings.RAG_FILTER_TOC_HEURISTIC:
            perm_chunks = [
                c for c in perm_chunks
                if not _is_probably_toc_only_chunk(c)
            ]

        perm_chunks = perm_chunks[:limit]

        for chunk in perm_chunks:
            results.append(
                self._to_result_dict(chunk, source="permanent")
            )

        # -----------------------------------------------------------
        # Temp
        # -----------------------------------------------------------

        if spec_guid and org_id:

            temp_chunks = await vector_service.hybrid_search_temp_chunks_with_rerank(
                self.db,
                query,
                spec_guid=spec_guid,
                org_id=org_id,
                rrf_limit=rrf_limit,
                final_limit=prefetch,
            )

            if _is_table_lookup_query(query) or _is_doc_lookup_query(query):

                for q in _lexical_supplement_queries(query):

                    extra = await vector_service.search_temp_chunks_lexical(
                        self.db,
                        q,
                        spec_guid=spec_guid,
                        org_id=org_id,
                        limit=prefetch,
                    )

                    temp_chunks.extend(extra)

            # dedupe
            seen_temp = set()
            temp_unique = []

            for chunk in temp_chunks:
                key = (
                    getattr(chunk, "temp_document_id", None),
                    getattr(chunk, "chunk_index", None),
                    getattr(chunk, "section_id", None),
                )

                if key in seen_temp:
                    continue

                seen_temp.add(key)
                temp_unique.append(chunk)

            temp_chunks = temp_unique

            if settings.RAG_FILTER_TOC_HEURISTIC:
                temp_chunks = [
                    c for c in temp_chunks
                    if not _is_probably_toc_only_chunk(c)
                ]

            temp_chunks = temp_chunks[:limit]

            for chunk in temp_chunks:
                results.append(
                    self._to_result_dict(chunk, source="temp")
                )

        return results