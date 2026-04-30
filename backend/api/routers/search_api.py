"""
File    : backend/api/routers/search_api.py
Author  : 김지우
Create  : 2026-04-13
Description : 사이드카에서 읽어온 spec_guid를 이용하여 DB에서 유사한 청크를 검색하고,
              sLLM을 통해 사용자 질문에 대한 최종 답변을 생성 및 채팅 기록을 저장하는 API 라우터

Modification History :
    - 2026-04-13 (김지우) : 초기 구조 생성
    - 2026-04-13 (김지우) : 예외 처리 보강 및 llm_service 연동을 통한 RAG 파이프라인 완성
    - 2026-04-13 (김지우) : 일반 DB를 활용한 채팅 세션 저장 로직 추가
    - 2026-04-21 (김지우) : AsyncSession 전환 + await 누락 수정
"""

import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from backend.core.database import get_db

from backend.models.schema import ChatMessage
from backend.services import vector_service, llm_service

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/search/spec")
async def search_specification(
    query: str,
    spec_guid: str,
    org_id: str,
    db: AsyncSession = Depends(get_db),
):
    try:
        query_vector = await vector_service.get_embedding(query)

        chunks = await vector_service.search_temp_chunks(
            db=db,
            query_vector=query_vector,
            spec_guid=spec_guid,
            org_id=org_id,
        )

        if not chunks:
            return {"message": "관련 내용을 찾을 수 없습니다.", "context": ""}

        context = "\n\n".join([c.content for c in chunks])

        return {
            "status": "success",
            "context": context,
            "source_count": len(chunks),
        }

    except Exception as e:
        logger.exception("Search API Error")
        raise HTTPException(status_code=500, detail="검색 처리 중 오류가 발생했습니다.")


@router.post("/chat/spec")
async def chat_with_spec(
    session_id: str,
    query: str,
    spec_guid: str,
    org_id: str,
    db: AsyncSession = Depends(get_db),
):
    try:
        user_msg = ChatMessage(session_id=session_id, role="user", content=query)
        db.add(user_msg)
        await db.commit()

        query_vector = await vector_service.get_embedding(query)

        chunks = await vector_service.search_temp_chunks(
            db=db,
            query_vector=query_vector,
            spec_guid=spec_guid,
            org_id=org_id,
        )

        if not chunks:
            answer = "해당 시방서에서는 관련 내용을 찾을 수 없습니다."
            unique_sources = []
        else:
            context_text = "\n\n".join([c.content for c in chunks])
            answer = await llm_service.generate_answer(query, context_text)
            unique_sources = list({c.doc_name for c in chunks if c.doc_name})

        ai_msg = ChatMessage(session_id=session_id, role="assistant", content=answer)
        db.add(ai_msg)
        await db.commit()

        return {
            "status": "success",
            "answer": answer,
            "sources": unique_sources,
        }

    except Exception as e:
        await db.rollback()
        logger.exception("Chat API Error")
        raise HTTPException(status_code=500, detail="AI 답변 생성 중 오류가 발생했습니다.")
