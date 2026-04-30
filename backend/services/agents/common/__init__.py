"""
File    : backend/services/agents/common/__init__.py
Author  : 송주엽
Create  : 2026-04-16
Description : 전 도메인 에이전트 공통 유틸리티 패키지.

              ── 공개 API ─────────────────────────────────────────────────
              [tools/common_tools.py]  공통 @tool 함수 / COMMON_TOOLS 리스트
              [tools/elec_tools.py] 전기 전용 @tool 함수 / ELECTRIC_TOOLS
              [multi_object_mapper.py] 다중 CAD 객체 매핑 함수

              ── 빠른 사용 예 ─────────────────────────────────────────────
              from backend.services.agents.common import (
                  # @tool 함수
                  COMMON_TOOLS,
                  search_law_tool,
                  get_cad_entity_info_tool,
                  resolve_ambiguous_mapping_tool,

                  # 다중 객체 매핑
                  LayerBonusConfig,
                  calculate_mapping_score,
                  find_best_match,
                  llm_fallback_resolver,
                  auto_map_entities,
              )

              # 도메인 전용 tool 은 직접 import
              from backend.services.agents.common.tools.elec_tools import ELECTRIC_TOOLS

              # 도메인 graph 에서 COMMON_TOOLS 에 도메인 tool 추가
              TOOLS = COMMON_TOOLS + [my_piping_tool, my_other_tool]
              ─────────────────────────────────────────────────────────────
"""

from backend.services.agents.common.multi_object_mapper import (
    LayerBonusConfig,
    MappingResult,
    auto_map_entities,
    calculate_mapping_score,
    find_best_match,
    llm_fallback_resolver,
)

# tools 패키지는 langchain_core 가 설치된 환경에서만 동작하므로 지연 임포트
# 사용 시: from backend.services.agents.common.tools import search_law_tool, COMMON_TOOLS
def __getattr__(name: str):
    _tool_names = {
        "COMMON_TOOLS",
        "search_law_tool",
        "get_cad_entity_info_tool",
        "resolve_ambiguous_mapping_tool",
    }
    if name in _tool_names:
        from backend.services.agents.common.tools import common_tools as _tools
        return getattr(_tools, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    # tools
    "COMMON_TOOLS",
    "search_law_tool",
    "get_cad_entity_info_tool",
    "resolve_ambiguous_mapping_tool",
    # multi_object_mapper
    "LayerBonusConfig",
    "MappingResult",
    "calculate_mapping_score",
    "find_best_match",
    "llm_fallback_resolver",
    "auto_map_entities",
]
