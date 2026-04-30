"""
File    : backend/services/agents/piping/sub/review/revision.py
Author  : 송주엽
Create  : 2026-04-09
Description : ComplianceAgent의 위반 리포트를 기반으로 배관 설비 수정 대안을 연산
              위반 타입 → 수정 액션 매핑 기준으로 결과를 반환합니다.

Modification History :
    - 2026-04-09 (송주엽) : 위반 사항 기반 대안/수정 액션 계산 로직 초안 작성
    - 2026-04-29 (송주엽) : MOVE_ENTITY 이동량 자동 계산 (separation_mm 기반),
                            confidence_score < 0.7 위반은 MANUAL_REVIEW 강제,
                            _parse_mm() 유틸 추가
    - 2026-04-29 (송주엽) : position/_source 전달, _adjust_slope 분수기울기 파싱,
                            _change_size 추천 관경 역산, _add_insulation 온도 필드
"""
from __future__ import annotations

import re
import math

from backend.services.agents.pipe.schemas import PipingViolationType, RevisionAction


# 위반 타입 → 1차 수정 액션 매핑
_VIOLATION_ACTION_MAP: dict[str, str] = {
    PipingViolationType.DISTANCE_ERROR:             RevisionAction.MOVE_ENTITY,
    PipingViolationType.HANGER_SPACING_ERROR:       RevisionAction.ADD_HANGER,
    PipingViolationType.SLOPE_ERROR:                RevisionAction.ADJUST_SLOPE,
    PipingViolationType.VALVE_POSITION_ERROR:       RevisionAction.MOVE_ENTITY,
    PipingViolationType.PRESSURE_OVERLOAD:          RevisionAction.REDUCE_PRESSURE,
    PipingViolationType.PIPE_SIZE_MISMATCH:         RevisionAction.CHANGE_SIZE,
    PipingViolationType.INSULATION_THICKNESS_ERROR: RevisionAction.ADD_INSULATION,
    PipingViolationType.FIRE_PENETRATION_ERROR:     RevisionAction.ADD_FIRE_SEAL,
    PipingViolationType.MATERIAL_MISMATCH:          RevisionAction.REPLACE_MATERIAL,
    PipingViolationType.SEISMIC_SUPPORT_ERROR:      RevisionAction.ADD_SEISMIC_HANGER,
    PipingViolationType.EXPANSION_JOINT_MISSING:    RevisionAction.ADD_EXPANSION_JOINT,
}

# confidence 임계값 — 이하이면 MANUAL_REVIEW 강제
_MIN_CONFIDENCE_FOR_AUTO_FIX = 0.7
# mm 수치 추출 정규식
_MM_RE = re.compile(r"([\d]+(?:\.[\d]+)?)\s*mm", re.IGNORECASE)
_NUM_RE = re.compile(r"([\d]+(?:\.[\d]+)?)")


def _parse_mm(text: str | None) -> float | None:
    """텍스트에서 mm 수치를 추출한다. 없으면 None."""
    if not text:
        return None
    m = _MM_RE.search(str(text))
    if m:
        return float(m.group(1))
    m = _NUM_RE.search(str(text))
    if m:
        return float(m.group(1))
    return None


class RevisionAgent:
    def calculate_fix(self, violations: list, current_layout: dict) -> list:
        """
        위반 항목마다 수정 액션을 결정하고 좌표·파라미터를 반환합니다.
        current_layout: {equipment_id: {x, y, z, ...}} 형태

        Phase 6: confidence_score < 0.7인 위반은 자동 수정을 시도하지 않고
                 MANUAL_REVIEW로 처리하여 오수정(false-positive fix) 방지.
        """
        fixes = []
        for violation in violations:
            target_id = violation.get("equipment_id")
            v_type    = violation.get("violation_type")
            current   = current_layout.get(target_id, {})

            # 위반 position 정보 → current에 병합 (RevCloud/UI 좌표 연동)
            pos = violation.get("position")
            if pos and not current:
                current = pos

            # ── Phase 6: 낮은 신뢰도 위반 수동 검토 강제 ───────────────────
            confidence = violation.get("confidence_score", 1.0)
            if isinstance(confidence, (int, float)) and confidence < _MIN_CONFIDENCE_FOR_AUTO_FIX:
                fixes.append({
                    "equipment_id": target_id,
                    "_source":       violation.get("_source", "llm"),
                    "position":      pos,
                    "proposed_fix": {
                        "action": RevisionAction.MANUAL_REVIEW,
                        "reason": (
                            f"신뢰도 낮음 (confidence={confidence:.2f} < {_MIN_CONFIDENCE_FOR_AUTO_FIX}) "
                            f"— 자동 수정 보류. 원인: {violation.get('confidence_reason', '-')}"
                        ),
                    },
                })
                continue

            fix_action = self._dispatch(v_type, violation, current)
            fixes.append({
                "equipment_id": target_id,
                "_source":       violation.get("_source", "llm"),
                "position":      pos,
                "proposed_fix":  fix_action,
            })
        return fixes

    def _dispatch(self, v_type: str, violation: dict, current: dict) -> dict:
        action = _VIOLATION_ACTION_MAP.get(v_type, RevisionAction.MANUAL_REVIEW)

        handlers = {
            RevisionAction.MOVE_ENTITY:         self._move_entity,
            RevisionAction.ADJUST_SLOPE:        self._adjust_slope,
            RevisionAction.ADD_HANGER:          self._add_hanger,
            RevisionAction.CHANGE_SIZE:         self._change_size,
            RevisionAction.REDUCE_PRESSURE:     self._reduce_pressure,
            RevisionAction.ADD_INSULATION:      self._add_insulation,
            RevisionAction.ADD_FIRE_SEAL:       self._add_fire_seal,
            RevisionAction.REPLACE_MATERIAL:    self._replace_material,
            RevisionAction.ADD_SEISMIC_HANGER:  self._add_seismic_hanger,
            RevisionAction.ADD_EXPANSION_JOINT: self._add_expansion_joint,
        }
        handler = handlers.get(action)
        if handler:
            return handler(violation, current)
        return {"action": RevisionAction.MANUAL_REVIEW, "reason": "자동 수정 범위를 벗어남"}

    @staticmethod
    def _move_entity(violation: dict, current: dict) -> dict:
        """
        Phase 2 개선: separation_mm 기반으로 필요 이동량(displacement_mm)을 계산.
        이동 방향은 도면 컨텍스트 없이 판단 불가 → 크기만 제공하고 방향은 수동 확인.
        """
        cur_mm  = _parse_mm(violation.get("current_value"))
        req_mm  = _parse_mm(violation.get("required_value"))

        # 이동량 = 필요 이격 - 현재 이격 (양수이면 벌려야 함)
        displacement_mm: float | None = None
        if cur_mm is not None and req_mm is not None and req_mm > cur_mm:
            displacement_mm = round(req_mm - cur_mm, 1)

        result: dict = {
            "action":              RevisionAction.MOVE_ENTITY,
            "anchor_position":     current,
            "current_value_mm":    cur_mm,
            "required_value_mm":   req_mm,
        }
        if displacement_mm is not None:
            result["displacement_mm"] = displacement_mm
            result["note"] = (
                f"최소 {displacement_mm}mm 이상 인접 설비로부터 멀리 이동 필요. "
                "이동 방향은 도면에서 확인하십시오."
            )
        else:
            result["action"] = RevisionAction.MANUAL_REVIEW
            result["reason"] = (
                f"이격거리 위반 (현재: {violation.get('current_value')}, "
                f"요구: {violation.get('required_value')}) — 이동량 산출 불가, 수동 검토 필요"
            )
        return result

    @staticmethod
    def _adjust_slope(violation: dict, current: dict) -> dict:
        """기울기 미표기/기울기 부족: 분수(1/50) 및 퍼센트(2%) 양쪽 파싱."""
        try:
            req_str = str(violation.get("required_value", "0")).replace("%", "").strip()
            if "/" in req_str:
                parts = req_str.split("/")
                req = round(float(parts[0]) / float(parts[1]) * 100, 3)
            else:
                req = float(req_str) if req_str else 0.0
            cur_str = str(violation.get("current_value", "0")).replace("%", "").strip()
            m_cur = _NUM_RE.search(cur_str)
            cur = float(m_cur.group(1)) if m_cur else 0.0
            return {
                "action":             RevisionAction.ADJUST_SLOPE,
                "target_slope_pct":   req,
                "current_slope_pct":  cur,
                "anchor_position":    current,
                "note": f"기울기를 {cur:.3f}% → {req:.3f}%로 조정 필요",
            }
        except Exception:
            return {"action": RevisionAction.MANUAL_REVIEW, "reason": "기울기 수치 파싱 실패"}

    @staticmethod
    def _add_hanger(violation: dict, current: dict) -> dict:
        try:
            req = _parse_mm(violation.get("required_value")) or 2000.0
            cur = _parse_mm(violation.get("current_value"))
            count: int = 1
            if cur is not None and cur > req:
                # 현재 구간 길이 / 기준 간격 → 필요 행거 수
                count = max(1, math.ceil(cur / req) - 1)
            return {
                "action": RevisionAction.ADD_HANGER,
                "add_count": count,
                "reference_spacing_mm": req,
                "anchor_position": current,
                "note": f"기준 간격({req:.0f}mm)마다 행거 1개 추가 필요 (추정 {count}개)",
            }
        except Exception:
            return {"action": RevisionAction.ADD_HANGER, "add_count": 1, "anchor_position": current}

    @staticmethod
    def _change_size(violation: dict, current: dict) -> dict:
        """pipe_size_mismatch: 유량·유속에서 추천 관경 역산."""
        req    = violation.get("required_value", "")
        cur    = violation.get("current_value", "")
        reason = violation.get("reason", "")
        recommended_dn: float | None = None

        # 유량 파싱: "1.5m³/h" or "Q=1.5m³/h"
        q_m = re.search(r"([\\d.]+)\s*m³/h", reason)
        # 한계 유속 파싱: "한계 3.0m/s"
        v_m = re.search(r"한계\s*([\d.]+)\s*m/s", reason)
        if q_m and v_m:
            try:
                q     = float(q_m.group(1))
                v_max = float(v_m.group(1))
                area  = (q / 3600.0) / v_max
                recommended_dn = round(math.sqrt(area / math.pi) * 2000.0)
            except Exception:
                pass

        result: dict = {
            "action":             RevisionAction.CHANGE_SIZE,
            "required_size":      req,
            "current_size":       cur,
            "equipment_position": current,
        }
        if recommended_dn:
            result["recommended_dn_mm"] = recommended_dn
            result["note"] = f"DN{int(recommended_dn)} 이상으로 관경 교체 필요 (유량·유속 기반 역산)"
        return result

    @staticmethod
    def _reduce_pressure(violation: dict, current: dict) -> dict:
        return {
            "action": RevisionAction.REDUCE_PRESSURE,
            "reason": f"압력 초과: {violation.get('current_value')} → {violation.get('required_value')} 이하 필요",
        }

    @staticmethod
    def _add_insulation(violation: dict, current: dict) -> dict:
        """고온 배관 보온재: 온도 수치를 파싱하여 fluid_temp_c 필드로 전달."""
        temp_raw = str(violation.get("current_value", ""))
        temp_c: float | None = None
        m = re.search(r"([\d]+(?:\.[\d]*)?)\s*[℃C도]", temp_raw)
        if m:
            try:
                temp_c = float(m.group(1))
            except Exception:
                pass
        result: dict = {
            "action":             RevisionAction.ADD_INSULATION,
            "required_thickness": violation.get("required_value"),
            "current_thickness":  temp_raw,
            "equipment_position": current,
        }
        if temp_c is not None:
            result["fluid_temp_c"] = temp_c
            result["note"] = (
                f"유체 온도 {temp_c:.0f}℃ — KDS 31 60 05 기준 보온재 시공 필요."
            )
        return result

    @staticmethod
    def _add_fire_seal(violation: dict, current: dict) -> dict:
        return {
            "action": RevisionAction.ADD_FIRE_SEAL,
            "reason": violation.get("reason", "관통부 방화충전 누락"),
            "equipment_position": current,
        }

    @staticmethod
    def _replace_material(violation: dict, current: dict) -> dict:
        return {
            "action": RevisionAction.REPLACE_MATERIAL,
            "required_material": violation.get("required_value"),
            "current_material":  violation.get("current_value"),
            "equipment_position": current,
        }

    @staticmethod
    def _add_seismic_hanger(violation: dict, current: dict) -> dict:
        return {
            "action": RevisionAction.ADD_SEISMIC_HANGER,
            "reason": violation.get("reason", "내진 지지 누락"),
            "anchor_position": current,
        }

    @staticmethod
    def _add_expansion_joint(violation: dict, current: dict) -> dict:
        return {
            "action": RevisionAction.ADD_EXPANSION_JOINT,
            "reason": violation.get("reason", "신축이음 누락"),
            "equipment_position": current,
        }
