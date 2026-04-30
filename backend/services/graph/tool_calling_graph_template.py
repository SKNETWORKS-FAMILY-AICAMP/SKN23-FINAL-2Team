"""
File    : backend/services/graph/tool_calling_graph_template.py
Author  : 양창일
Create  : 2026-04-15
Description : LangGraph 표준 Tool Calling 기반 공통 Graph 템플릿

Modification History :
    - 2026-04-15 (양창일) : ToolNode + bind_tools 기반 공통 Graph 뼈대 작성
    - 2026-04-16 (양창일) : [AI-04] 공통 오류 처리 및 재시도 로직 추가
"""

from __future__ import annotations

import json
from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from pydantic import BaseModel, Field, ValidationError

from backend.core.config import settings


# =========================================================
# 1. 최종 출력 스키마
# =========================================================
class ViolationItem(BaseModel):
    object_id: str = Field(default="", description="위반 대상 CAD 객체 ID")
    violation_type: str = Field(default="", description="위반 유형")
    reason: str = Field(default="", description="위반 사유")
    legal_reference: str = Field(default="", description="참조 법규")
    suggestion: str = Field(default="", description="수정 제안")


class ReviewResult(BaseModel):
    is_violation: bool = Field(..., description="위반 여부")
    violations: list[ViolationItem] = Field(
        default_factory=list,
        description="위반 상세 항목 리스트",
    )
    suggestions: list[str] = Field(
        default_factory=list,
        description="수정 제안 리스트",
    )
    referenced_laws: list[str] = Field(
        default_factory=list,
        description="참조한 법규 목록",
    )


# =========================================================
# 2. Tool 예시
# =========================================================
@tool
def search_law_tool(query: str) -> str:
    """
    법규 검색 샘플 Tool.
    실제 프로젝트에서는 DB/RAG 검색 로직으로 교체하면 됩니다.
    """
    return (
        f"[더미 법규 검색 결과]\n"
        f"- query: {query}\n"
        f"- 건축법 시행령 제61조: 복도 폭은 1200mm 이상이어야 한다.\n"
        f"- NFSC 103: 방화 구획 관련 검토 필요.\n"
    )


TOOLS = [search_law_tool]


# =========================================================
# 3. LangGraph 상태 정의
# =========================================================
class AgentState(TypedDict):
    # ToolNode 가 동작하려면 messages 상태가 필요합니다.
    messages: Annotated[list[AnyMessage], add_messages]

    # 실행 컨텍스트
    user_request: str
    drawing_data: dict
    retrieved_laws: list[dict]

    # 최종 결과
    review_result: dict
    current_step: str

    # [AI-04 안전장치 추가] finalize self-refine 루프 횟수 제한
    retry_count: int


# =========================================================
# 4. 프롬프트 유틸
# =========================================================
SYSTEM_PROMPT = """
너는 도면 법규 검토 전문가다.

규칙:
1. 필요한 경우에만 tool을 호출해서 법규 정보를 보강한다.
2. tool 호출이 끝나면 최종적으로 검토 결과를 정리한다.
3. 최종 답변 단계에서는 반드시 ReviewResult 구조에 맞는 판단 근거를 만든다.
4. 도면 데이터(drawing_data)와 검색된 법규(retrieved_laws)를 우선 참고한다.
5. 정보가 부족하면 추측하지 말고 확인 필요 항목으로 정리한다.
"""


def format_drawing_data(drawing_data: dict) -> str:
    if not drawing_data:
        return "{}"
    return json.dumps(drawing_data, ensure_ascii=False, indent=2)


def format_retrieved_laws(retrieved_laws: list[dict]) -> str:
    if not retrieved_laws:
        return "[]"
    return json.dumps(retrieved_laws, ensure_ascii=False, indent=2)


def build_context_prompt(state: AgentState) -> str:
    return f"""
[사용자 요청]
{state.get("user_request", "")}

[도면 데이터]
{format_drawing_data(state.get("drawing_data", {}))}

[사전 검색된 법규]
{format_retrieved_laws(state.get("retrieved_laws", []))}
"""


def build_default_review_result() -> dict:
    return ReviewResult(
        is_violation=False,
        violations=[],
        suggestions=["검토 실패: 기본 결과를 반환했습니다. 입력 데이터와 모델 상태를 확인해 주세요."],
        referenced_laws=[],
    ).model_dump()


# =========================================================
# 5. LLM 준비
# =========================================================
# # 1차 LLM: tool 호출 여부를 판단하는 agent 모델
# # [AI-04 안전장치 추가] 노드 레벨 재시도
# tool_enabled_llm = (
#     ChatOpenAI(
#         model="qwen3.5-27b-qlora",
#         temperature=0,
#         base_url=settings.VLLM_SERVER_URL,
#         api_key=settings.VLLM_API_KEY,
#     )
#     .bind_tools(TOOLS)
#     .with_retry(stop_after_attempt=3)
# )


# # 2차 LLM: 최종 결과를 ReviewResult 구조로 반환하는 모델
# # [AI-04 안전장치 추가] 노드 레벨 재시도
# structured_llm = (
#     ChatOpenAI(
#         model="qwen3.5-27b-qlora",
#         temperature=0,
#         base_url=settings.VLLM_SERVER_URL,
#         api_key=settings.VLLM_API_KEY,
#     )
#     .with_structured_output(ReviewResult)
#     .with_retry(stop_after_attempt=3)
# )

tool_enabled_llm = ChatOpenAI(
    model=settings.OPENAI_MODEL_NAME,
    temperature=0,
    api_key=settings.OPENAI_API_KEY,
).bind_tools(TOOLS).with_retry(stop_after_attempt=3)

structured_llm = ChatOpenAI(
    model=settings.OPENAI_MODEL_NAME,
    temperature=0,
    api_key=settings.OPENAI_API_KEY,
).with_structured_output(ReviewResult).with_retry(stop_after_attempt=3)



# =========================================================
# 6. Agent Node
# =========================================================
async def agent_node(state: AgentState) -> AgentState:
    """
    역할:
    - 현재 문맥을 보고 tool 호출이 필요한지 결정
    - 필요하면 tool_calls 를 포함한 AIMessage 반환
    - 필요 없으면 바로 다음 finalize 단계로 이동
    """
    context_prompt = build_context_prompt(state)

    response = await tool_enabled_llm.ainvoke(
        [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(
                content=(
                    f"{context_prompt}\n\n"
                    "필요하면 tool을 호출해서 법규를 더 찾아라. "
                    "충분하면 tool 없이 다음 단계로 넘어갈 수 있게 응답해라."
                )
            ),
            *state["messages"],
        ]
    )

    return {
        "messages": [response],
        "current_step": "cross_checked",
    }


# =========================================================
# 7. Finalize Node
# =========================================================
async def finalize_node(state: AgentState) -> AgentState:
    """
    역할:
    - tool 호출이 모두 끝난 뒤
    - 전체 messages 와 현재 컨텍스트를 바탕으로
    - 최종 ReviewResult 구조를 생성
    """
    context_prompt = build_context_prompt(state)

    try:
        result: ReviewResult = await structured_llm.ainvoke(
            [
                SystemMessage(
                    content=(
                        SYSTEM_PROMPT
                        + "\n이제 tool 호출은 끝났다. 최종 검토 결과만 ReviewResult 형식으로 반환하라."
                    )
                ),
                HumanMessage(content=context_prompt),
                *state["messages"],
            ]
        )

        return {
            "review_result": result.model_dump() if hasattr(result, "model_dump") else result,
            "current_step": "review_completed",
        }

    # [AI-04 안전장치 추가] Graph 레벨 self-refine 재시도
    except ValidationError as exc:
        next_retry_count = state.get("retry_count", 0) + 1

        if next_retry_count >= 3:
            return {
                "review_result": {
                    **build_default_review_result(),
                    "suggestions": [
                        "검토 실패: 출력 형식 복구에 3회 실패했습니다. 기본 결과를 반환합니다."
                    ],
                },
                "current_step": "error",
                "retry_count": next_retry_count,
            }

        return {
            "messages": [
                HumanMessage(
                    content=(
                        "출력 형식이 ReviewResult 스키마와 맞지 않습니다: "
                        f"{str(exc)}\n"
                        "반드시 ReviewResult 스키마에 맞는 결과만 다시 생성하세요."
                    )
                )
            ],
            "current_step": "finalize_retry",
            "retry_count": next_retry_count,
        }


# =========================================================
# 8. ToolNode
# =========================================================
tool_node = ToolNode(TOOLS)


# =========================================================
# 9. Graph 라우팅
# =========================================================
def route_after_finalize(state: AgentState) -> str:
    # [AI-04 안전장치 추가] 포맷 오류 시 finalize self-loop
    if state.get("current_step") == "finalize_retry":
        return "retry_finalize"
    return "__end__"


# =========================================================
# 10. Graph 구성
# =========================================================
graph_builder = StateGraph(AgentState)

graph_builder.add_node("agent", agent_node)
graph_builder.add_node("tools", tool_node)
graph_builder.add_node("finalize", finalize_node)

graph_builder.add_edge(START, "agent")

# - agent 가 tool_calls 를 만들면 tools 로 감
# - 아니면 finalize 로 감
graph_builder.add_conditional_edges(
    "agent",
    tools_condition,
    {
        "tools": "tools",
        "__end__": "finalize",
    },
)

# Tool 실행 후 다시 agent 로 돌아가서
# 추가 tool 이 필요한지 / 이제 끝낼지 다시 판단
graph_builder.add_edge("tools", "agent")

# [AI-04 안전장치 추가] finalize 결과에 따라 self-refine loop 또는 종료
graph_builder.add_conditional_edges(
    "finalize",
    route_after_finalize,
    {
        "retry_finalize": "finalize",
        "__end__": END,
    },
)

tool_calling_review_graph = graph_builder.compile()


# =========================================================
# 11. 사용 예시
# =========================================================
if __name__ == "__main__":
    import asyncio

    async def main():
        initial_state: AgentState = {
            "messages": [],
            "user_request": "이 도면에서 복도 폭과 방화 관련 위반이 있는지 검토해줘",
            "drawing_data": {
                "project_name": "A동 증축",
                "floor": "3F",
                "objects": [
                    {"object_id": "door_101", "type": "door", "width_mm": 900},
                    {"object_id": "corridor_01", "type": "corridor", "width_mm": 900},
                ],
            },
            "retrieved_laws": [],
            "review_result": {},
            "current_step": "request_received",
            "retry_count": 0,
        }

        result = await tool_calling_review_graph.ainvoke(initial_state)
        print(json.dumps(result["review_result"], ensure_ascii=False, indent=2))

    asyncio.run(main())
