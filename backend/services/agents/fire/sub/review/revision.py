"""
File    : backend/services/agents/fire/sub/review/revision.py
Author  : 김민정
Create  : 2026-04-15
Description : 위반 사항에 대해 구체적인 수정 대안을 제시합니다.

Modification History:
    - 2026-04-15 (김민정) : 위반 사항 해소를 위한 수정 제안 로직 구현
    - 2026-04-18 (김지우) : llm_service 연동으로 리팩터링
    - 2026-04-23       : piping 방식과 동일하게 통일 (calculate_fix, 위반 타입 → 액션 매핑)
"""


# 소방 위반 타입 → 수정 액션 매핑
_FIRE_VIOLATION_ACTION_MAP: dict[str, str] = {
    "spacing_error":        "move_entity",
    "radius_error":         "move_entity",
    "height_error":         "move_entity",
    "quantity_error":       "add_equipment",
    "pressure_error":       "manual_review",
    "pipe_size_error":      "change_size",
    "installation_missing": "add_equipment",
    "access_blocked":       "move_entity",
    "material_error":       "replace_material",
    "other_violation":      "manual_review",
    "noise_entity":         "delete_entity",
}


class RevisionAgent:
    def calculate_fix(self, violations: list, current_layout: dict) -> list:
        """
        위반 항목마다 수정 액션을 결정하고 반환합니다.
        current_layout: {equipment_id: {x, y, z, ...}} 형태
        """
        fixes = []
        for violation in violations:
            target_id = violation.get("equipment_id")
            v_type = str(violation.get("violation_type") or "").lower()
            current = current_layout.get(target_id, {})
            fix_action = self._dispatch(v_type, violation, current)
            fixes.append({"equipment_id": target_id, "proposed_fix": fix_action})
        return fixes

    def _dispatch(self, v_type: str, violation: dict, current: dict) -> dict:
        action = _FIRE_VIOLATION_ACTION_MAP.get(v_type, "manual_review")

        handlers = {
            "move_entity":      self._move_entity,
            "add_equipment":    self._add_equipment,
            "change_size":      self._change_size,
            "replace_material": self._replace_material,
            "delete_entity":    self._delete_entity,
        }
        handler = handlers.get(action)
        if handler:
            return handler(violation, current)
        return {
            "action": "manual_review",
            "reason": f"위반 유형 '{v_type}' — 자동 수정 범위를 벗어남, 수동 검토 필요"
        }

    @staticmethod
    def _move_entity(violation: dict, current: dict) -> dict:
        return {
            "action": "manual_review",
            "reason": (
                f"이격/반경 위반 (현재: {violation.get('current_value')}, "
                f"요구: {violation.get('required_value')}) — 이동 방향 판단 불가, 수동 검토 필요"
            ),
        }

    @staticmethod
    def _add_equipment(violation: dict, current: dict) -> dict:
        return {
            "action": "add_equipment",
            "reason": violation.get("reason", "소방 설비 추가 설치 필요"),
            "reference_rule": violation.get("reference_rule"),
            "required_value": violation.get("required_value"),
            "anchor_position": current,
        }

    @staticmethod
    def _change_size(violation: dict, current: dict) -> dict:
        return {
            "action": "change_size",
            "required_size": violation.get("required_value"),
            "current_size": violation.get("current_value"),
            "equipment_position": current,
        }

    @staticmethod
    def _replace_material(violation: dict, current: dict) -> dict:
        return {
            "action": "replace_material",
            "required_material": violation.get("required_value"),
            "current_material": violation.get("current_value"),
            "equipment_position": current,
        }

    @staticmethod
    def _delete_entity(violation: dict, current: dict) -> dict:
        return {
            "action": "DELETE",
            "reason": violation.get("reason", "낙서/노이즈 엔티티 삭제"),
        }
