"""
File    : backend/services/agents/piping/sub/query.py
Author  : 송주엽
Date    : 2026-04-14
Description : 배관 시방서 하이브리드 RAG (Dense + Sparse RRF + Qwen3-Reranker)
              vector_service의 with_rerank 검색을 통해 대화·시방 질의에 응답합니다.

Modification History :
    - 2026-04-09 (송주엽) : 초기 작성 (BGEM3FlagModel 직접 사용, raw psycopg2)
    - 2026-04-14 (송주엽) : vector_service 기반으로 리팩터링 (SQLAlchemy Session, RRF Hybrid RAG)
    - 2026-04-24 : 목차(표) 청크 휴리스틱 제외 — 키워드만 많고 본문이 없는 RAG 상위 완화
    - 2026-04-24 : 대화 RAG에 hybrid_search_*_with_rerank 적용 (RRF → Qwen3 리랭크)
"""

import logging
import re

from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import settings
from backend.services import vector_service

logger = logging.getLogger(__name__)

_TOC_KO = re.compile(
    r"(?:일반사항|자재|시공|참고기준|적용범위|용어의\s*정의|배관재료|목차|CONTENTS)"
)


def _is_probably_toc_only_chunk(row: object) -> bool:
    """
    시방서 앞부에 흔한 '목차/페이지' 표(마크다운 표) 청크.
    Dense·Lexical RAG는 용어 밀도가 높아 본문보다 먼저 뜨기 쉬움.
    """
    ct = getattr(row, "chunk_type", None) or getattr(row, "category", None)
    if str(ct or "").lower() in ("toc", "table_of_contents", "contents"):
        return True
    if getattr(row, "category", None) == "dictionary":
        return False
    text = (getattr(row, "content", None) or "").strip()
    if len(text) < 30:
        return False
    if text.count("|") < 10:
        return False
    if "\n" not in text and "|" in text:
        if _TOC_KO.search(text) and re.search(r"\d+\.\d+", text):
            return True
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    tlike = sum(1 for ln in lines if ln.count("|") >= 2)
    if tlike < max(3, int(len(lines) * 0.5)):
        return False
    long_prose = sum(1 for ln in lines if len(ln) > 140 and ln.count("|") < 2)
    if long_prose > 0:
        return False
    if tlike >= 3 and _TOC_KO.search(text):
        return True
    return False


class QueryAgent:
    def __init__(self, db: AsyncSession):
        self.db = db

    # document_chunks.domain 값 (S3 standards/pipe/... 와 정합)
    PIPING_DOMAINS = ("pipe",)

    async def execute(
        self,
        query: str,
        spec_guid: str | None = None,
        org_id: str | None = None,
        domain: str = "pipe",
        limit: int = 5,
        permanent_category: str | None = None,
    ) -> list[dict]:
        """
        배관 시방서를 하이브리드 RAG(Dense + Sparse RRF + Qwen3-Reranker)로 검색합니다.

        - 항상 영구 시방서(국가 표준/관리자 업로드)를 먼저 검색
        - spec_guid + org_id 제공 시: 임시 시방서(사용자 업로드)를 추가 검색하여 뒤에 병합
        - 각 소스 내부는 RRF → 리랭크, 두 소스 간 교차 재정렬은 하지 않음 (영구 → 임시 순서)
        """
        results: list[dict] = []

        prefetch = min(
            settings.RAG_QUERY_PREFETCH_CAP,
            max(limit * 6, 24),
        )
        rrf_limit = min(48, max(prefetch, 20))

        # 1. 영구 시방서 검색 — document_chunks.domain=pipe (및 선택적 category)
        # spec_guid 는 temp_documents 쪽 ID이므로 영구 청크 검색의 document_id 로 쓰지 않음
        # (AsyncSession은 동시 쿼리 불가 — asyncio.gather 사용 불가)
        search_domains = self.PIPING_DOMAINS if domain in ("pipe", "pipe") else (domain,)
        perm_chunks_all: list = []
        for d in search_domains:
            chunks = await vector_service.hybrid_search_permanent_chunks_with_rerank(
                self.db,
                query,
                document_id=None,
                domain=d,
                category=permanent_category,
                rrf_limit=rrf_limit,
                final_limit=prefetch,
            )
            perm_chunks_all.extend(chunks)
        # 중복 제거 (chunk_index + document_id 기준) 후 limit 적용
        seen = set()
        perm_chunks = []
        for c in perm_chunks_all:
            key = (c.document_id, c.chunk_index)
            if key not in seen:
                seen.add(key)
                perm_chunks.append(c)
        if settings.RAG_FILTER_TOC_HEURISTIC:
            before = len(perm_chunks)
            perm_chunks = [c for c in perm_chunks if not _is_probably_toc_only_chunk(c)]
            dropped = before - len(perm_chunks)
            if dropped:
                logger.debug(
                    "[QueryAgent] 목차 추정 청크 제외 %d건 (잔여 후보 %d)",
                    dropped,
                    len(perm_chunks),
                )
        perm_chunks = perm_chunks[:limit]
        for chunk in perm_chunks:
            results.append({
                "source": "permanent",
                "id": chunk.id,
                "document_id": chunk.document_id,
                "chunk_index": chunk.chunk_index,
                "content": chunk.content,
                "domain": chunk.domain,
                "category": chunk.category,
                "doc_name": chunk.doc_name,
                "section_id": chunk.section_id,
            })

        # 2. 임시 시방서 검색 (사용자 업로드 시방서가 있을 경우)
        if spec_guid and org_id:
            temp_chunks = await vector_service.hybrid_search_temp_chunks_with_rerank(
                self.db,
                query,
                spec_guid=spec_guid,
                org_id=org_id,
                rrf_limit=rrf_limit,
                final_limit=prefetch,
            )
            if settings.RAG_FILTER_TOC_HEURISTIC:
                temp_chunks = [c for c in temp_chunks if not _is_probably_toc_only_chunk(c)]
            temp_chunks = temp_chunks[:limit]
            for chunk in temp_chunks:
                results.append({
                    "source": "temp",
                    "document_id": chunk.temp_document_id,
                    "chunk_index": chunk.chunk_index,
                    "content": chunk.content,
                    "domain": chunk.domain,
                    "category": chunk.category,
                    "doc_name": chunk.doc_name,
                    "section_id": chunk.section_id,
                })

        return results
