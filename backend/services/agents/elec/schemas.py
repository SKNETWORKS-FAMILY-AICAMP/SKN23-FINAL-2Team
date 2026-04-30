"""
File    : backend/services/agents/electric/schemas.py
Author  : 김지우
Create  : 2026-04-23
Description : 전기 서브 에이전트 호출용 스키마 및 도메인 열거형 정의
"""

from enum import Enum

class ElectricViolationType(str, Enum):
    VOLTAGE_DROP_ERROR       = "voltage_drop_error"          # 전압 강하 초과
    CABLE_AMPACITY_ERROR     = "cable_ampacity_error"        # 허용 전류 부족
    COLOR_MISMATCH_ERROR     = "color_mismatch_error"        # 규정 색상 위반
    CLEARANCE_DISTANCE_ERROR = "clearance_distance_error"    # 이격 거리 위반
    BREAKER_CAPACITY_ERROR   = "breaker_capacity_error"      # 차단기 용량 부족/초과
    CONDUIT_SIZE_ERROR       = "conduit_size_error"          # 배관 점적률 초과 (전선관 크기)
    GROUNDING_WIRE_ERROR     = "grounding_wire_error"        # 접지선 규격 미달
    OPEN_CIRCUIT_ERROR       = "open_circuit_error"          # 회로 단선 (전선 끊김)

class RevisionAction(str, Enum):
    CHANGE_CABLE_SIZE        = "change_cable_size"      # 전선 굵기 변경
    CHANGE_COLOR             = "change_color"           # 객체 색상 변경
    UPDATE_ATTRIBUTE         = "update_attribute"       # 속성값 재지정
    CHANGE_BREAKER_CAPACITY  = "change_breaker_capacity"# 차단기 용량 변경
    CHANGE_CONDUIT_SIZE      = "change_conduit_size"    # 전선관 크기 변경
    MOVE_ENTITY              = "move_entity"            # 이격거리 확보 위한 이동
    MANUAL_REVIEW            = "manual_review"          # 자동 수정 범위 초과 (수동 검토)

ELEC_SUB_AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "call_query_agent",
            "description": "AWS RDS에서 전기 시방서 및 설비 스펙 데이터를 하이브리드 RAG로 검색합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "검색할 전기 규정이나 스펙 쿼리 (예: 케이블 허용 전류 기준)",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "call_review_agent",
            "description": (
                "전기 도면 데이터를 파싱·검증하고 위반 항목별 수정 대안을 계산한 뒤 "
                "수정 결과가 포함된 최종 리뷰 리포트를 반환합니다. "
                "도면이 로드되어 있을 때만 호출하세요. "
                "내부 순서: parser → RAG 검색 → compliance → revision → report."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_id": {
                        "type": "string",
                        "description": (
                            "검토 대상 설비 ID. 시스템 프롬프트에 제공된 설비 목록에서 선택하거나, "
                            "전체 도면 검토 시 'ALL'을 입력하세요."
                        ),
                    },
                },
                "required": ["target_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "call_action_agent",
            "description": (
                "리뷰 리포트의 수정 좌표·대안을 C# 클라이언트 제어 명령 JSON으로 직렬화합니다. "
                "pending_fixes 목록이 있을 때만 호출하세요."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "modifications": {
                        "type": "string",
                        "description": "적용할 수정 항목 JSON (없으면 세션의 pending_fixes 전체 사용)",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_cad_entity_info",
            "description": (
                "CAD 도면에서 특정 handle의 엔티티 정보를 상세 조회합니다. "
                "반드시 context에 존재하는 실제 handle 값을 입력하세요."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "handle": {
                        "type": "string",
                        "description": "조회할 CAD 객체의 handle 값 (예: '2A')",
                    }
                },
                "required": ["handle"],
            },
        },
    },
]