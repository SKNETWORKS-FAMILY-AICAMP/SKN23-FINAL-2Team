"""
File    : backend/services/agents/electric/sub/query.py
Author  : 김지우
Date    : 2026-04-23
Description : 전기 시방서 하이브리드 RAG 검색 (Dense + Sparse RRF)
              vector_service의 하이브리드 검색을 통해 임시/영구 시방서를 검색합니다.
"""

from sqlalchemy.ext.asyncio import AsyncSession
from backend.services import vector_service

class QueryAgent:
    def __init__(self, db: AsyncSession):
        self.db = db

    # document_chunks.domain 값 (S3 standards/electric/... 와 정합)
    ELECTRIC_DOMAINS = ("electric", "elec", "전기")

    async def execute(
        self,
        query: str,
        spec_guid: str | None = None,
        org_id: str | None = None,
        domain: str = "electric",
        limit: int = 5,
        permanent_category: str | None = None,
    ) -> list[dict]:
        """
        전기 시방서를 하이브리드 RAG(Dense + Sparse RRF)로 검색합니다.
        - 항상 영구 시방서(국가 표준/관리자 업로드)를 먼저 검색
        - spec_guid + org_id 제공 시: 임시 시방서(사용자 업로드)를 추가 검색하여 뒤에 병합
        """
        results: list[dict] = []

        search_domains = self.ELECTRIC_DOMAINS if domain in ("electric", "elec", "전기") else (domain,)
        perm_chunks_all = []
        for d in search_domains:
            chunks = await vector_service.hybrid_search_permanent_chunks_with_rerank(
                self.db,
                query,
                document_id=None,
                domain=d,
                category=permanent_category,
                final_limit=limit,
            )
            perm_chunks_all.extend(chunks)
            
        # 중복 제거
        seen = set()
        perm_chunks = []
        for c in perm_chunks_all:
            key = (c.document_id, c.chunk_index)
            if key not in seen:
                seen.add(key)
                perm_chunks.append(c)
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

        # 임시 시방서 검색
        if spec_guid and org_id:
            temp_chunks = await vector_service.hybrid_search_temp_chunks_with_rerank(
                self.db,
                query,
                spec_guid=spec_guid,
                org_id=org_id,
                final_limit=limit,
            )
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
