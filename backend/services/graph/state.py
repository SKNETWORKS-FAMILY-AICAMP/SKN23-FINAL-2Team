"""
File    : backend/services/graph/state.py
Author  : 양창일
Create  : 2026-04-13
Description : AutoCAD 도면 법규 검토 AI 에이전트를 위한 LangGraph 상태 정의

Modification History :
    - 2026-04-13 (양창일) : AgentState TypedDict 초기 구조 작성
    - 2026-04-15 (양창일) : summary_text recent_chat 기반 텍스트 메모리 필드 추가
    - 2026-04-20 (김지우) : S3/Redis 테넌트 관리(org_id, device_id) 및 
                            전기 도메인 워크플로우를 위한 중간 상태(Phase) 필드 추가
"""

from typing import Any, Literal, NotRequired, TypedDict
from langchain_core.messages import AnyMessage # 메시지 관리를 위해 추가

# 전기 에이전트 단계(mapped, interpreted, finalize_retry) 추가
CurrentStep = Literal[
    "request_received",
    "mapped",               # [추가] 물리적 매핑 완료
    "drawing_parsed",
    "laws_retrieved",
    "interpreted",          # [추가] 규정 해석 완료
    "cross_checked",
    "finalize_retry",       # [추가] ReviewResult 포맷 오류 재시도
    "review_completed",
    "query_completed",
    "pending_fix_review",
    "action_ready",
    "agent_completed",
    "error",
]

DomainType = Literal["arch", "elec", "fire", "pipe"]

class SessionMeta(TypedDict):
    session_id: str
    domain_type: DomainType
    session_title: str
    # ✨ S3/Redis 관리를 위한 멀티 테넌트 식별자 (타 도메인 호환성을 위해 NotRequired 처리)
    org_id: NotRequired[str]
    device_id: NotRequired[str]

class ChatMessageRef(TypedDict):
    message_id: str
    role: str
    content: str
    active_object_ids: list[str]
    tool_calls: list[str]

class LawReference(TypedDict):
    chunk_id: str
    document_id: str
    legal_reference: str
    snippet: str
    score: float
    source_type: str
    document_chunk_id: NotRequired[int]  # document_chunks.id (RAG DB FK)

class ViolationItem(TypedDict):
    object_id: str
    violation_type: str
    reason: str
    legal_reference: str
    suggestion: str
    current_value: str
    required_value: str

class ReviewResult(TypedDict):
    is_violation: bool
    violations: list[ViolationItem]
    suggestions: list[str]
    referenced_laws: list[str]
    final_message: str

class TurnSummary(TypedDict):
    turn_index: int
    user_intent: str
    reviewed_object_ids: list[str]
    retrieved_law_refs: list[str]
    violations_found: list[str]
    suggested_actions: list[str]
    step_after_turn: CurrentStep

class PendingFix(TypedDict):
    fix_id: str
    equipment_id: str          
    violation_type: str
    action: str
    description: str
    proposed_fix: dict
    handle: NotRequired[str]
    reference_chunk_id: NotRequired[int]  # review_results.reference_chunk_id → document_chunks.id 

class AgentState(TypedDict):
    session_meta: SessionMeta
    user_request: str
    drawing_data: dict
    retrieved_laws: list[LawReference]
    review_result: ReviewResult
    current_step: CurrentStep
    summary_text: str
    recent_chat: str
    combined_memory: str
    assistant_response: str
    recent_chat_history: list[ChatMessageRef]
    turn_summaries: list[TurnSummary]
    active_object_ids: list[str]
    recent_message_ids: list[str]
    
    # --- S3/멀티테넌트 워크플로우 필수 필드 ---
    session_id: NotRequired[str]
    org_id: NotRequired[str]
    device_id: NotRequired[str]
    current_phase: NotRequired[str]
    user_query: NotRequired[str]
    raw_drawing_data_path: NotRequired[str]
    retrieved_specs: NotRequired[list[dict]]
    chat_history: NotRequired[list[dict]]
    
    # --- LangGraph 및 S3 워크플로우를 위한 메타 필드 ---
    messages: NotRequired[list[AnyMessage]]
    raw_drawing_data: NotRequired[dict]
    interpreted_rules: NotRequired[list[dict]]
    retry_count: NotRequired[int]
    
    # -----------------------------------------------------------
    
    session_extra: NotRequired[dict]
    runtime_meta: NotRequired[dict]
    pending_fixes: NotRequired[list[PendingFix]]
    # 배관: RAG/직접응답 구분, 출처 청크 메타(프론트·로그용)
    response_meta: NotRequired[dict[str, Any]]
    # /api/v1/agent/start 등: LLM 의도분류를 건너뛰고 review로 고정 (도면검토 버튼)
    intent_hint: NotRequired[str]