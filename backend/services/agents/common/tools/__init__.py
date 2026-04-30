"""
File    : backend/services/agents/common/tools/__init__.py
Description : 전 도메인 tool 패키지.
              공통 tool 및 각 도메인 전용 tool 을 한 곳에서 관리합니다.

              ── 파일 구조 ─────────────────────────────────────────────────
              common_tools.py       : 전 도메인 공통 @tool 함수 / COMMON_TOOLS
              elec_tools.py     : 전기 도메인 전용 @tool 함수 / ELECTRIC_TOOLS
              fire_tools.py         : 소방 도메인 tool 스텁 / FIRE_SUB_AGENT_TOOLS
              pipe_tools.py       : 배관 도메인 OpenAI 스키마 / PIPE_SUB_AGENT_TOOLS
              arch_tools.py : 건축 도메인 OpenAI 스키마 / ARCH_SUB_AGENT_TOOLS
              ─────────────────────────────────────────────────────────────

              ── 사용 예 ──────────────────────────────────────────────────
              from backend.services.agents.common.tools import COMMON_TOOLS, search_law_tool
              from backend.services.agents.common.tools import AGENT_TOOLS   # 슈퍼바이저용
              from backend.services.agents.common.tools.elec_tools import ELECTRIC_TOOLS
              from backend.services.agents.common.tools.fire_tools import FIRE_SUB_AGENT_TOOLS
              from backend.services.agents.common.tools.pipe_tools import PIPE_SUB_AGENT_TOOLS
              from backend.services.agents.common.tools.arch_tools import ARCH_SUB_AGENT_TOOLS
              ─────────────────────────────────────────────────────────────
"""

from backend.services.agents.common.tools.common_tools import (
    COMMON_TOOLS,
    get_cad_entity_info_tool,
    resolve_ambiguous_mapping_tool,
    search_law_tool,
)
from backend.services.agents.common.tools.agent_tools import (
    AGENT_TOOLS,
    call_arch_agent,
    call_electric_agent,
    call_fire_agent,
    call_pipe_agent,
)

__all__ = [
    # 공통 tool
    "COMMON_TOOLS",
    "search_law_tool",
    "get_cad_entity_info_tool",
    "resolve_ambiguous_mapping_tool",
    # 도메인 에이전트 tool (슈퍼바이저용)
    "AGENT_TOOLS",
    "call_arch_agent",
    "call_electric_agent",
    "call_fire_agent",
    "call_pipe_agent",
]
