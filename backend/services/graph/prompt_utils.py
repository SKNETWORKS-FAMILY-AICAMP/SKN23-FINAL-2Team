"""
File    : backend/services/graph/prompt_utils.py
Author  : 양창일
Create  : 2026-04-15
Description : AgentState 기반 메모리 프롬프트 조립 공통 유틸리티

Modification History :
    - 2026-04-15 (양창일) : state 유무에 따라 메모리 프롬프트를 쉽게 만들 수 있는 공통 함수 추가
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from backend.services.graph.state import AgentState, DomainType
from backend.services.state_service import build_initial_state, load_agent_state


def build_memory_prompt_from_state(state: AgentState) -> str:
    summary_text = (state.get("summary_text") or "").strip()
    recent_chat = (state.get("recent_chat") or "").strip()
    parts: list[str] = []

    if summary_text:
        parts.append(f"[누적 요약]\n{summary_text}")

    if recent_chat:
        parts.append(f"[최근 대화 원문]\n{recent_chat}")

    if not parts:
        return "[대화 메모리]\n아직 저장된 대화가 없습니다."

    return "\n\n".join(parts)


def load_memory_prompt(db: Session, session_id: str) -> str:
    state = load_agent_state(db, session_id)
    return build_memory_prompt_from_state(state)


def build_initial_prompt_state(
    session_id: str,
    domain_type: DomainType,
    session_title: str = "",
    user_request: str = "",
) -> AgentState:
    return build_initial_state(
        session_id=session_id,
        domain_type=domain_type,
        session_title=session_title,
        user_request=user_request,
        summary_text="",
        recent_chat="",
    )


def load_or_build_memory_prompt(
    *,
    db: Session | None = None,
    session_id: str,
    domain_type: DomainType = "arch",
    session_title: str = "",
    user_request: str = "",
) -> tuple[AgentState, str]:
    if db is not None:
        try:
            state = load_agent_state(db, session_id)
        except ValueError:
            state = build_initial_prompt_state(
                session_id=session_id,
                domain_type=domain_type,
                session_title=session_title,
                user_request=user_request,
            )
    else:
        state = build_initial_prompt_state(
            session_id=session_id,
            domain_type=domain_type,
            session_title=session_title,
            user_request=user_request,
        )

    return state, build_memory_prompt_from_state(state)
