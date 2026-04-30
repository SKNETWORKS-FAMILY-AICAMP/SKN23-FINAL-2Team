"""
File    : backend/services/agents/electric/graph.py
Author  : 김지우
Description : LangGraph 기반 대화형(ReAct) 전기 에이전트 상태 머신
"""
from typing import Literal
from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode, tools_condition
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage

from backend.core.config import settings
from backend.services.agents.elec.schemas import ElectricAgentState
from backend.services.agents.common.tools.elec_tools import ELECTRIC_TOOLS
from backend.services.agents.common.tools.common_tools import COMMON_TOOLS

# 1. 사용할 툴 통합 (계산, RAG, 도면 수정 툴 등)
ALL_TOOLS = ELECTRIC_TOOLS + COMMON_TOOLS

# 2. LLM 초기화 및 툴 바인딩
llm = ChatOpenAI(model=settings.OPENAI_MODEL_NAME, temperature=0)
llm_with_tools = llm.bind_tools(ALL_TOOLS)

# --- 노드 로직 ---
async def agent_node(state: ElectricAgentState) -> dict:
    """
    LLM이 사용자의 메시지와 도면 상태를 보고, 
    그냥 대답할지 아니면 Tool을 쓸지 결정합니다.
    """
    # 시스템 프롬프트 부여 (역할 및 제약사항)
    sys_msg = SystemMessage(content=(
        "당신은 오토캐드 전기 도면을 분석하고 수정하는 전문 AI 에이전트입니다.\n"
        "사용자가 도면 분석을 요청하면 툴을 이용해 규격을 확인하고,\n"
        "수정을 요청하면 (예: 선 굵기를 1.5로, 색상을 빨강으로) CAD 수정 명령을 내릴 수 있습니다.\n"
        "친절하고 명확하게 답변하세요."
    ))
    
    # LLM 호출
    response = await llm_with_tools.ainvoke([sys_msg] + state.messages)
    
    # 생성된 메시지를 기존 메시지 배열에 추가 (Annotated[operator.add]에 의해 누적됨)
    return {"messages": [response]}

# Tool 실행 노드는 langgraph.prebuilt의 ToolNode를 그대로 사용합니다.
tool_node = ToolNode(ALL_TOOLS)

# ==========================================
# 대화형 그래프 조립 (Agentic Loop)
# ==========================================
workflow = StateGraph(ElectricAgentState)

# 노드 추가
workflow.add_node("agent", agent_node)
workflow.add_node("tools", tool_node)

# 흐름 제어: 시작 -> 에이전트
workflow.add_edge(START, "agent")

# 에이전트 노드 이후의 조건부 라우팅
# LLM이 툴을 호출했으면 'tools' 노드로, 일반 대화면 'END'로 빠져나가 사용자에게 답변 반환
workflow.add_conditional_edges(
    "agent",
    tools_condition,  # LangGraph 내장 라우터 (Tool 호출 여부 판단)
    {"tools": "tools", END: END}
)

# 툴 실행이 끝나면 다시 에이전트에게 돌아가서 결과를 보고 판단하게 함
workflow.add_edge("tools", "agent")

# 전역 컴파일
electric_agent_graph = workflow.compile()