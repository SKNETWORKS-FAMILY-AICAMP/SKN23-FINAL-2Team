"""
File    : backend/services/agents/pipe/sub/deterministic_checker.py
Author  : 송주엽
Create  : 2026-04-29
Description :
  LLM에 의존하지 않는 확정적(코드 기반) 배관 위반 검출기.

  ★ 핵심 원칙: "없음"을 근거로 위반을 보고하지 않는다.
     반드시 "X가 있고, 그 값이 규정 기준 Y를 초과/위반한다"는 양성 증거 형태만 보고.

  검출 항목 (양성 증거 기반):
    1. 단선 배관 — 양 끝점이 아무 LINE과도 연결되지 않은 고립 세그먼트
    2. MEP 설비 겹침 — mep_clearances.overlapping=True (두 블록이 같은 좌표 점유)
    3. 블록 위치 중복 — 두 블록의 insert_point가 1mm 이내 (동일 위치 중복 배치)
"""
from __future__ import annotations

import logging
import math
import re
from typing import Any

_log = logging.getLogger(__name__)

# 행거 간격 기본 규정 (mm) — 확정 검출에는 사용하지 않고, 명시 데이터가 있을 때만 참조
_DEFAULT_HANGER_SPACING_MM = 2000.0
# 블록 위치 중복 판정 허용 오차 (mm)
_DUP_POSITION_TOL_MM = 1.0
# 보온재 의무 온도 임계 (℃)
_INSULATION_TEMP_C = 60.0
# 유속 한도 (m/s)
_GAS_VEL_MAX   = 10.0
_WATER_VEL_MAX =  3.0
# 배관 방향 직교 허용 오차 (±5°)
_SLOPE_ORTH_TOL_DEG = 5.0
_CONTINUITY_MIN_LENGTH_MM = 500.0

# ── 도메인 판별용 ACI 색상 인덱스 ───────────────────────────────────────────
_ACI_ARCH_COLORS: frozenset[int] = frozenset({8, 9, 250, 251, 252, 253, 254, 255})
_ACI_PIPE_COLORS: frozenset[int] = frozenset({4, 154, 170, 30})
_ACI_FIRE_COLORS: frozenset[int] = frozenset({1, 10, 11})
_ARCH_LAYER_RE = re.compile(r"^A[-_]|^AR[-_]|ARCH|^0$|DEFPOINTS", re.IGNORECASE)
_HIDDEN_LINETYPE_RE = re.compile(r"HIDDEN|DASHDOT|PHANTOM|CENTER", re.IGNORECASE)
_PIPE_LAYER_STRONG_RE = re.compile(
    r"GAS|PIPE|PIPING|배관|급수|급탕|배수|위생|소화|SPRINK|CWS|HWS|FIRE|^P[-_]|^M[-_]",
    re.IGNORECASE,
)


def _is_arch_element(el: dict) -> bool:
    """색상/레이어/역할 기준으로 건축 요소 여부 판별. 건축 요소는 배관 위반 대상에서 제외."""
    # 1. 명시적 역할(layer_role) 확인 (DB 매핑 결과 존중)
    role = str(el.get("layer_role") or "").lower()
    if role in {"arch", "aux"}:
        return True

    # 2. 색상 기반 (ACI 8, 9, 250-255는 통상 건축 배경색)
    color = el.get("color")
    try:
        if int(color) in _ACI_ARCH_COLORS:
            return True
    except (TypeError, ValueError):
        pass

    # 3. 레이어명 패턴 기반
    layer = str(el.get("layer") or "")
    return bool(_ARCH_LAYER_RE.search(layer))


def _is_hidden_pipe(el: dict) -> bool:
    """HIDDEN 선종류 = 매립·숨김 배관. 단선 신뢰도를 낮춰야 함."""
    lt = str(el.get("linetype") or "")
    return bool(_HIDDEN_LINETYPE_RE.search(lt))


def _has_explicit_pipe_attrs(el: dict) -> bool:
    attrs = el.get("attributes") or {}
    return bool(
        el.get("diameter_mm")
        or el.get("pressure_mpa")
        or el.get("slope_pct")
        or str(el.get("material") or "").upper() not in ("", "UNKNOWN", "NONE")
        or attrs.get("SIZE")
        or attrs.get("DIAMETER")
        or attrs.get("MATERIAL")
        or attrs.get("TAG_NAME")
        or attrs.get("PRESSURE")
        or attrs.get("SLOPE")
    )


def _has_strong_continuity_evidence(el: dict, run: dict) -> bool:
    """연속성 위반은 실제 배관임이 강한 경우에만 보고한다."""
    if _has_explicit_pipe_attrs(el):
        return True
    layer = str(el.get("layer") or "")
    if _PIPE_LAYER_STRONG_RE.search(layer):
        return True
    material = str(run.get("material") or "").upper()
    if material not in ("", "UNKNOWN", "NONE") or float(run.get("diameter_mm") or 0) > 0:
        return True
    if (
        bool(el.get("flag_for_piping_agent"))
        and str(el.get("source_layer_role") or "").lower() in {"arch", "unknown"}
    ):
        return False
    role = str(el.get("layer_role") or "").lower()
    return role == "mep"


def _calc_velocity(flow_m3h: float, diameter_mm: float) -> float:
    """Q(유량 m³/h)와 D(관경 mm)으로 유속(m/s) 산출."""
    if diameter_mm <= 0:
        return 0.0
    r_m = (diameter_mm / 2.0) / 1000.0
    return (flow_m3h / 3600.0) / (math.pi * r_m * r_m)


_GAS_LAYER_DET_RE = re.compile(r"GAS|가스", re.IGNORECASE)


def _is_gas_element(el: dict) -> bool:
    """GAS 레이어/재질 기준 가스 배관 여부 판별."""
    mat   = str(el.get("material") or "").upper()
    layer = str(el.get("layer") or "")
    return "GAS" in mat or bool(_GAS_LAYER_DET_RE.search(layer))


def _dist2d(a: dict | None, b: dict | None) -> float:
    """두 점 {x, y} 사이 유클리드 거리. 좌표 없으면 inf."""
    if not a or not b:
        return math.inf
    try:
        return math.hypot(float(a["x"]) - float(b["x"]), float(a["y"]) - float(b["y"]))
    except (KeyError, TypeError, ValueError):
        return math.inf


def _violation(
    equipment_id: str,
    violation_type: str,
    reason: str,
    current_value: str,
    required_value: str,
    reference_rule: str,
    confidence_score: float = 1.0,
) -> dict:
    """확정적 위반 항목 표준 구조 생성 (confidence=1.0 고정 — 코드 확정)."""
    return {
        "equipment_id":   equipment_id,
        "violation_type": violation_type,
        "reference_rule": reference_rule,
        "current_value":  current_value,
        "required_value": required_value,
        "reason":         reason,
        "severity":       "major",
        "confidence_score":  confidence_score,
        "confidence_reason": "deterministic_code_check",
        "_source":        "deterministic",   # compliance와 구분용 내부 태그
    }


def run_deterministic_checks(
    elements: list[dict],
    topology: dict,
    geo: dict,
    *,
    unit_factor: float = 1.0,
    hanger_spacing_mm: float = _DEFAULT_HANGER_SPACING_MM,
) -> list[dict]:
    """
    확정적 배관 위반 검출을 실행한다.

    Args:
        elements      : ParserAgent 출력 elements 목록
        topology      : PipeTopologyBuilder.build() 결과
        geo           : GeometryPreprocessor.process() 결과
        unit_factor   : drawing_unit → mm 변환 계수 (geometry.separation_mm이 없을 때 폴백)
        hanger_spacing_mm: 행거 간격 기준 (기본 2000mm)

    Returns:
        violations list — LLM 결과와 동일 구조 (confidence_score=1.0)
    """
    violations: list[dict] = []

    # ── 핸들 → element 인덱스 ────────────────────────────────────────────────
    el_map: dict[str, dict] = {
        str(e.get("handle") or e.get("id") or ""): e
        for e in elements if isinstance(e, dict)
    }
    run_by_handle: dict[str, dict] = {}
    for run in topology.get("pipe_runs", []) or []:
        if not isinstance(run, dict):
            continue
        for h in run.get("handles") or []:
            run_by_handle[str(h)] = run
    broken_gap_handles = {
        str(h)
        for gap in (topology.get("broken_gaps") or [])
        for h in (gap.get("from_handle"), gap.get("to_handle"))
        if h
    }
    mismatch_handles = {
        str(h)
        for item in (topology.get("connection_mismatches") or [])
        for h in (item.get("endpoint_handle"), item.get("segment_handle"))
        if h
    }

    # ── 1. 단선 배관 감지 ─────────────────────────────────────────────────────
    # topology의 pipe_runs에서 단독(length=1) run이고 total_length_mm > 0 인 것은
    # 양 끝이 연결되지 않은 고립 세그먼트.
    # unconnected_lines 카운트 > 0 이면 실제로 고립된 세그먼트가 존재함.
    broken_runs = _collect_unconnected_runs(topology, unit_factor)
    for handle, length_mm, run in broken_runs:
        if handle in broken_gap_handles or handle in mismatch_handles:
            continue
        el = el_map.get(handle, {})
        # 건축 요소는 배관 위반 대상 제외
        if _is_arch_element(el):
            continue
        if length_mm < _CONTINUITY_MIN_LENGTH_MM:
            continue
        if not _has_strong_continuity_evidence(el, run):
            continue
        layer = el.get("layer", "")
        # HIDDEN 선종류 = 매립 배관 → 의도적 끝단일 수 있어 신뢰도 낮춤
        conf = 0.65 if _is_hidden_pipe(el) else 0.75
        violations.append(_violation(
            equipment_id=handle,
            violation_type="pipe_continuity_isolated_segment",
            reason=(
                f"고립 배관 세그먼트 — 양 끝점이 다른 배관과 연결되지 않음 "
                f"(길이 {length_mm:.0f}mm, 레이어 {layer!r}, 선종류 {el.get('linetype') or 'N/A'}). "
                "단선 또는 미완성 배관으로 의심됩니다."
            ),
            current_value=f"고립 세그먼트 {length_mm:.0f}mm",
            required_value="인접 배관과 끝점 연결 필요",
            reference_rule="배관 연속성 원칙 — 배관은 양 끝이 연결된 연속 경로를 구성해야 함",
            confidence_score=conf,
        ))

    for gap in topology.get("broken_gaps", []) or []:
        ha = str(gap.get("from_handle") or "")
        hb = str(gap.get("to_handle") or "")
        ea = el_map.get(ha, {})
        eb = el_map.get(hb, {})
        if _is_arch_element(ea) or _is_arch_element(eb):
            continue
        if not (
            _has_strong_continuity_evidence(ea, run_by_handle.get(ha, {}))
            and _has_strong_continuity_evidence(eb, run_by_handle.get(hb, {}))
        ):
            continue
        gap_mm = float(gap.get("gap_mm") or 0)
        if gap_mm <= 0:
            continue
        violations.append(_violation(
            equipment_id=ha,
            violation_type="pipe_gap",
            reason=(
                f"배관 후보 선분 사이 미연결 gap 감지 — {ha!r}와 {hb!r}의 끝점이 "
                f"{gap_mm:.0f}mm 떨어져 있으나 같은 방향·같은 스타일의 배관 경로로 보입니다. "
                "주석(G/DN 등)으로 복원되는 의도적 생략이 아니므로 단선 가능성이 있습니다."
            ),
            current_value=f"미연결 gap {gap_mm:.0f}mm",
            required_value="인접 배관 끝점 연결 또는 의도적 생략 주석 필요",
            reference_rule="배관 연속성 원칙 — 배관은 양 끝이 연결된 연속 경로를 구성해야 함",
            confidence_score=0.78,
        ))

    for item in topology.get("connection_mismatches", []) or []:
        endpoint_handle = str(item.get("endpoint_handle") or "")
        segment_handle = str(item.get("segment_handle") or "")
        ea = el_map.get(endpoint_handle, {})
        eb = el_map.get(segment_handle, {})
        if _is_arch_element(ea) or _is_arch_element(eb):
            continue
        if not (
            _has_strong_continuity_evidence(ea, run_by_handle.get(endpoint_handle, {}))
            and _has_strong_continuity_evidence(eb, run_by_handle.get(segment_handle, {}))
        ):
            continue
        offset_mm = float(item.get("offset_mm") or 0)
        if offset_mm <= 0:
            continue
        violations.append(_violation(
            equipment_id=endpoint_handle,
            violation_type="connection_mismatch",
            reason=(
                f"배관 접속점 어긋남 — {endpoint_handle!r} 끝점이 {segment_handle!r} 배관 선분에 "
                f"접속되어야 할 위치에서 {offset_mm:.0f}mm 벗어나 있습니다. "
                "T/L 접속부의 끝점 불일치 또는 미세 단선 가능성이 있습니다."
            ),
            current_value=f"접속점 offset {offset_mm:.0f}mm",
            required_value="배관 끝점과 접속 대상 선분이 허용 오차 내에서 접해야 함",
            reference_rule="배관 접속 정합 원칙 — 배관 접속부의 끝점은 대상 배관 또는 피팅에 정확히 접속되어야 함",
            confidence_score=0.76,
        ))

    # ── 3. MEP 설비 겹침 (overlapping=True) ──────────────────────────────────
    for pair in geo.get("mep_clearances", []):
        if not pair.get("overlapping"):
            continue
        ha, hb = str(pair.get("handle_a", "")), str(pair.get("handle_b", ""))
        la = el_map.get(ha, {}).get("layer", "")
        lb = el_map.get(hb, {}).get("layer", "")
        violations.append(_violation(
            equipment_id=ha,
            violation_type="collision",
            reason=(
                f"설비 겹침 — {ha!r}(레이어 {la!r})와 {hb!r}(레이어 {lb!r})의 "
                "bbox가 서로 겹칩니다. 두 설비가 동일 공간을 점유하여 충돌 가능성이 있습니다."
            ),
            current_value="이격거리 0mm (겹침)",
            required_value="0mm 초과 (겹침 없음)",
            reference_rule="MEP 설비 이격 기준 — 설비 간 최소 이격거리는 0mm 초과 (물리적 겹침 불가)",
            confidence_score=1.0,
        ))

    # ── 4. 블록 위치 중복 ─────────────────────────────────────────────────────
    blocks = [
        e for e in elements
        if str(e.get("raw_type") or "").upper() in ("INSERT", "BLOCK")
        and e.get("handle")
        and not _is_arch_element(e)   # 건축 블록 제외
    ]
    dup_pairs: set[tuple[str, str]] = set()
    for i, b1 in enumerate(blocks):
        p1 = b1.get("position") or b1.get("insert_point")
        for b2 in blocks[i + 1:]:
            p2 = b2.get("position") or b2.get("insert_point")
            d = _dist2d(p1, p2)
            if d <= _DUP_POSITION_TOL_MM * unit_factor:
                key = tuple(sorted([b1["handle"], b2["handle"]]))
                if key in dup_pairs:
                    continue
                dup_pairs.add(key)  # type: ignore[arg-type]
                violations.append(_violation(
                    equipment_id=b1["handle"],
                    violation_type="duplicate_equipment_position",
                    reason=(
                        f"블록 위치 중복 — {b1['handle']!r}와 {b2['handle']!r}가 "
                        f"{d:.1f}mm 이내 동일 좌표에 배치됨. 중복 삽입 오류로 의심됩니다."
                    ),
                    current_value=f"블록 간 거리 {d:.1f}mm",
                    required_value=f"{_DUP_POSITION_TOL_MM}mm 초과 (위치 분리 필요)",
                    reference_rule="설비 배치 원칙 — 동일 위치에 두 설비를 중복 배치하면 안 됨",
                    confidence_score=1.0,
                ))

    # ── 5. slope_pct 검증 ────────────────────────────────────────────────
    # A non-orthogonal 2D line is often just plan-view routing, not pipe slope.
    # Treat missing slope as a violation only when the CAD data has measurable
    # Z/elevation delta; otherwise it is data_missing/manual-review territory.
    pipe_run_handles = {
        str(h)
        for run in topology.get("pipe_runs", [])
        for h in (run.get("handles") or [])
    }
    for el in elements:
        if _is_arch_element(el):
            continue
        if str(el.get("raw_type") or "").upper() not in ("LINE", "ARC"):
            continue
        handle = str(el.get("handle") or el.get("id") or "")
        if pipe_run_handles and handle not in pipe_run_handles:
            continue
        angle = el.get("angle_deg")
        slope = el.get("slope_pct") or 0
        if angle is None or slope != 0:
            continue

        # XY 평면상 표준 각도(직교 및 45도 부속 각도) 여부 판단
        norm = abs(float(angle)) % 180
        # 0°, 45°, 90°, 135° 주변 각도인지 확인
        is_standard_routing = (
            norm < _SLOPE_ORTH_TOL_DEG or                           # 0° (180°)
            abs(norm - 45) < _SLOPE_ORTH_TOL_DEG or                 # 45°
            abs(norm - 90) < _SLOPE_ORTH_TOL_DEG or                 # 90°
            abs(norm - 135) < _SLOPE_ORTH_TOL_DEG or                # 135°
            norm > (180 - _SLOPE_ORTH_TOL_DEG)                      # 180°
        )

        # 표준 각도가 아닌 선도 2D 평면 배관일 수 있으므로 Z 차이가 있어야만 판단한다.
        if not is_standard_routing:
            start_z = (el.get("start") or {}).get("z", 0) or 0
            end_z   = (el.get("end")   or {}).get("z", 0) or 0
            z_diff  = abs(float(start_z) - float(end_z))
            if z_diff <= 1e-9:
                continue

            violations.append(_violation(
                equipment_id=handle,
                violation_type="slope_error",
                reason=(
                    f"Z 고도차 {z_diff:.3f}가 있는 경사 배관이지만 기울기 미표기. "
                    "구배가 필요한 배관은 기울기(slope_pct) 명시가 필요합니다."
                ),
                current_value=f"각도 {float(angle):.1f}°, Z 고도차 {z_diff:.3f}, 기울기 0%",
                required_value="기울기 명시 필요 (예: 1/100, 2% 등)",
                reference_rule="배관 기울기 표기 기준 — 구배용 경사 배관은 기울기를 도면에 명시해야 함",
                confidence_score=0.90,
            ))

    # ── 6. temp_c → 보온재 의무 플래그 ──────────────────────────────────────
    for el in elements:
        if _is_arch_element(el):
            continue
        temp = el.get("temp_c")
        if temp is None or float(temp) < _INSULATION_TEMP_C:
            continue
        handle = str(el.get("handle") or el.get("id") or "")
        violations.append(_violation(
            equipment_id=handle,
            violation_type="insulation_thickness_error",
            reason=f"유체 온도 {float(temp):.0f}℃ ≥ {_INSULATION_TEMP_C:.0f}℃ — 고온 배관 보온재 의무 적용 대상.",
            current_value=f"유체 온도 {float(temp):.0f}℃",
            required_value=f"{_INSULATION_TEMP_C:.0f}℃ 미만 또는 보온재 시공 확인",
            reference_rule="KDS 31 60 05 — 60℃ 초과 고온 배관 보온재 의무 설치",
            confidence_score=0.60,
        ))

    # ── 7. flow_rate_m3h + diameter_mm → 계산 유속 검증 ─────────────────────
    for el in elements:
        if _is_arch_element(el):
            continue
        flow = el.get("flow_rate_m3h")
        dia  = el.get("diameter_mm") or 0
        if not flow or not dia:
            continue
        calc_v = _calc_velocity(float(flow), float(dia))
        is_gas = _is_gas_element(el)
        max_v  = _GAS_VEL_MAX if is_gas else _WATER_VEL_MAX
        dlabel = "가스" if is_gas else "급수"
        if calc_v > max_v:
            handle = str(el.get("handle") or el.get("id") or "")
            violations.append(_violation(
                equipment_id=handle,
                violation_type="pipe_size_mismatch",
                reason=f"유량 {float(flow):.1f}m³/h, DN{float(dia):.0f} → 계산 유속 {calc_v:.2f}m/s > 한계 {max_v:.1f}m/s.",
                current_value=f"계산 유속 {calc_v:.2f}m/s",
                required_value=f"최대 유속 {max_v:.1f}m/s 이하",
                reference_rule=f"배관 유속 기준 — {dlabel} 배관 최대 유속 {max_v:.1f}m/s 이하",
                confidence_score=0.95,
            ))

    # ── 8. velocity_ms 도면 주석값 직접 검증 ────────────────────────────────
    for el in elements:
        if _is_arch_element(el):
            continue
        v = el.get("velocity_ms")
        if v is None:
            continue
        is_gas = _is_gas_element(el)
        max_v  = _GAS_VEL_MAX if is_gas else _WATER_VEL_MAX
        dlabel = "가스" if is_gas else "급수"
        if float(v) > max_v:
            handle = str(el.get("handle") or el.get("id") or "")
            violations.append(_violation(
                equipment_id=handle,
                violation_type="pipe_size_mismatch",
                reason=f"도면 주석 유속 {float(v):.2f}m/s > 한계 {max_v:.1f}m/s — 유속 기준 초과.",
                current_value=f"도면 주석 유속 {float(v):.2f}m/s",
                required_value=f"최대 유속 {max_v:.1f}m/s 이하",
                reference_rule=f"배관 유속 기준 — {dlabel} 배관 최대 유속 {max_v:.1f}m/s 이하",
                confidence_score=0.98,
            ))

    det_count = len(violations)
    _log.info(
        "[PipeDeterministicChecker] 확정적 위반 %d건 "
        "(단선=%d, gap=%d, 접속어긋남=%d, 겹침=%d, 위치중복=%d, 경사미표기=%d, 보온=%d, 유속=%d)",
        det_count,
        sum(1 for v in violations if "고립 배관" in v.get("reason", "")),
        sum(1 for v in violations if "미연결 gap" in v.get("reason", "")),
        sum(1 for v in violations if "접속점 어긋남" in v.get("reason", "")),
        sum(1 for v in violations if "bbox가 서로 겹" in v.get("reason", "")),
        sum(1 for v in violations if "위치 중복" in v.get("reason", "")),
        sum(1 for v in violations if "기울기 미표기" in v.get("reason", "")),
        sum(1 for v in violations if "보온재 의무" in v.get("reason", "")),
        sum(1 for v in violations if "유속" in v.get("reason", "") and "한계" in v.get("reason", "")),
    )
    return violations


# ── 내부 헬퍼 ─────────────────────────────────────────────────────────────────

def _collect_unconnected_runs(
    topology: dict,
    unit_factor: float,
) -> list[tuple[str, float, dict]]:
    """
    단선 pipe_run (멤버 1개, 인접 run 없음)에서 (handle, length_mm) 수집.
    total_length > 0 인 것만 (길이가 0인 점 LINE은 제외).
    """
    result: list[tuple[str, float, dict]] = []
    runs = topology.get("pipe_runs", [])
    if not runs:
        return result

    for run in runs:
        handles = run.get("handles") or []
        if len(handles) != 1:
            continue   # 단독 세그먼트만 대상
        if run.get("connected_blocks"):
            continue   # 블록에 연결된 것은 끝단 배관일 수 있음 → 제외
        length_mm = run.get("total_length_mm") or (
            run.get("total_length", 0) * unit_factor
        )
        if length_mm <= 0:
            continue
        result.append((handles[0], length_mm, run))
    return result
