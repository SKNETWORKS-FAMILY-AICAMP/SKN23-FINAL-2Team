"""
File    : backend/api/routers/session_api.py
Author  : 양창일
Create  : 2026-04-13
Description : chat session 생성 및 조회 API 라우터

Modification History :
    - 2026-04-13 (양창일) : 초기 구조 생성
    - 2026-04-14 (양창일) : device 미존재 시 400 응답으로 처리하도록 수정
    - 2026-04-19 (김지우) : 비동기 연결(AsyncSession) 대응에 따른 await 추가
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from typing import List

from backend.api.schemas.session import (
    SessionCreateRequest, SessionResponse,
    FrontendSessionCreateRequest, SessionSummaryResponse, ChatMessageResponse,
)
from backend.core.database import get_db
from backend.api.deps.license_auth import get_authenticated_org_id, require_session_org
from backend.services.session_service import (
    create_chat_session, get_chat_session,
    create_session_for_frontend, list_sessions_by_domain,
    get_session_messages, AGENT_TYPE_TO_DOMAIN,
)

router = APIRouter()


# ── 프론트엔드 채팅 UI 전용 엔드포인트 ───────────────────────────────────

@router.get("/sessions", response_model=List[SessionSummaryResponse])
async def list_sessions(
    agent_type: str = Query(default="", description="Korean name or domain code"),
    domain_type: str = Query(default="", description="domain code (pipe/elec/arch/fire)"),
    device_id: str = Query(default="", description="기기 UUID — 있으면 해당 기기 세션만 반환"),
    db: AsyncSession = Depends(get_db),
    auth_org_id: str = Depends(get_authenticated_org_id),
):
    """도메인 + 기기별 세션 목록 조회 (프론트엔드 채팅 UI용)."""
    raw = agent_type or domain_type
    domain = AGENT_TYPE_TO_DOMAIN.get(raw, raw)
    if not domain:
        raise HTTPException(status_code=400, detail="agent_type 또는 domain_type 파라미터가 필요합니다.")
    return await list_sessions_by_domain(db, domain, device_id, auth_org_id)


@router.post("/sessions", response_model=SessionSummaryResponse)
async def create_session(
    body: FrontendSessionCreateRequest,
    db: AsyncSession = Depends(get_db),
    auth_org_id: str = Depends(get_authenticated_org_id),
):
    """세션 생성 (프론트엔드 채팅 UI용). device_id가 있으면 기기에 귀속."""
    try:
        return await create_session_for_frontend(
            db, body.agent_type, body.dwg_filename, body.device_id, auth_org_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/sessions/{session_id}/messages", response_model=List[ChatMessageResponse])
async def get_messages(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    auth_org_id: str = Depends(get_authenticated_org_id),
):
    """세션 메시지 목록 조회."""
    await require_session_org(db, session_id, auth_org_id)
    return await get_session_messages(db, session_id)


# ── 기존 C# 플러그인 호환 엔드포인트 ─────────────────────────────────────

@router.get("/sessions/{session_id}", response_model=SessionResponse)
async def get_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    auth_org_id: str = Depends(get_authenticated_org_id),
):
    try:
        await require_session_org(db, session_id, auth_org_id)
        return await get_chat_session(db, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    auth_org_id: str = Depends(get_authenticated_org_id),
):
    """세션과 관련 데이터(메시지, 검토 결과)를 모두 삭제합니다."""
    await require_session_org(db, session_id, auth_org_id)
    await db.execute(text("DELETE FROM chat_messages   WHERE session_id = :sid"), {"sid": session_id})
    await db.execute(text("DELETE FROM review_results  WHERE session_id = :sid"), {"sid": session_id})
    result = await db.execute(text("DELETE FROM chat_sessions WHERE id = :sid"), {"sid": session_id})
    await db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")
