"""
File    : backend/services/agents/fire/sub/review/revision.py
Author  : 김민정
Create  : 2026-04-15
Description : 위반 사항에 대해 구체적인 수정 대안을 제시합니다.

Modification History:
    - 2026-04-15 (김민정) : 위반 사항 해소를 위한 수정 제안 로직 구현
    - 2026-04-18 (김지우) : llm_service 연동으로 리팩터링
    - 2026-04-23       : piping 방식과 동일하게 통일 (calculate_fix, 위반 타입 → 액션 매핑)
    - 2026-05-06 (양창일) : spacing_error → CREATE_BLOCK 자동 수정 (인접 설비 중간 지점 추가)
"""


# 소방 위반 타입 → 수정 액션 매핑
_FIRE_VIOLATION_ACTION_MAP: dict[str, str] = {
    "spacing_error":        "add_detector_midpoint",
    "coverage_error":       "add_extinguisher_coverage",
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
    def calculate_fix(
        self,
        violations: list,
        current_layout: dict,
        neighbor_map: dict | None = None,
        element_meta: dict | None = None,
    ) -> list:
        """
        위반 항목마다 수정 액션을 결정하고 반환합니다.
        current_layout : {equipment_id: {x, y, z, ...}} 형태
        neighbor_map   : {equipment_id: nearest_equipment_id} — spacing_error 중간 지점 계산용
        element_meta   : {equipment_id: {block_name, layer, fire_category}} — CREATE_BLOCK 필드용
        """
        _neighbor = neighbor_map or {}
        _meta = element_meta or {}
        fixes = []
        for violation in violations:
            target_id = str(violation.get("equipment_id") or "")
            v_type = str(violation.get("violation_type") or "").lower()
            current_pos = current_layout.get(target_id, {})
            if v_type == "coverage_error":
                nearest_id  = violation.get("nearest_extinguisher_id", "")
                meta        = _meta.get(nearest_id, {})
            else:
                nearest_id  = _neighbor.get(target_id, "")
                meta        = _meta.get(target_id, {})
            nearest_pos = current_layout.get(nearest_id, {}) if nearest_id else {}
            fix_action = self._dispatch(
                v_type, violation, current_pos,
                nearest_pos=nearest_pos, meta=meta,
            )
            fixes.append({"equipment_id": target_id, "proposed_fix": fix_action})
        return fixes

    def _dispatch(
        self,
        v_type: str,
        violation: dict,
        current: dict,
        nearest_pos: dict | None = None,
        meta: dict | None = None,
    ) -> dict:
        action = _FIRE_VIOLATION_ACTION_MAP.get(v_type, "manual_review")

        if action == "add_detector_midpoint":
            return self._add_detector_midpoint(
                violation, current, nearest_pos or {}, meta or {}
            )

        if action == "add_extinguisher_coverage":
            return self._add_extinguisher_coverage_block(violation, meta or {})

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
    def _fmt_mm(value, default: str = "") -> str:
        """숫자 또는 문자열 값을 "Xmm" 포맷으로 안전하게 변환한다."""
        try:
            return f"{float(value):.0f}mm"
        except (TypeError, ValueError):
            return str(value or default)

    @staticmethod
    def _add_extinguisher_coverage_block(violation: dict, meta: dict) -> dict:
        # Guard 1: fire_category
        fire_category = str(
            violation.get("fire_category") or meta.get("fire_category") or ""
        )
        if fire_category != "extinguisher":
            return {
                "action": "manual_review",
                "reason": (
                    f"coverage_error 자동수정은 소화기만 지원합니다. "
                    f"fire_category={fire_category or 'unknown'}"
                ),
            }

        # Guard 2: nearest_extinguisher_id
        nearest_id = violation.get("nearest_extinguisher_id") or ""
        if not nearest_id:
            return {
                "action": "manual_review",
                "reason": "소화기 커버리지 공백 — nearest_extinguisher_id 없음, 수동 검토 필요",
            }

        # Guard 3: block_name
        block_name = meta.get("block_name") or ""
        if not block_name:
            return {
                "action": "manual_review",
                "reason": (
                    f"소화기 커버리지 공백 — 인접 소화기({nearest_id}) 블록 이름 없음, "
                    "자동 블록 추가 불가"
                ),
            }

        # Guard 4: layer
        layer = meta.get("layer") or ""
        if not layer:
            return {
                "action": "manual_review",
                "reason": (
                    f"소화기 커버리지 공백 — 인접 소화기({nearest_id}) 레이어 정보 없음, "
                    "자동 블록 추가 불가"
                ),
            }

        # Guard 5: coordinates
        x = violation.get("x")
        y = violation.get("y")
        if x is None or y is None:
            return {
                "action": "manual_review",
                "reason": "소화기 커버리지 공백 — 갭 좌표 없음, 수동 검토 필요",
            }

        # distance_mm / threshold_mm fallback
        dist_mm = (
            violation.get("distance_mm")
            if violation.get("distance_mm") is not None
            else violation.get("current_value", "")
        )
        thr_mm = (
            violation.get("threshold_mm")
            if violation.get("threshold_mm") is not None
            else violation.get("required_value", 20_000)
        )

        return {
            "type":              "CREATE_BLOCK",
            "action":            "CREATE_BLOCK",
            "new_block_name":    block_name,
            "base_x":            float(x),
            "base_y":            float(y),
            "new_layer":         layer,
            "modification_tier": 3,
            "fire_category":     "extinguisher",
            "reason": (
                f"소화기 커버리지 공백 해소 — ({float(x):.1f}, {float(y):.1f}) 지점에 "
                f"'{block_name}' 소화기 블록 추가 제안 "
                f"(현재: {RevisionAgent._fmt_mm(dist_mm)}, "
                f"기준: {RevisionAgent._fmt_mm(thr_mm)}). "
                "보행거리 20m 기준을 직선거리로 근사한 자동 수정. "
                f"[인접 소화기: {nearest_id}]"
            ),
            "current_value":  RevisionAgent._fmt_mm(dist_mm),
            "required_value": f"{RevisionAgent._fmt_mm(thr_mm)} 이하",
            "reference_rule": violation.get("reference_rule"),
        }

    @staticmethod
    def _add_detector_midpoint(
        violation: dict,
        current_pos: dict,
        nearest_pos: dict,
        meta: dict,
    ) -> dict:
        _meta = meta or {}
        _cv = violation.get("current_value")
        _rv = violation.get("required_value")

        fire_category = _meta.get("fire_category") or ""
        if fire_category != "detector":
            return {
                "action": "manual_review",
                "reason": (
                    f"이격 위반 (현재: {_cv}, 요구: {_rv}) — "
                    f"감지기(detector) 외 설비({fire_category or '미분류'})는 "
                    "자동 블록 추가 대상 아님, 수동 검토 필요"
                ),
            }

        block_name = _meta.get("block_name") or ""
        if not block_name:
            return {
                "action": "manual_review",
                "reason": (
                    f"이격 위반 (현재: {_cv}, 요구: {_rv}) — "
                    "블록 이름 없음, 자동 블록 추가 불가"
                ),
            }

        layer = _meta.get("layer") or ""
        if not layer:
            return {
                "action": "manual_review",
                "reason": (
                    f"이격 위반 (현재: {_cv}, 요구: {_rv}) — "
                    "레이어 정보 없음, 자동 블록 추가 불가"
                ),
            }

        tx = (current_pos or {}).get("x")
        ty = (current_pos or {}).get("y")
        nx = (nearest_pos or {}).get("x")
        ny = (nearest_pos or {}).get("y")
        if tx is None or ty is None or nx is None or ny is None:
            return {
                "action": "manual_review",
                "reason": (
                    f"이격 위반 (현재: {_cv}, 요구: {_rv}) — "
                    "인접 설비 좌표 없음, 수동 검토 필요"
                ),
            }

        mid_x = (float(tx) + float(nx)) / 2.0
        mid_y = (float(ty) + float(ny)) / 2.0
        return {
            "type": "CREATE_BLOCK",
            "action": "CREATE_BLOCK",
            "new_block_name": block_name,
            "base_x": mid_x,
            "base_y": mid_y,
            "new_layer": layer,
            "modification_tier": 3,
            "reason": (
                f"이격 위반 해소 — 인접 설비 중간 지점 ({mid_x:.1f}, {mid_y:.1f})에 "
                f"'{block_name}' 블록 추가 제안 "
                f"(현재: {_cv}, 기준: {_rv})"
            ),
            "current_value": _cv,
            "required_value": _rv,
            "reference_rule": violation.get("reference_rule"),
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
