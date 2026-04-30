"""
File    : backend/services/agents/architecture/sub/review/revision.py
Author  : 김다빈
WBS     : AI-05 (건축 도메인 에이전트)
Create  : 2026-04-15

Description :
    ComplianceAgent의 위반 리포트를 기반으로 건축 도면 수정 대안을 연산.
    RevisionAction은 DrawingPatcher의 12종 AutoFix 타입과 1:1 대응.

    위반 타입 → RevisionAction 매핑:
        CORRIDOR_WIDTH_ERROR    → SCALE  (폭 확장)
        FIRE_COMPARTMENT_AREA  → GEOMETRY (구획 경계 재설정)
        EXIT_DISTANCE_ERROR    → MOVE   (출구 위치 이동)
        STAIR_DIMENSION_ERROR  → SCALE  (계단 폭 확장)
        FLOOR_HEIGHT_ERROR     → GEOMETRY (층고 조정)
        WINDOW_AREA_ERROR      → SCALE  (창호 크기 확장)
        FIRE_DOOR_ERROR        → ATTRIBUTE (문 속성 변경)
        SETBACK_DISTANCE_ERROR → MOVE   (후퇴)
        ACCESSIBILITY_ERROR    → ATTRIBUTE (접근성 속성 수정)
        STRUCTURE_SPACING_ERROR→ MOVE
        WALL_THICKNESS_ERROR   → SCALE
        ROOM_AREA_ERROR        → SCALE
        LAYER_VIOLATION        → LAYER  (레이어 변경)
"""

from backend.services.agents.arch.schemas import ArchViolationType, RevisionAction


_VIOLATION_ACTION_MAP: dict[str, str] = {
    ArchViolationType.CORRIDOR_WIDTH_ERROR:    RevisionAction.SCALE,
    ArchViolationType.FIRE_COMPARTMENT_AREA:   RevisionAction.GEOMETRY,
    ArchViolationType.EXIT_DISTANCE_ERROR:     RevisionAction.MOVE,
    ArchViolationType.STAIR_DIMENSION_ERROR:   RevisionAction.SCALE,
    ArchViolationType.FLOOR_HEIGHT_ERROR:      RevisionAction.GEOMETRY,
    ArchViolationType.WINDOW_AREA_ERROR:       RevisionAction.SCALE,
    ArchViolationType.FIRE_DOOR_ERROR:         RevisionAction.ATTRIBUTE,
    ArchViolationType.SETBACK_DISTANCE_ERROR:  RevisionAction.MOVE,
    ArchViolationType.ACCESSIBILITY_ERROR:     RevisionAction.ATTRIBUTE,
    ArchViolationType.STRUCTURE_SPACING_ERROR: RevisionAction.MOVE,
    ArchViolationType.WALL_THICKNESS_ERROR:    RevisionAction.SCALE,
    ArchViolationType.ROOM_AREA_ERROR:         RevisionAction.SCALE,
    ArchViolationType.LAYER_VIOLATION:         RevisionAction.LAYER,
}


class RevisionAgent:
    def calculate_fix(self, violations: list) -> list:
        """
        위반 항목마다 수정 액션을 결정하고 파라미터를 반환합니다.

        Returns
        -------
        list of:
            {handle, violation_type, proposed_fix: {action, ...}}
        """
        fixes = []
        for violation in violations:
            handle = violation.get("handle")
            v_type = violation.get("violation_type")
            fix = self._dispatch(v_type, violation)
            fixes.append({"handle": handle, "violation_type": v_type, "proposed_fix": fix})
        return fixes

    def _dispatch(self, v_type: str, violation: dict) -> dict:
        action = _VIOLATION_ACTION_MAP.get(v_type, RevisionAction.MANUAL_REVIEW)

        handlers = {
            RevisionAction.MOVE:         self._move,
            RevisionAction.SCALE:        self._scale,
            RevisionAction.GEOMETRY:     self._geometry,
            RevisionAction.ATTRIBUTE:    self._attribute,
            RevisionAction.LAYER:        self._layer,
        }
        handler = handlers.get(action)
        if handler:
            return handler(violation)
        return {
            "action": RevisionAction.MANUAL_REVIEW,
            "reason": f"{v_type} — 자동 수정 범위를 벗어남. 수동 검토 필요.",
        }

    @staticmethod
    def _move(violation: dict) -> dict:
        return {
            "action":         RevisionAction.MOVE,
            "reason":         violation.get("reason", ""),
            "current_value":  violation.get("current_value"),
            "required_value": violation.get("required_value"),
            "note": "이동 방향은 도면 컨텍스트 기반으로 결정 필요",
        }

    @staticmethod
    def _scale(violation: dict) -> dict:
        """required_value 수치를 파싱하여 목표 스케일 계산."""
        try:
            req_str = str(violation.get("required_value", "0"))
            cur_str = str(violation.get("current_value", "1"))
            # mm 숫자만 추출
            import re
            req = float(re.sub(r"[^\d.]", "", req_str) or "0")
            cur = float(re.sub(r"[^\d.]", "", cur_str) or "1")
            scale_factor = round(req / cur, 4) if cur > 0 else 1.0
        except Exception:
            scale_factor = 1.0

        return {
            "action":         RevisionAction.SCALE,
            "scale_factor":   scale_factor,
            "current_value":  violation.get("current_value"),
            "required_value": violation.get("required_value"),
            "reason":         violation.get("reason", ""),
        }

    @staticmethod
    def _geometry(violation: dict) -> dict:
        return {
            "action":         RevisionAction.GEOMETRY,
            "current_value":  violation.get("current_value"),
            "required_value": violation.get("required_value"),
            "reason":         violation.get("reason", ""),
            "note": "꼭짓점 재배치가 필요합니다. 수동 확인 후 적용하세요.",
        }

    @staticmethod
    def _attribute(violation: dict) -> dict:
        return {
            "action":     RevisionAction.ATTRIBUTE,
            "key":        violation.get("violation_type", ""),
            "new_value":  violation.get("required_value"),
            "old_value":  violation.get("current_value"),
            "reason":     violation.get("reason", ""),
        }

    @staticmethod
    def _layer(violation: dict) -> dict:
        return {
            "action":      RevisionAction.LAYER,
            "new_layer":   violation.get("required_value", ""),
            "current_layer": violation.get("layer", ""),
            "reason":      violation.get("reason", ""),
        }
