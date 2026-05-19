"""
File    : backend/services/agents/electric/sub/review/revision.py
Author  : 김지우
Description : ComplianceAgent의 위반 리포트를 기반으로 전기 설비 수정 대안을 연산
"""

from backend.services.agents.elec.schemas import ElectricViolationType, RevisionAction
from backend.services.agents.elec.sub.geometry import calc_clearance_move_vector

_VIOLATION_ACTION_MAP: dict[str, str] = {
    ElectricViolationType.VOLTAGE_DROP_ERROR:        RevisionAction.CHANGE_CABLE_SIZE,
    ElectricViolationType.CABLE_AMPACITY_ERROR:      RevisionAction.CHANGE_CABLE_SIZE,
    ElectricViolationType.COLOR_MISMATCH_ERROR:      RevisionAction.CHANGE_COLOR,
    ElectricViolationType.CLEARANCE_DISTANCE_ERROR:  RevisionAction.MOVE_ENTITY,
    ElectricViolationType.BREAKER_CAPACITY_ERROR:    RevisionAction.CHANGE_BREAKER_CAPACITY,
    ElectricViolationType.CONDUIT_SIZE_ERROR:        RevisionAction.CHANGE_CONDUIT_SIZE,
    ElectricViolationType.GROUNDING_WIRE_ERROR:      RevisionAction.UPDATE_ATTRIBUTE,
    ElectricViolationType.DEVICE_NOT_CONNECTED:      RevisionAction.CONNECT_DEVICE,
    ElectricViolationType.WRONG_LAYER:               RevisionAction.FIX_LAYER,
    ElectricViolationType.DUPLICATE_SYMBOL:          RevisionAction.CLEANUP_DUPLICATE,
}

_MIN_CONFIDENCE_FOR_AUTO_FIX = 0.7

class RevisionAgent:
    def calculate_fix(self, violations: list, current_layout: dict) -> list:
        fixes = []
        for violation in violations:
            target_id = violation.get("equipment_id") or violation.get("object_id") or violation.get("handle")
            v_type = violation.get("violation_type")
            current = current_layout.get(target_id, {})
            confidence = violation.get("confidence_score", 1.0)
            if isinstance(confidence, (int, float)) and confidence < _MIN_CONFIDENCE_FOR_AUTO_FIX:
                fix_action = {
                    "action": RevisionAction.MANUAL_REVIEW,
                    "reason": (
                        f"신뢰도 낮음 (confidence={confidence:.2f} < {_MIN_CONFIDENCE_FOR_AUTO_FIX}) — "
                        f"자동 수정 보류. 원인: {violation.get('confidence_reason', '-')}"
                    ),
                }
            else:
                fix_action = self._dispatch(v_type, violation, current)
            row = {
                "equipment_id": target_id,
                "violation_type": v_type,
                "confidence_score": violation.get("confidence_score"),
                "confidence_reason": violation.get("confidence_reason"),
                "proposed_fix": fix_action,
            }
            for key in ("affected_handles", "bbox", "target_bbox", "terminal_candidate_id"):
                if key in violation:
                    row[key] = violation[key]
            fixes.append(row)
        return fixes

    def _dispatch(self, v_type: str, violation: dict, current: dict) -> dict:
        # 직접 조회 → str,Enum이므로 소문자 값이면 일치. LLM이 대/소문자를 섞을 경우를 위해 lower 폴백
        if str(v_type or "").startswith("terminal_") or violation.get("category") == "geometry_qa":
            return self._geometry_qa_review(violation)
        if str(v_type or "") in {"open_circuit_error", "wire_disconnected"}:
            return self._topology_reconnect_review(violation)
        if str(v_type or "") == "wire_overlap":
            return self._wire_overlap_review(violation)
        if str(v_type or "") == "grounding_rod_count_mismatch":
            return self._fix_grounding_e_tag(violation)
        if str(v_type or "") == "grounding_rod_spacing_violation":
            return {
                "action": RevisionAction.MANUAL_REVIEW,
                "description": (
                    f"접지봉 이격거리 부족 — 현재 {violation.get('current_value', '?')},"
                    f" 기준 {violation.get('required_value', '?')} (KEC 140.6). "
                    "접지봉을 재배치하거나 도면 설계를 수정하십시오."
                ),
            }
        if str(v_type or "") == "outlet_height_violation":
            cur = violation.get("current_value", "?")
            req = violation.get("required_value", "?")
            return {
                "action": RevisionAction.MANUAL_REVIEW,
                "description": (
                    f"콘센트 설치 높이 미달 — 현재 {cur}, 기준 {req} (KEC 232.56). "
                    "MH 표기 텍스트 수정 후 실물 재설치가 필요합니다."
                ),
            }
        if str(v_type or "") == "wire_count_violation":
            cur = violation.get("current_value", "?")
            req = violation.get("required_value", "?")
            return {
                "action": RevisionAction.MANUAL_REVIEW,
                "description": (
                    f"배선 가닥 수 표기 오류 — 현재 {cur}, 기준 {req} (KEC 232). "
                    "가닥 수 표기 텍스트를 올바른 값으로 수정하십시오."
                ),
            }

        action = _VIOLATION_ACTION_MAP.get(v_type)
        if action is None:
            normalized = str(v_type or "").lower().strip()
            action = next(
                (val for key, val in _VIOLATION_ACTION_MAP.items() if str(key).lower() == normalized),
                RevisionAction.MANUAL_REVIEW,
            )

        handlers = {
            RevisionAction.CHANGE_CABLE_SIZE:       self._change_cable_size,
            RevisionAction.CHANGE_COLOR:            self._change_color,
            RevisionAction.CHANGE_BREAKER_CAPACITY: self._change_breaker_capacity,
            RevisionAction.CHANGE_CONDUIT_SIZE:     self._change_conduit_size,
            RevisionAction.MOVE_ENTITY:             self._move_entity,
            RevisionAction.UPDATE_ATTRIBUTE:        self._update_attribute,
            RevisionAction.CONNECT_DEVICE:          self._connect_device,
            RevisionAction.FIX_LAYER:               self._fix_layer,
            RevisionAction.CLEANUP_DUPLICATE:       self._cleanup_duplicate,
        }
        handler = handlers.get(action)
        if handler:
            return handler(violation, current)
        return {"action": RevisionAction.MANUAL_REVIEW, "reason": "자동 수정 범위를 벗어남"}

    @staticmethod
    def _topology_reconnect_review(violation: dict) -> dict:
        midpoint = violation.get("midpoint") or violation.get("reconnect_point")
        bridge = violation.get("bridge_segment")
        snap = violation.get("snap_vector")
        handles = [
            str(h) for h in (
                violation.get("target_handles")
                or [violation.get("object_id"), violation.get("handle_b")]
            )
            if h
        ]
        if bridge or midpoint or snap:
            is_outlet = "콘센트" in str(violation.get("reason") or "")
            return {
                "action": RevisionAction.MOVE_ENTITY,
                "reason": (
                    "끊어진 콘센트 연결선을 다시 이어 줄 수 있습니다."
                    if is_outlet
                    else "끊어진 전기 연결선을 다시 이어 줄 수 있습니다."
                ),
                "target_handles": handles,
                "reconnect_point": midpoint,
                "snap_vector": snap,
                "bridge_segment": bridge,
                "auto_fix": {
                    "type": "BRIDGE_WIRE" if bridge or midpoint else "SNAP_ENDPOINT",
                    "target_handles": handles,
                    "reconnect_point": midpoint,
                    "snap_vector": snap,
                    "bridge_segment": bridge,
                },
            }
        return {
            "action": RevisionAction.MANUAL_REVIEW,
            "reason": "연결 대상이 불명확하여 수동 검토가 필요합니다.",
        }

    @staticmethod
    def _wire_overlap_review(violation: dict) -> dict:
        handles = [str(h) for h in (violation.get("target_handles") or violation.get("affected_handles") or []) if h]
        if violation.get("duplicate") and handles:
            return {
                "action": RevisionAction.CLEANUP_DUPLICATE,
                "reason": "완전히 겹친 중복 전선 선분을 정리할 수 있습니다.",
                "target_handles": handles,
                "auto_fix": {
                    "type": "DELETE_DUPLICATE_WIRE",
                    "target_handles": handles[1:],
                    "keep_handle": handles[0],
                },
            }
        return {
            "action": RevisionAction.MANUAL_REVIEW,
            "reason": "겹친 전선 경로가 병렬 배선인지 중복 작성인지 수동 확인이 필요합니다.",
            "target_handles": handles,
            "overlap_segment": violation.get("overlap_segment"),
            "modification_tier": "manual_review",
        }

    @staticmethod
    def _geometry_qa_review(violation: dict) -> dict:
        bbox = violation.get("bbox") or violation.get("target_bbox")
        handles = list(violation.get("target_handles") or violation.get("affected_handles") or [])

        # --- batch MOVE: multiple circles each have their own actual/expected center ---
        move_instructions = violation.get("move_instructions") or []
        if move_instructions:
            auto_fix_list = []
            for instr in move_instructions:
                try:
                    dx = round(float(instr["expected_center"]["x"]) - float(instr["actual_center"]["x"]), 4)
                    dy = round(float(instr["expected_center"]["y"]) - float(instr["actual_center"]["y"]), 4)
                    h = instr.get("handle")
                    auto_fix_list.append({
                        "type": "MOVE",
                        "target_handles": [h] if h else handles,
                        "delta_x": dx,
                        "delta_y": dy,
                    })
                except (TypeError, ValueError, KeyError):
                    continue
            if auto_fix_list:
                primary = auto_fix_list[0] if len(auto_fix_list) == 1 else {
                    "type": "MOVE_BATCH",
                    "moves": auto_fix_list,
                }
                return {
                    "action": RevisionAction.MOVE_ENTITY,
                    "target_handles": handles,
                    "reason": "원형 단자를 기준 단자 배열 위치로 복원합니다.",
                    "auto_fix": primary,
                    "auto_fix_list": auto_fix_list,
                    "_entity_bbox": bbox,
                    "target_bbox": violation.get("target_bbox") or bbox,
                }

        # --- single MOVE: outlier circle with pre-computed actual/expected ---
        actual = violation.get("actual_center")
        expected = violation.get("expected_center")
        if actual and expected and handles:
            try:
                dx = round(float(expected["x"]) - float(actual["x"]), 4)
                dy = round(float(expected["y"]) - float(actual["y"]), 4)
                return {
                    "action": RevisionAction.MOVE_ENTITY,
                    "target_handles": handles,
                    "delta_x": dx,
                    "delta_y": dy,
                    "reason": f"원형 단자를 기준 단자 배열 위치로 복원합니다. (dx={dx}, dy={dy})",
                    "auto_fix": {
                        "type": "MOVE",
                        "target_handles": handles,
                        "delta_x": dx,
                        "delta_y": dy,
                    },
                    "_entity_bbox": bbox,
                    "target_bbox": violation.get("target_bbox") or bbox,
                }
            except (TypeError, ValueError, KeyError):
                pass

        # --- orphan circle: no expected position exists → delete the stray circle ---
        if str(violation.get("violation_type") or "") == "terminal_orphan_circle" and handles:
            return {
                "action": RevisionAction.CLEANUP_DUPLICATE,
                "type": "DELETE",
                "target_handles": handles,
                "reason": "클러스터에 속하지 않는 단자 원형을 제거합니다.",
                "_entity_bbox": bbox,
                "target_bbox": violation.get("target_bbox") or bbox,
            }

        # --- fallback: no coordinate info available ---
        return {
            "action": RevisionAction.MANUAL_REVIEW,
            "reason": violation.get("suggestion") or violation.get("reason") or "상세도 geometry를 수동 검토하십시오.",
            "_entity_bbox": bbox,
            "target_bbox": violation.get("target_bbox") or bbox,
            "related_handles": handles,
            "target_handles": handles,
            "symbol_cluster_handles": handles,
            "modification_tier": "manual_review",
        }

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
        # geometry 데이터가 충분하면 이동 벡터를 자동 계산한다
        # 다양한 키명을 순서대로 시도
        _SEP_KEYS     = ("separation_drawing", "current_sep_mm", "distance", "actual_distance", "current_value")
        _MIN_SEP_KEYS = ("min_clearance", "required_clearance", "required_sep_mm", "required_value")

        def _get_first(d: dict, keys: tuple):
            for k in keys:
                v = d.get(k)
                if v is not None:
                    return v
            return None

        current_sep  = _get_first(violation, _SEP_KEYS)
        required_sep = _get_first(violation, _MIN_SEP_KEYS)
        target_bbox = violation.get("target_bbox")
        ref_bbox = violation.get("ref_bbox")

        try:
            current_sep_f  = float(current_sep)
            required_sep_f = float(required_sep)
        except (TypeError, ValueError):
            current_sep_f = required_sep_f = None

        if (
            target_bbox and ref_bbox
            and current_sep_f is not None
            and required_sep_f is not None
            and required_sep_f > current_sep_f
        ):
            def _to_tuple(b):
                if isinstance(b, dict):
                    if "x1" in b:
                        return (float(b["x1"]), float(b["y1"]), float(b["x2"]), float(b["y2"]))
                    if "min_x" in b:
                        return (float(b["min_x"]), float(b["min_y"]), float(b["max_x"]), float(b["max_y"]))
                return None

            t_tup = _to_tuple(target_bbox)
            r_tup = _to_tuple(ref_bbox)
            if t_tup and r_tup:
                dx, dy = calc_clearance_move_vector(t_tup, r_tup, current_sep_f, required_sep_f)
                return {
                    "action":   RevisionAction.MOVE_ENTITY,
                    "delta_x":  dx,
                    "delta_y":  dy,
                    "reason":   f"이격거리 자동 보정 (현재 {current_sep_f:.0f}mm → 목표 {required_sep_f:.0f}mm)",
                    "auto_fix": {"type": "MOVE", "delta_x": dx, "delta_y": dy},
                }

        return {
            "action": RevisionAction.MANUAL_REVIEW,
            "reason": f"이격거리 위반 (요구: {required_sep}) — geometry 정보 부족, 수동 검토 필요",
        }

    @staticmethod
    def _update_attribute(violation: dict, current: dict) -> dict:
        return {
            "action": RevisionAction.UPDATE_ATTRIBUTE,
            "reason": violation.get("reason", "속성값 변경 필요"),
            "new_value": violation.get("required_value"),
            "equipment_position": current,
        }

    @staticmethod
    def _connect_device(violation: dict, current: dict) -> dict:
        return {
            "action":        RevisionAction.CONNECT_DEVICE,
            "device_handle": violation.get("object_id"),
            "panel_handle":  violation.get("panel_handle"),
            "circuit":       violation.get("circuit") or violation.get("required_value"),
            "reason":        violation.get("reason", "기기 미연결 — 회로 연결 필요"),
            "auto_fix": {
                "type":          "connect_device",
                "device_handle": violation.get("object_id"),
                "panel_handle":  violation.get("panel_handle"),
                "circuit":       violation.get("circuit") or violation.get("required_value"),
            },
        }

    @staticmethod
    def _fix_layer(violation: dict, current: dict) -> dict:
        return {
            "action":         RevisionAction.FIX_LAYER,
            "current_layer":  violation.get("current_value"),
            "standard_layer": violation.get("required_value"),
            "reason":         violation.get("reason", "비표준 레이어 — 표준 레이어로 변경 필요"),
            "auto_fix": {
                "type":      "LAYER",
                "new_layer": violation.get("required_value") or "",
            },
        }

    @staticmethod
    def _cleanup_duplicate(violation: dict, current: dict) -> dict:
        dup_handles = violation.get("duplicate_handles") or []
        keep   = dup_handles[0] if dup_handles else violation.get("object_id")
        remove = dup_handles[1] if len(dup_handles) > 1 else None
        return {
            "action":        RevisionAction.CLEANUP_DUPLICATE,
            "keep_handle":   keep,
            "remove_handle": remove,
            "reason":        violation.get("reason", "중복 심볼 감지 — 하나 제거 필요"),
            "auto_fix": {
                "type":          "cleanup_duplicate",
                "keep_handle":   keep,
                "remove_handle": remove,
            },
        }

    @staticmethod
    def _fix_grounding_e_tag(violation: dict) -> dict:
        e_tag_handle = violation.get("e_tag_handle")
        e_tag_new    = violation.get("e_tag_new")
        e_tag_old    = violation.get("e_tag_old")
        if e_tag_handle and e_tag_new:
            return {
                "action": RevisionAction.UPDATE_ATTRIBUTE,
                "reason": f"E 태그를 실제 접지봉 개수에 맞게 수정합니다 ({e_tag_old} → {e_tag_new})",
                "target_handles": [e_tag_handle],
                "new_value": e_tag_new,
                "auto_fix": {
                    "type":           "TEXT_CONTENT",
                    "target_handles": [e_tag_handle],
                    "new_text":       e_tag_new,
                },
            }
        return {
            "action": RevisionAction.MANUAL_REVIEW,
            "reason": "E 태그 핸들 정보 없음 — E 태그 번호를 수동으로 수정하십시오.",
        }
