"""
File    : backend/services/agents/common/tools/agent_tools.py
Author  : 송주엽
Create  : 2026-04-17
Description : 각 도메인 에이전트(건축/배관/전기/소방)를 LangChain @tool 로 노출합니다.
              슈퍼바이저 LLM 이 도메인을 판단한 뒤 아래 tool 을 호출하면
              내부적으로 AgentService.run() → 해당 도메인 LangGraph 가 실행됩니다.

              ── 사용 예 (슈퍼바이저 그래프) ─────────────────────────────
              from backend.services.agents.common.tools.agent_tools import AGENT_TOOLS

              llm_with_tools = llm.bind_tools(AGENT_TOOLS)
              ─────────────────────────────────────────────────────────────

Modification History :
    - 2026-04-17 (송주엽) : 최초 작성 — 4개 도메인 agent @tool 래퍼 구현
"""

from __future__ import annotations

import json
import logging

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# ── 공통 내부 헬퍼 ──────────────────────────────────────────────────────────

async def _invoke_agent(domain: str, user_request: str, payload_json: str) -> str:
    """도메인 에이전트를 실행하고 결과를 JSON 문자열로 반환합니다."""
    from backend.core.database import SessionLocal
    from backend.services.agent_service import AgentService

    try:
        payload: dict = json.loads(payload_json) if payload_json else {}
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"payload_json 파싱 실패: {e}"}, ensure_ascii=False)

    payload.setdefault("message", user_request)

    db = SessionLocal()
    try:
        result = await AgentService().run(
            domain=domain,
            state={},       # 세션 없이 단발 호출 — _build_initial_state 가 payload 로 채움
            payload=payload,
            db=db,
        )
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as exc:
        logger.error("[agent_tools] %s 에이전트 실행 오류: %s", domain, exc)
        return json.dumps({"error": str(exc), "domain": domain}, ensure_ascii=False)
    finally:
        db.close()


# =========================================================
# 1. 건축 도메인 에이전트
# =========================================================
@tool
async def call_arch_agent(user_request: str, payload_json: str = "{}") -> str:
    """
    건축 도메인 에이전트를 호출합니다.

    건축법 시행령·주택건설기준 기반 CAD 도면 검토(방화구획·복도 폭·피난 동선 등)
    및 위반 항목 수정 명령 생성을 수행합니다.

    Args:
        user_request : 사용자 요청 문자열 (예: "방화구획 면적을 검토해줘")
        payload_json : 추가 컨텍스트 JSON 문자열.
                       선택 키: drawing_data, retrieved_laws, active_object_ids,
                                pending_fixes, org_id, spec_guid

    Returns:
        에이전트 실행 결과 JSON 문자열
        {"domain", "current_step", "drawing_data", "review_result", "pending_fixes", ...}
    """
    return await _invoke_agent("arch", user_request, payload_json)


# =========================================================
# 2. 전기 도메인 에이전트
# =========================================================
@tool
async def call_electric_agent(user_request: str, payload_json: str = "{}") -> str:
    """
    전기 도메인 에이전트를 호출합니다.

    KEC 전기설비기술기준 기반 CAD 도면 검토(전압 강하·이격 거리·허용 전류 등)
    및 위반 항목 수정 명령 생성을 수행합니다.

    Args:
        user_request : 사용자 요청 문자열 (예: "전선 이격거리 위반을 확인해줘")
        payload_json : 추가 컨텍스트 JSON 문자열.
                       선택 키: drawing_data, retrieved_laws, active_object_ids,
                                pending_fixes, org_id, spec_guid

    Returns:
        에이전트 실행 결과 JSON 문자열
        {"domain", "current_step", "drawing_data", "review_result", "pending_fixes", ...}
    """
    return await _invoke_agent("elec", user_request, payload_json)


# =========================================================
# 3. 소방 도메인 에이전트
# =========================================================
@tool
async def call_fire_agent(user_request: str, payload_json: str = "{}") -> str:
    """
    소방 도메인 에이전트를 호출합니다.

    NFSC 국가화재안전기준 기반 CAD 도면 검토(스프링클러 헤드 간격·소화전 위치 등)
    및 위반 항목 수정 명령 생성을 수행합니다.

    Args:
        user_request : 사용자 요청 문자열 (예: "스프링클러 배치 기준을 검토해줘")
        payload_json : 추가 컨텍스트 JSON 문자열.
                       선택 키: drawing_data, retrieved_laws, active_object_ids,
                                pending_fixes, org_id, spec_guid

    Returns:
        에이전트 실행 결과 JSON 문자열
        {"domain", "current_step", "drawing_data", "review_result", "pending_fixes", ...}
    """
    return await _invoke_agent("fire", user_request, payload_json)


# =========================================================
# 4. 배관 도메인 에이전트
# =========================================================
@tool
async def call_pipe_agent(user_request: str, payload_json: str = "{}") -> str:
    """
    배관 도메인 에이전트를 호출합니다.

    KCS 배관 시방서 기반 CAD 도면 검토(행거 간격·경사도·이격거리·재질 등)
    및 위반 항목 수정 명령 생성을 수행합니다.

    Args:
        user_request : 사용자 요청 문자열 (예: "급수 배관 행거 간격을 검토해줘")
        payload_json : 추가 컨텍스트 JSON 문자열.
                       선택 키: drawing_data, retrieved_laws, active_object_ids,
                                pending_fixes, org_id, spec_guid

    Returns:
        에이전트 실행 결과 JSON 문자열
        {"domain", "current_step", "drawing_data", "review_result", "pending_fixes", ...}
    """
    return await _invoke_agent("pipe", user_request, payload_json)


# =========================================================
# 편의 목록 — 슈퍼바이저에서 llm.bind_tools(AGENT_TOOLS) 로 사용
# =========================================================
AGENT_TOOLS = [
    call_arch_agent,
    call_electric_agent,
    call_fire_agent,
    call_pipe_agent,
]
