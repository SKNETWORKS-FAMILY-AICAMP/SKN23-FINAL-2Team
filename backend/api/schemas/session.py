"""
File    : backend/api/schemas/session.py
Author  : 양창일
Create  : 2026-04-13
Description : chat session 생성 및 조회 요청 응답 스키마

Modification History :
    - 2026-04-13 (양창일) : 초기 구조 생성
    - 2026-04-14 (양창일) : device_id 를 UUID 형식으로 검증하도록 수정
    - 2026-04-15 (양창일) : summary_text recent_chat 응답 필드로 변경
"""

from uuid import UUID
from typing import Any, Optional

from pydantic import BaseModel

from backend.api.schemas.agent import AgentDomain


# 기존 C# 플러그인 호환용 (device_id 기반)
class SessionCreateRequest(BaseModel):
    device_id: UUID
    domain_type: AgentDomain
    session_title: str


class SessionResponse(BaseModel):
    session_id: str
    device_id: str
    domain_type: AgentDomain
    session_title: str
    summary_text: str
    recent_chat: str


# 프론트엔드 채팅 UI용
class FrontendSessionCreateRequest(BaseModel):
    agent_type: str          # "배관" | "전기" | "건축" | "소방" | domain code
    dwg_filename: str = ""
    device_id: str = ""      # localStorage skn23_device_id — 없으면 device 미인증 상태


class SessionSummaryResponse(BaseModel):
    id: str
    agent_type: str          # domain code ("pipe", "elec", ...)
    title: str
    dwg_filename: str
    created_at: str
    message_count: int


class ChatMessageResponse(BaseModel):
    id: str
    session_id: str
    role: str                # "user" | "agent"
    content: str
    created_at: str
    active_object_ids: list[str] = []
    tool_calls: list[Any] = []
    token_count: int = 0
    agent_name: Optional[str] = None
    tool_call_id: Optional[str] = None
    approval_status: str = "completed"
    metadata: Optional[dict[str, Any]] = None
