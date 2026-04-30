"""
File    : backend/services/agents/fire/sub/mapping.py
Author  : 김민정
Create  : 2026-04-15
Description : 소방(fire) 도메인 CAD 매핑 에이전트.
              공통 BaseMappingAgent(common/mapping.py)를 상속하며
              DOMAIN="fire" 로 DB를 조회한다.
              소방 전용 장비 접두어(스프링클러, 감지기, 소화전 등)를 추가 제공한다.

Modification History :
    - 2026-04-15 (김민정) : 초기 작성 (단순 dict 룩업)
    - 2026-04-24 (송주엽) : common/mapping.py 기반으로 전면 재작성
                            org_id 기반 DB 룰 로드 및 lru_cache 적용
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
    소방 도메인 매핑 에이전트 (DOMAIN="fire").
    소방 전용 장비 접두어를 추가로 제공한다.
    """
    DOMAIN = "fire"

    def _get_domain_prefix_map(self) -> dict[str, str]:
        return {
            "SPK":  "스프링클러헤드",
            "HYD":  "소화전",
            "FDH":  "화재감지기",
            "SMK":  "연기감지기",
            "HTD":  "열감지기",
            "MCP":  "수동조작함",
            "FP":   "소화펌프",
            "ALV":  "경보밸브",
            "FCV":  "유량제어밸브",
            "RFV":  "릴리프밸브",
            "DPRV": "건식예작동밸브",
            "PRV":  "감압밸브",
            "FLW":  "유수검지기",
            "TAM":  "탬퍼스위치",
            "FS":   "유수검지스위치",
            "PS":   "압력스위치",
            "FHC":  "소화호스릴함",
            "EX":   "소화기",
        }
