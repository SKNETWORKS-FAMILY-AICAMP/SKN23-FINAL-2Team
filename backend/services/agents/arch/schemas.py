"""
File    : backend/services/agents/architecture/schemas.py
Author  : 김다빈
WBS     : AI-05 (건축 도메인 에이전트)
Create  : 2026-04-15

Description :
    건축 서브 에이전트 호출용 OpenAI Tool 스키마 및 도메인 열거형 정의.
    위반 타입은 건축법 시행령 기준 주요 항목으로 구성.
    RevisionAction은 DrawingPatcher의 12종 AutoFix 타입과 1:1 대응.
"""

from enum import Enum


class ArchViolationType(str, Enum):
    FIRE_COMPARTMENT_AREA     = "fire_compartment_area"      # 방화구획 면적 초과
    CORRIDOR_WIDTH_ERROR      = "corridor_width_error"       # 복도/통로 폭 미달
    EXIT_DISTANCE_ERROR       = "exit_distance_error"        # 피난 거리/출구 간격 위반
    STAIR_DIMENSION_ERROR     = "stair_dimension_error"      # 계단 폭·단높이·단너비 위반
    FLOOR_HEIGHT_ERROR        = "floor_height_error"         # 층고 미달
    WINDOW_AREA_ERROR         = "window_area_error"          # 채광·환기창 면적 미달
    FIRE_DOOR_ERROR           = "fire_door_error"            # 방화문 규격 위반
    SETBACK_DISTANCE_ERROR    = "setback_distance_error"     # 인접 경계선 이격 거리 위반
    ACCESSIBILITY_ERROR       = "accessibility_error"        # 장애인 편의시설 미비
    STRUCTURE_SPACING_ERROR   = "structure_spacing_error"    # 구조 부재 간격 위반
    WALL_THICKNESS_ERROR      = "wall_thickness_error"       # 벽체 두께 미달
    ROOM_AREA_ERROR           = "room_area_error"            # 최소 실 면적 미달
    LAYER_VIOLATION           = "layer_violation"            # 잘못된 레이어 배치


class RevisionAction(str, Enum):
    MOVE         = "MOVE"          # 위치 이동 (이격거리 조정)
    SCALE        = "SCALE"         # 크기 조정 (폭·면적 확장)
    DELETE       = "DELETE"        # 불필요 요소 제거
    LAYER        = "LAYER"         # 레이어 변경
    ATTRIBUTE    = "ATTRIBUTE"     # 블록 속성 변경
    TEXT_CONTENT = "TEXT_CONTENT"  # 텍스트 내용 수정
    TEXT_HEIGHT  = "TEXT_HEIGHT"   # 텍스트 높이 조정
    COLOR        = "COLOR"         # 색상 변경
    LINETYPE     = "LINETYPE"      # 선종류 변경
    LINEWEIGHT   = "LINEWEIGHT"    # 선굵기 변경
    ROTATE       = "ROTATE"        # 회전
    GEOMETRY     = "GEOMETRY"      # 기하형상 변경 (꼭짓점 이동)
    MANUAL_REVIEW = "MANUAL_REVIEW" # 자동 수정 불가 — 수동 검토 필요


from backend.services.agents.common.tools.arch_tools import ARCH_SUB_AGENT_TOOLS  # noqa: F401
