"""
File    : backend/services/graph/review_graph.py
Author  : 양창일
Create  : 2026-04-14
Description : review_agent_node 와 memory_summary_node 를 실제 LangGraph 워크플로우에 연결하는 공통 그래프 빌더

Modification History :
    - 2026-04-14 (양창일) : review_agent_node 연결용 공통 LangGraph 추가
    - 2026-04-15 (양창일) : 메모리 요약 노드를 후속 단계로 연결

    - 2026-04-17 (김지우) : 4개 도메인(electric, piping, architecture, fire)의 LangGraph 워크플로우를 조립하는 메인 그래프 빌더

    - 2026-04-17 (송주엽) : build_review_graph 파라미터화 — 배관 그래프 통합
                            pipe_review_graph를 이 파일에서 함께 생성

    - 2026-04-18 (김지우) : 전기 도메인 그래프 인스턴스 추가 (electric_review_graph 파일 필요 부분만 가져옴)

    - 2026-04-25 (김다빈) : arch_review_graph를 전용 노드(arch_review_node)로 교체.
                            기존 공통 템플릿(review_agent_node)은 handle→RevCloud 정합이 없어
                            건축 위반 엔티티가 CAD에 정확히 표시되지 않는 버그가 있었음.
                            arch_review_node는 ArchAgent._violations_from_report_items()와
                            _build_pending_fixes()를 재사용하여 handle→object_id 체인 보장.

"""
from __future__ import annotations

from typing import Any, Callable, Optional

from langgraph.graph import END, START, StateGraph

from backend.services.graph.state import AgentState

# 공통 및 일반 도메인 노드 임포트
from backend.services.graph.nodes.memory_summary_node import memory_summary_node
from backend.services.graph.nodes.review_agent_template import review_agent_node
from backend.services.graph.nodes.pipe_review_node import pipe_review_node
from backend.services.graph.nodes.fire_review_node import fire_review_node

# 1. 선형 워크플로우 빌더 (건축, 소방, 배관용)
#    배관: LangGraph 노드 2개 — domain_node(=pipe_review_node) → memory_summary_node.
#    내부에서 워크플로 핸들러가 query/review/action 서브에이전트 3갈래를 탄다(그래프 노드 수와 별개).
def build_review_graph(node_fn: Callable = review_agent_node):
    workflow = StateGraph(AgentState)
    workflow.add_node("domain_node", node_fn)
    workflow.add_node("memory_summary_node", memory_summary_node)
    
    workflow.add_edge(START, "domain_node")
    workflow.add_edge("domain_node", "memory_summary_node")
    workflow.add_edge("memory_summary_node", END)
    return workflow.compile()

from backend.services.graph.nodes.elec_review_node import elec_review_node
from backend.services.graph.nodes.arch_review_node import arch_review_node

def build_electric_graph():
    """하위 호환: 전기 그래프 인스턴스(지연 생성)."""
    return electric_review_graph

electric_review_graph = build_review_graph(node_fn=elec_review_node)



pipe_review_graph = build_review_graph(node_fn=pipe_review_node)
arch_review_graph = build_review_graph(node_fn=arch_review_node)
fire_review_graph = build_review_graph(node_fn=fire_review_node)
review_graph = build_review_graph()
