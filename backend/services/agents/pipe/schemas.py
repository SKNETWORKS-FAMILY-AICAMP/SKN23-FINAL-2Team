"""
File    : backend/services/agents/piping/schemas.py
Author  : 송주엽
Create  : 2026-04-09
Description : 배관 서브 에이전트 호출용 OpenAI Tool 스키마 및 도메인 열거형 정의

Modification History :
    - 2026-04-09 (송주엽) : 초기 도구 스키마 및 열거형 구조 정의
    - 2026-04-15 (송주엽) : call_review_agent에서 spec_context·layout_data를 optional로 변경
                            (workflow_handler가 직접 RAG 검색 및 context 참조하므로 LLM이 생성 불필요)
    - 2026-04-17 (송주엽) : PIPE_SUB_AGENT_TOOLS 임포트를 파일 상단으로 이동
"""

from enum import Enum

from backend.services.agents.common.tools.pipe_tools import PIPE_SUB_AGENT_TOOLS  # noqa: F401


class PipingViolationType(str, Enum):
    HANGER_SPACING_ERROR       = "hanger_spacing_error"        # 행거 간격 초과
    SLOPE_ERROR                = "slope_error"                 # 배관 경사도 부적합
    VALVE_POSITION_ERROR       = "valve_position_error"        # 밸브 접근성/높이 부적합
    PRESSURE_OVERLOAD          = "pressure_overload"           # 압력등급 초과
    PIPE_SIZE_MISMATCH         = "pipe_size_mismatch"          # 구경 부적합
    INSULATION_THICKNESS_ERROR = "insulation_thickness_error"  # 보온재 두께 부족
    FIRE_PENETRATION_ERROR     = "fire_penetration_error"      # 관통부 방화충전 누락
    DISTANCE_ERROR             = "distance_error"              # 설비 간 이격거리 위반
    MATERIAL_MISMATCH          = "material_mismatch"           # 유체/압력 등급 대비 재질 부적합
    SEISMIC_SUPPORT_ERROR      = "seismic_support_error"       # 내진 지지 누락
    EXPANSION_JOINT_MISSING    = "expansion_joint_missing"     # 신축이음 누락


class RevisionAction(str, Enum):

    MOVE_ENTITY          = "move_entity"            # 이격거리 조정
    ADJUST_SLOPE         = "adjust_slope"           # 기울기 조정
    CHANGE_SIZE          = "change_size"            # 구경 변경
    ADD_HANGER           = "add_hanger"             # 행거 추가
    ADD_EXPANSION_JOINT  = "add_expansion_joint"    # 신축이음 추가
    ADD_FIRE_SEAL        = "add_fire_seal"          # 방화충전 추가
    ADD_SEISMIC_HANGER   = "add_seismic_hanger"     # 내진 행거 추가
    REPLACE_MATERIAL     = "replace_material"       # 재질 교체
    REDUCE_PRESSURE      = "reduce_pressure"        # 감압 권고
    ADD_INSULATION       = "add_insulation"         # 보온재 추가/변경
    MANUAL_REVIEW        = "manual_review"          # 자동 수정 범위 초과 — 수동 검토
