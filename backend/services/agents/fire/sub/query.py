"""
File    : backend/services/agents/fire/sub/query.py
Author  : 김민정
Create  : 2026-04-15
Description : 소방 법규 및 기준을 위한 하이브리드 RAG 검색 에이전트.

Modification History:
    - 2026-04-15 (김민정) : 하이브리드 RAG 기반 법규 검색 로직 구현
    - 2026-04-18 (김지우) : llm_service 연동으로 리팩터링
    - 2026-04-19 (김민정) : 누락된 임포트 추가 및 하이브리드 RAG 런타임 오류 수정
    - 2026-04-23       : piping 방식과 동일하게 통일 (AsyncSession, vector_service 기반)
"""

import json
import logging

from sqlalchemy.ext.asyncio import AsyncSession
from backend.services import vector_service
from backend.services.llm_service import generate_answer


class QueryAgent:
    def __init__(self, db: AsyncSession):
        self.db = db

    FIRE_DOMAINS = ("fire",)

    async def execute(
        self,
        query: str,
        spec_guid: str | None = None,
        org_id: str | None = None,
        domain: str = "fire",
        limit: int = 5,
        permanent_category: str | None = None,
    ) -> list[dict]:
        """
        소방 법규(NFSC) 시방서를 하이브리드 RAG(Dense + Sparse RRF)로 검색합니다.

        - 항상 영구 시방서(국가 표준/관리자 업로드)를 먼저 검색
        - spec_guid + org_id 제공 시: 임시 시방서(사용자 업로드)를 추가 검색하여 뒤에 병합
        """
        results: list[dict] = []

        # 1. 영구 시방서 검색
        search_domains = self.FIRE_DOMAINS if domain in ("fire",) else (domain,)
        perm_chunks_all = []
        for d in search_domains:
            chunks = await vector_service.hybrid_search_permanent_chunks(
                self.db,
                query,
                document_id=None,
                domain=d,
                category=permanent_category,
                limit=limit,
            )
            perm_chunks_all.extend(chunks)

        # 중복 제거 후 limit 적용
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

        # 2. 임시 시방서 검색 (사용자 업로드 시방서가 있을 경우)
        if spec_guid and org_id:
            temp_chunks = await vector_service.hybrid_search_temp_chunks(
                self.db,
                query,
                spec_guid=spec_guid,
                org_id=org_id,
                limit=limit,
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

    async def resolve_terms(self, terms: list) -> dict:
        """
        도면의 레이어/블록명(terms)을 받아 표준 소방 타입으로 매핑합니다.
        """
        if not terms:
            return {}

        search_query = f"소방 도면 기호 및 용어 정의: {', '.join(terms[:10])}"
        knowledge_data = await self.execute(search_query)
        knowledge_context = "\n".join([r["content"] for r in knowledge_data if r.get("content")])

        system_prompt = """
        당신은 소방 설계 도면 해석 전문가입니다.
        [지식 베이스]를 활용하여 [도면 용어 리스트]를 소방 표준 타입으로 매핑하십시오.

        [표준 타입 목록]
        - SPRINKLER_HEAD, FIRE_HYDRANT, FIRE_HYDRANT_BOX, EXTINGUISHER, DETECTOR, ALARM_CONTROL_PANEL, FIRE_WALL, FIRE_DOOR 등

        [출력 스키마]
        { "mapping": { "용어": "표준_타입" } }
        """

        user_prompt = f"""
        [지식 베이스]: {knowledge_context}
        [도면 용어 리스트]: {json.dumps(terms, ensure_ascii=False)}
        """

        try:
            response_data = await generate_answer(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                response_format={"type": "json_object"}
            )
            return response_data.get("mapping", {})
        except Exception as e:
            logging.warning("[FireQueryAgent] 용어 매핑 오류: %s", e)
            return {}
