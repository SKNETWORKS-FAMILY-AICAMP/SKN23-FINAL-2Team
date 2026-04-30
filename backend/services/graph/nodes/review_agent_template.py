"""
File    : backend/services/graph/nodes/review_agent_template.py
Author  : 양창일
Create  : 2026-04-14
Description : LangGraph 기반 도면 법규 검토 공통 에이전트 노드 템플릿

Modification History :
    - 2026-04-14 (양창일) : 공통 에이전트 노드 템플릿 초기 구조 작성
    - 2026-04-14 (양창일) : LangGraph 실연결을 위한 실제 review_agent_node 구현 정리
    - 2026-04-15 (양창일) : summary_text 와 recent_chat 기반 메모리 프롬프트 주입 구조 반영
    - 2026-04-15 (김다빈) : 도메인별 에이전트 라우팅 연결 — ArchAgent/PipingAgent 등 실제 호출
"""

from __future__ import annotations

import logging
from typing import Any

from backend.services.graph.state import AgentState


DOMAIN_LABELS: dict[str, str] = {
    "arch": "건축",
    "elec": "전기",
    "fire": "소방",
    "pipe": "배관",
}


def _get_agent(domain: str):
    """도메인별 에이전트 인스턴스 반환 (지연 임포트 — 순환 참조 방지)"""
    if domain == "arch":
        from backend.services.agents.arch.arch_agent import ArchAgent
        return ArchAgent()
    elif domain == "pipe":
        # pipe 도메인은 agent_service.py에서 pipe_review_graph로 직접 라우팅됨
        # review_graph → review_agent_node 경로로는 호출되지 않음
        return None
    elif domain == "elec":
        from backend.services.agents.elec.electric_agent import ElectricAgent
        return ElectricAgent()
    elif domain == "fire":
        from backend.services.agents.fire.fire_agent import FireAgent
        return FireAgent()
    return None


def _default_review_result() -> dict[str, Any]:
    return {
        "is_violation": False,
        "violations": [],
        "suggestions": [],
        "referenced_laws": [],
        "final_message": "",
    }


async def review_agent_node(state: AgentState) -> AgentState:
    """
    도메인 에이전트를 라우팅하여 실행하고 결과를 AgentState에 병합합니다.

    도메인별 에이전트:
        arch → ArchAgent
        pipe → PipingAgent
        elec → ElectricAgent
        fire → FireAgent

    각 에이전트는 db=None 으로 호출됩니다.
    에이전트 내부에서 SessionLocal()로 자체 DB 세션을 생성합니다.
    """
    domain = state["session_meta"].get("domain_type", "")
    domain_name = DOMAIN_LABELS.get(domain, domain)

    agent = _get_agent(domain)
    if agent is None:
        logging.error("[review_agent_node] 알 수 없는 도메인: %s", domain)
        return {
            **state,
            "review_result": {
                **_default_review_result(),
                "final_message": f"지원하지 않는 도메인입니다: {domain}",
            },
            "current_step": "error",
        }

    # AgentState를 payload로 그대로 전달 (domain agent의 run()은 payload dict를 기대)
    try:
        result = await agent.run(dict(state), db=None)
    except Exception as e:
        logging.exception("[review_agent_node] %s 에이전트 실행 오류", domain_name)
        return {
            **state,
            "review_result": {
                **_default_review_result(),
                "final_message": f"{domain_name} 에이전트 실행 중 오류 발생: {e}",
            },
            "current_step": "error",
        }

    # 에이전트 결과를 AgentState에 병합
    review_result = result.get("review_result") or _default_review_result()
    final_message  = review_result.get("final_message", "")
    if not final_message:
        if review_result.get("is_violation"):
            final_message = f"{domain_name} 검토 결과 위반 항목이 확인되었습니다."
        else:
            final_message = f"{domain_name} 검토 결과 위반 항목이 확인되지 않았습니다."
        review_result["final_message"] = final_message

    return {
        **state,
        "review_result":     review_result,
        "pending_fixes":     result.get("pending_fixes") or [],
        "active_object_ids": result.get("active_object_ids") or state.get("active_object_ids") or [],
        "current_step":      result.get("current_step") or "review_completed",
        "assistant_response": final_message,
    }
