"""
File    : backend/services/agents/elec/sub/mapping.py
Author  : 송주엽
Create  : 2026-04-09
Description : 전기(elec) 도메인 CAD 매핑 에이전트.
              공통 BaseMappingAgent(common/mapping.py)를 상속하며
              DOMAIN="elec" 로 DB를 조회한다.

Modification History :
    - 2026-04-09 (송주엽) : 초기 작성
    - 2026-04-24 (송주엽) : common/mapping.py 기반으로 리팩토링 (391줄 → 박피)
                            버그 수정: 싱글턴 엔진(pool_size=3) 자동 적용
                            (기존: 캐시 미스 시 새 엔진 생성 후 즉시 dispose → 연결 풀 미재사용)
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
    전기 도메인 매핑 에이전트 (DOMAIN="elec").
    전기 전용 장비 접두어를 추가로 제공한다.
    """
    DOMAIN = "elec"

    def _get_domain_prefix_map(self) -> dict[str, str]:
        return {
            "CB":   "차단기",
            "MCB":  "배선용차단기",
            "MCCB": "배선용차단기",
            "ELB":  "누전차단기",
            "MS":   "전자개폐기",
            "TR":   "변압기",
            "PNL":  "분전반",
            "MCC":  "모터제어반",
            "UPS":  "무정전전원장치",
            "GEN":  "발전기",
            "SW":   "개폐기",
            "DS":   "단로기",
            "ACB":  "기중차단기",
            "VCB":  "진공차단기",
            "MOF":  "계기용변성기",
            "PT":   "계기용변압기",
            "CT":   "변류기",
            "GR":   "접지저항기",
        }
