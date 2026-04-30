"""
File    : backend/services/agents/electric/sub/review/revision.py
Author  : 김지우
Description : ComplianceAgent의 위반 리포트를 기반으로 전기 설비 수정 대안을 연산
"""

from backend.services.agents.elec.schemas import ElectricViolationType, RevisionAction

_VIOLATION_ACTION_MAP: dict[str, str] = {
    ElectricViolationType.VOLTAGE_DROP_ERROR:        RevisionAction.CHANGE_CABLE_SIZE,
    ElectricViolationType.CABLE_AMPACITY_ERROR:      RevisionAction.CHANGE_CABLE_SIZE,
    ElectricViolationType.COLOR_MISMATCH_ERROR:      RevisionAction.CHANGE_COLOR,
    ElectricViolationType.CLEARANCE_DISTANCE_ERROR:  RevisionAction.MOVE_ENTITY,
    ElectricViolationType.BREAKER_CAPACITY_ERROR:    RevisionAction.CHANGE_BREAKER_CAPACITY,
    ElectricViolationType.CONDUIT_SIZE_ERROR:        RevisionAction.CHANGE_CONDUIT_SIZE,
    ElectricViolationType.GROUNDING_WIRE_ERROR:      RevisionAction.UPDATE_ATTRIBUTE,
}

class RevisionAgent:
    def calculate_fix(self, violations: list, current_layout: dict) -> list:
        fixes = []
        for violation in violations:
            target_id = violation.get("equipment_id")
            v_type = violation.get("violation_type")
            current = current_layout.get(target_id, {})
            fix_action = self._dispatch(v_type, violation, current)
            fixes.append({"equipment_id": target_id, "proposed_fix": fix_action})
        return fixes

    def _dispatch(self, v_type: str, violation: dict, current: dict) -> dict:
        # 직접 조회 → str,Enum이므로 소문자 값이면 일치. LLM이 대/소문자를 섞을 경우를 위해 lower 폴백
        action = _VIOLATION_ACTION_MAP.get(v_type)
        if action is None:
            normalized = str(v_type or "").lower().strip()
            action = next(
                (val for key, val in _VIOLATION_ACTION_MAP.items() if str(key).lower() == normalized),
                RevisionAction.MANUAL_REVIEW,
            )

        handlers = {
            RevisionAction.CHANGE_CABLE_SIZE: self._change_cable_size,
            RevisionAction.CHANGE_COLOR: self._change_color,
            RevisionAction.CHANGE_BREAKER_CAPACITY: self._change_breaker_capacity,
            RevisionAction.CHANGE_CONDUIT_SIZE: self._change_conduit_size,
            RevisionAction.MOVE_ENTITY: self._move_entity,
            RevisionAction.UPDATE_ATTRIBUTE: self._update_attribute,
        }
        handler = handlers.get(action)
        if handler:
            return handler(violation, current)
        return {"action": RevisionAction.MANUAL_REVIEW, "reason": "자동 수정 범위를 벗어남"}

    @staticmethod
    def _change_cable_size(violation: dict, current: dict) -> dict:
        return {
            "action": RevisionAction.CHANGE_CABLE_SIZE,
            "required_size": violation.get("required_value"),
            "current_size": violation.get("current_value"),
            "equipment_position": current,
        }

    @staticmethod
    def _change_color(violation: dict, current: dict) -> dict:
        return {
            "action": RevisionAction.CHANGE_COLOR,
            "required_color": violation.get("required_value"),
            "equipment_position": current,
        }

    @staticmethod
    def _change_breaker_capacity(violation: dict, current: dict) -> dict:
        return {
            "action": RevisionAction.CHANGE_BREAKER_CAPACITY,
            "required_capacity": violation.get("required_value"),
            "equipment_position": current,
        }

    @staticmethod
    def _change_conduit_size(violation: dict, current: dict) -> dict:
        return {
            "action": RevisionAction.CHANGE_CONDUIT_SIZE,
            "required_size": violation.get("required_value"),
            "equipment_position": current,
        }

    @staticmethod
    def _move_entity(violation: dict, current: dict) -> dict:
        return {
            "action": RevisionAction.MANUAL_REVIEW,
            "reason": f"이격거리 위반 (요구: {violation.get('required_value')}) — 이동 방향 수동 검토 필요",
        }

    @staticmethod
    def _update_attribute(violation: dict, current: dict) -> dict:
        return {
            "action": RevisionAction.UPDATE_ATTRIBUTE,
            "reason": violation.get("reason", "속성값 변경 필요"),
            "new_value": violation.get("required_value"),
            "equipment_position": current,
        }
