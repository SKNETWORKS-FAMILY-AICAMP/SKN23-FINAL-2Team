"""
File    : backend/services/agents/arch/sub/mapping.py
Author  : 김다빈
Create  : 2026-04-24
Description : 건축(arch) 도메인 CAD 매핑 에이전트.
              공통 BaseMappingAgent(common/mapping.py)를 상속하며
              DOMAIN="arch" 로 DB를 조회한다.
              건축 전용 레이어·블록 접두어(벽체, 기둥, 보, 계단 등)를 추가 제공한다.
"""

from backend.services.agents.common.mapping import (
    BaseMappingAgent,
    _is_ignored_layer,
    invalidate_mapping_cache,
    get_mapping_cache_stats,
)

__all__ = [
    "MappingAgent",
    "_is_ignored_layer",
    "invalidate_mapping_cache",
    "get_mapping_cache_stats",
]


class MappingAgent(BaseMappingAgent):
    """
    건축 도메인 매핑 에이전트 (DOMAIN="arch").
    건축 전용 레이어·블록 접두어를 추가로 제공한다.
    """
    DOMAIN = "arch"

    def _get_domain_prefix_map(self) -> dict[str, str]:
        return {
            "W":    "벽체",
            "WL":   "벽체",
            "COL":  "기둥",
            "BM":   "보",
            "SL":   "슬래브",
            "FND":  "기초",
            "STR":  "계단",
            "RMP":  "경사로",
            "DOOR": "문",
            "WIN":  "창문",
            "CLG":  "천장",
            "FLR":  "바닥",
            "RF":   "지붕",
            "PRT":  "파라펫",
            "BAL":  "발코니",
            "COR":  "복도",
            "ELV":  "엘리베이터",
            "ESC":  "에스컬레이터",
            "SH":   "샤프트",
            "MR":   "기계실",
        }
