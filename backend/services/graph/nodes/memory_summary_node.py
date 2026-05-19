"""
File    : backend/services/graph/nodes/memory_summary_node.py
Author  : 양창일
Create  : 2026-04-15
Description : summary_text 와 recent_chat 을 관리하는 LangGraph 메모리 요약 노드

Modification History :
    - 2026-04-15 (양창일) : TEXT 기반 메모리 관리 노드 초기 작성
"""

from __future__ import annotations

import logging

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from backend.core.config import settings
from backend.services.graph.state import AgentState

MAX_RECENT_TURNS = 5
TURN_SEPARATOR = "\n\n---\n\n"


class TurnSummaryResult(BaseModel):
    summary: str = Field(..., description="다음 대화에 필요한 누적 대화 메모리")


def format_turn_text(user_text: str, ai_text: str) -> str:
    return f"User: {user_text}\nAI: {ai_text}"


def split_turns(recent_chat: str) -> list[str]:
    if not recent_chat.strip():
        return []
    return [chunk.strip() for chunk in recent_chat.split(TURN_SEPARATOR) if chunk.strip()]


def join_turns(turns: list[str]) -> str:
    return TURN_SEPARATOR.join(turns)


def build_combined_memory(summary_text: str, recent_chat: str) -> str:
    parts: list[str] = []
    if summary_text.strip():
        parts.append(f"[누적 요약]\n{summary_text.strip()}")
    if recent_chat.strip():
        parts.append(f"[최근 대화 원문]\n{recent_chat.strip()}")
    return "\n\n".join(parts)


async def compress_memory(llm: ChatOpenAI, existing_summary: str, oldest_turn_text: str) -> str:
    structured_llm = llm.with_structured_output(TurnSummaryResult)

    memory_rules = (
        "대화 메모리는 사용자의 후속 질문에 재사용될 작업 기억입니다. "
        "짧은 감상문처럼 요약하지 말고, 재사용 가능한 사실을 보존하세요.\n"
        "- 사용자 목표, 결정사항, 선호, 제약, 파일/도면/핸들/엔드포인트/모델명 같은 고유값을 유지합니다.\n"
        "- 해결된 내용과 아직 남은 확인사항을 구분합니다.\n"
        "- 불필요한 인사말, 중복 표현, 긴 설명은 제거합니다.\n"
        "- 700~1200자 안팎의 간결한 한국어 bullet 메모리로 작성합니다."
    )
    
    if existing_summary:
        prompt_content = (
            f"기존 대화 요약본:\n{existing_summary}\n\n"
            f"추가된 대화 내용:\n{oldest_turn_text}\n\n"
            f"{memory_rules}\n\n"
            "기존 요약본의 맥락을 유지하면서 추가된 대화를 통합해 누적 메모리를 갱신하세요."
        )
        sys_content = "당신은 장기 대화 메모리 관리기입니다. 다음 추론에 필요한 정보 보존을 우선합니다."
    else:
        prompt_content = (
            f"다음 1턴 대화를 누적 메모리로 편입할 수 있게 정리하세요.\n\n"
            f"{memory_rules}\n\n"
            f"대화 내용:\n{oldest_turn_text}"
        )
        sys_content = "당신은 장기 대화 메모리 관리기입니다. 짧은 문장 수보다 정보 보존을 우선합니다."

    result: TurnSummaryResult = await structured_llm.ainvoke(
        [
            {
                "role": "system",
                "content": sys_content,
            },
            {
                "role": "user",
                "content": prompt_content,
            },
        ]
    )
    return result.summary.strip()


async def memory_summary_node(state: AgentState) -> AgentState:
    _sid = state.get("session_id") or (state.get("session_meta") or {}).get("session_id")
    logging.info(
        "[MemoryGraph] memory_summary_node ENTER session=%s recent_turn_slots=%s",
        _sid,
        len(split_turns((state.get("recent_chat") or ""))),
    )
    # llm = ChatOpenAI(
    #     model="qwen3.5-27b-qlora",
    #     temperature=0,
    #     base_url=settings.VLLM_SERVER_URL,
    #     api_key=settings.VLLM_API_KEY,
    # )
    llm = ChatOpenAI(  
        model=settings.OPENAI_MODEL_NAME, 
        api_key=settings.OPENAI_API_KEY,
        temperature=0
    )
        

    summary_text = (state.get("summary_text") or "").strip()
    recent_chat = (state.get("recent_chat") or "").strip()
    user_request = (state.get("user_request") or "").strip()
    assistant_response = (state.get("assistant_response") or "").strip()

    if user_request and assistant_response:
        turns = split_turns(recent_chat)
        turns.append(format_turn_text(user_request, assistant_response))

        if len(turns) > MAX_RECENT_TURNS:
            oldest_turn = turns.pop(0)
            summary_text = await compress_memory(llm, summary_text, oldest_turn)

        recent_chat = join_turns(turns)

    state["summary_text"] = summary_text
    state["recent_chat"] = recent_chat
    state["combined_memory"] = build_combined_memory(summary_text, recent_chat)
    logging.info(
        "[MemoryGraph] memory_summary_node EXIT session=%s summary_chars=%s recent_chars=%s",
        _sid,
        len(summary_text or ""),
        len(recent_chat or ""),
    )
    return state
