"""
File    : backend/services/agents/fire/schemas.py
Author  : 김민정
Create  : 2026-04-15
Description : 소방 서브 에이전트 호출을 위한 OpenAI Tool 스키마 정의

Modification History :
    - 2026-04-15 (김민정) : 소방 서브 에이전트 호출
    - 2026-04-17 (송주엽) : tool 정의를 common/tools/fire_tools.py 로 이동
"""

from backend.services.agents.common.tools.fire_tools import (  # noqa: F401
    FIRE_LANGCHAIN_TOOLS,
    FIRE_SUB_AGENT_TOOLS,
    call_action_agent,
    call_query_agent,
    call_review_agent,
)