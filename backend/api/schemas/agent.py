"""
File    : backend/api/schemas/agent.py
Author  : 양창일
Create  : 2026-04-07
Description : agent 실행 요청 및 응답 데이터 스키마

Modification History :
    - 2026-04-07 (양창일) : 초기 구조 생성
    - 2026-04-08 (양창일) : 고정 도메인 검증 및 응답 스키마 추가
    - 2026-04-13 (양창일) : session_id 및 review_result 응답 필드 추가
    - 2026-04-13 (양창일) : retrieved_laws 컨텍스트 요청 스키마 추가
    - 2026-04-14 (양창일) : pending_fixes 관련 스키마 추가
    - 2026-04-15 (양창일) : TEXT 메모리 구조 기준 주석 및 역할 설명 정리
"""

from typing import Any, Literal

from pydantic import BaseModel, Field

AgentDomain = Literal["arch", "elec", "fire", "pipe"]
AGENT_DOMAINS: tuple[AgentDomain, ...] = ("arch", "elec", "fire", "pipe")


class AgentExecuteRequest(BaseModel):
    session_id: str = Field(..., description="실행할 세션 ID")
    domain: AgentDomain = Field(..., description="실행할 도메인 에이전트")
    payload: dict[str, Any] = Field(
        ...,
        description=(
            "실행 시점에만 사용할 런타임 컨텍스트. "
            "TEXT 메모리 구조에서는 drawing_data 와 retrieved_laws 가 DB에 영속 저장되지 않으므로 "
            "필요하면 매 요청마다 함께 전달해야 합니다."
        ),
    )


class PendingFixResponse(BaseModel):
    fix_id: str
    equipment_id: str
    violation_type: str
    action: str
    description: str


class AgentViolationResponse(BaseModel):
    object_id: str
    violation_type: str
    reason: str
    legal_reference: str
    suggestion: str
    current_value: str
    required_value: str


class AgentReviewResultResponse(BaseModel):
    is_violation: bool
    violations: list[AgentViolationResponse]
    suggestions: list[str]
    referenced_laws: list[str]
    final_message: str


class AgentExecuteData(BaseModel):
    session_id: str
    domain: AgentDomain
    current_step: str
    active_object_ids: list[str]
    referenced_laws: list[str]
    review_result: AgentReviewResultResponse
    pending_fixes: list[PendingFixResponse]
    response_meta: dict[str, Any] = Field(
        default_factory=dict,
        description="RAG vs LLM 직답, 인용 DB·청크 메타(배관 도메인)",
    )
    received_payload: dict[str, Any] = Field(
        ...,
        description="이번 실행에서 실제로 사용한 런타임 payload 원본",
    )


class AgentExecuteResponse(BaseModel):
    status: str
    message: str
    data: AgentExecuteData


class AgentFixesConfirmRequest(BaseModel):
    session_id: str
    selected_fix_ids: list[str]


class AgentFixesConfirmResponse(BaseModel):
    status: str
    session_id: str
    selected_count: int
    pending_fixes: list[PendingFixResponse]


class AgentLawContextRequest(BaseModel):
    session_id: str
    retrieved_laws: list[dict[str, Any]] = Field(
        ...,
        description=(
            "법규 검색 결과. TEXT 메모리 구조에서는 이 값이 세션 DB에 영속 저장되지 않으며, "
            "검증 또는 사전 확인 용도로 사용됩니다."
        ),
    )


class AgentLawContextResponse(BaseModel):
    status: str
    message: str
    session_id: str
    current_step: str
    referenced_laws: list[str]
