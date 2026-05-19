"""
File    : backend/services/agents/elec/sub/deterministic_checker.py
Author  : 김지우
Create  : 2026-04-28
Modified: 2026-05-04
Description :
  LLM에 의존하지 않는 확정적(코드 기반) 전기 위반 검출기.
"""

from __future__ import annotations

import logging
import math
import re

from backend.services.agents.elec.entity_role_classifier import (
    ELECTRIC_CONTEXT,
    ELECTRIC_CORE,
    classify_entity_role,
)

_log = logging.getLogger(__name__)

_CB_MIN_SQ: dict[int, float] = {
    10: 1.5,
    15: 1.5,
    16: 1.5,
    20: 2.5,
    25: 4.0,
    30: 6.0,
    32: 6.0,
    40: 10.0,
    50: 16.0,
    63: 25.0,
    80: 35.0,
    100: 50.0,
    125: 70.0,
    160: 95.0,
    200: 120.0,
}

_MAX_DEVICES_PER_CIRCUIT = 20
_MAX_SINGLE_CIRCUIT_LEN_MM = 30_000
_OVERLAP_TOL = 5.0
_ISOLATED_DEVICE_MAX_GAP_MM = 1500.0
_CIRCLE_OVERLAP_TOL = 1.0
_TERMINAL_GRID_TOL_RATIO = 0.22
_TERMINAL_SYMMETRY_MIN = 0.75
_STANDALONE_TEXT_RE = re.compile(
    r"\b(?:TEST|E1|E2|GROUND|GND|GRD|EARTH|LA|TR|TB|TEST\s*BOX)\b|\uc811\uc9c0|\ud14c\uc2a4\ud2b8|\uce21\uc815",
    re.IGNORECASE,
)
_TEST_TEXT_RE = re.compile(r"\bTEST\b|\ud14c\uc2a4\ud2b8", re.IGNORECASE)
_GROUND_CONTEXT_TEXT_RE = re.compile(
    r"\b(?:E1|E2|GROUND|GND|GRD|EARTH|LA|TR|TB)\b|\uc811\uc9c0|\uce21\uc815",
    re.IGNORECASE,
)
_LOW_SIGNAL_LAYER_RE = re.compile(r"DIM|TEXT|ANNO|CENTER|CENTRE|GRID|BORDER|TABLE|DEFPOINTS", re.IGNORECASE)
_DASHED_LINETYPE_RE = re.compile(
    r"^(?:G[0-9]+|F[0-9]+|DASHED|HIDDEN|DOTTED|PHANTOM|DIVIDE|ZIGZAG|CENTER2?|DOT)",
    re.IGNORECASE,
)
_GROUND_TEST_LAYER_RE = re.compile(
    r"GROUND|GND|GRD|EARTH|TEST|MEASURE|\uce21\uc815|\uc811\uc9c0|\ud14c\uc2a4\ud2b8",
    re.IGNORECASE,
)
_OUTLET_CONNECTION_HINT_RE = re.compile(r"OUTLET|SOCKET|RECEPT|PLUG|콘센트", re.IGNORECASE)
_DEVICE_CONNECTION_LAYER_RE = re.compile(r"전등|조명|전기|배선|회로|전원|LIGHT|POWER|CIRCUIT", re.IGNORECASE)
_CIRCUIT_VIOLATION_TYPES = {
    "open_circuit_error",
    "wire_disconnected",
    "device_not_connected",
    "ground_missing",
    "panel_overload",
    "breaker_mismatch",
    "overcrowded_circuit",
    "excessive_circuit_length",
}

_PHASE_EXPECTED_POLES: dict[int, frozenset[int]] = {
    1: frozenset({1, 2}),
    3: frozenset({3, 4}),
}

_STANDARD_KR_VOLTAGES: frozenset[int] = frozenset({
    110, 220, 380, 440, 3300, 6600, 11000, 22900,
})
_VOLTAGE_TOLERANCE = 15

_GROUNDING_ROD_TEXT_RE = re.compile(
    r"접지봉|접지극|earth\s*rod|ground\s*rod",
    re.IGNORECASE,
)
_GROUNDING_E_LABEL_RE = re.compile(r"\bE([1-9]\d?)\b")
_GROUNDING_QTY_RE = re.compile(r"[×xX]\s*(\d+)\s*EA\b", re.IGNORECASE)
# KEC 140.6: 봉 길이 추출 (Φ18x2400 → 2400mm)
_GROUNDING_ROD_LENGTH_RE = re.compile(r"(?:Φ|φ|fi)\s*\d+\s*[xX×]\s*(\d+)", re.IGNORECASE)

# KEC 232.56: 콘센트 설치 높이(MH) 검사
_MH_VALUE_RE = re.compile(r"\bMH\s*:?\s*(\d+)\s*m?m\b", re.IGNORECASE)
_MH_OUTLET_CONTEXT_RE = re.compile(r"콘센트|outlet|socket|MH", re.IGNORECASE)
_HAZARDOUS_AREA_TEXT_RE = re.compile(
    r"가연성|위험\s*구역|위험\s*장소|방폭|가스|보일러실|기계실|"
    r"hazard(?:ous)?|explosion[-\s]*proof|gas|boiler",
    re.IGNORECASE,
)
_MH_MIN_GENERAL    = 300   # mm — KEC 232.56 일반 최솟값
_MH_MIN_HAZARDOUS  = 500   # mm — 가연성 가스 구역 권고 최솟값

# 전선 가닥 수 검사
_WIRE_CIRCLE_NUM_RE = re.compile(r"[①②③④⑤⑥⑦⑧⑨]")
_WIRE_CIRCLE_MAP: dict[str, int] = {
    "①": 1, "②": 2, "③": 3, "④": 4,
    "⑤": 5, "⑥": 6, "⑦": 7, "⑧": 8, "⑨": 9,
}
# 회로 타입 → 기대 가닥 수 (상선+중성+접지 포함)
_CIRCUIT_TYPE_RE = re.compile(
    r"(?P<phase>단상|1[φΦ]|3상|3[φΦ])\s*"
    r"(?P<wire>[234])\s*(?:선|W|선식)",
    re.IGNORECASE,
)
_CIRCUIT_WIRE_COUNT: dict[str, int] = {
    "단상2": 2, "1φ2": 2, "1Φ2": 2,
    "단상3": 3, "1φ3": 3, "1Φ3": 3,
    "3상3":  3, "3φ3": 3, "3Φ3": 3,
    "3상4":  4, "3φ4": 4, "3Φ4": 4,
}


def _xy_from_mapping(value: object) -> tuple[float, float] | None:
    if not isinstance(value, dict):
        return None
    try:
        return float(value["x"]), float(value["y"])
    except (KeyError, TypeError, ValueError):
        return None


def _wire_endpoint_for_side(entity: dict | None, side: object) -> tuple[float, float] | None:
    if not entity:
        return None
    side_name = str(side or "").lower()
    if side_name not in {"start", "end"}:
        side_name = "start"
    etype = _etype(entity)
    if etype == "LINE":
        return _xy_from_mapping(entity.get(side_name)) or _xy_from_mapping(entity.get(f"{side_name}_point"))
    if etype in {"POLYLINE", "LWPOLYLINE", "SPLINE"}:
        vertices = entity.get("vertices") or entity.get("points") or []
        if vertices:
            idx = 0 if side_name == "start" else -1
            return _xy_from_mapping(vertices[idx])
    return _entity_point(entity)


def _same_route_bridge_style(arc: dict, line_a: dict, line_b: dict) -> bool:
    default_types = {"", "CONTINUOUS", "BYLAYER", "BYBLOCK"}
    arc_layer = str(arc.get("layer") or "").strip().upper()
    arc_linetype = str(arc.get("linetype") or "").strip().upper()
    a_layer = str(line_a.get("layer") or "").strip().upper()
    b_layer = str(line_b.get("layer") or "").strip().upper()
    a_linetype = str(line_a.get("linetype") or "").strip().upper()
    b_linetype = str(line_b.get("linetype") or "").strip().upper()
    if arc_linetype not in default_types and arc_linetype in {a_linetype, b_linetype}:
        return True
    return bool(arc_layer and arc_layer in {a_layer, b_layer})


def _point_in_unordered_bbox(point: tuple[float, float], bbox: dict, margin: float = 0.0) -> bool:
    try:
        x1 = float(bbox["x1"])
        y1 = float(bbox["y1"])
        x2 = float(bbox["x2"])
        y2 = float(bbox["y2"])
    except (KeyError, TypeError, ValueError):
        return False
    left, right = min(x1, x2), max(x1, x2)
    bottom, top = min(y1, y2), max(y1, y2)
    return left - margin <= point[0] <= right + margin and bottom - margin <= point[1] <= top + margin


def _arc_bridges_topology_segment(
    seg: dict,
    handle_to_element: dict[str, dict],
    elements: list[dict],
    unit_factor: float,
) -> bool:
    line_a = handle_to_element.get(str(seg.get("handle_a") or ""))
    line_b = handle_to_element.get(str(seg.get("handle_b") or ""))
    point_a = _wire_endpoint_for_side(line_a, seg.get("side_a"))
    point_b = _wire_endpoint_for_side(line_b, seg.get("side_b"))
    if not (line_a and line_b and point_a and point_b):
        return False
    gap = math.hypot(point_a[0] - point_b[0], point_a[1] - point_b[1])
    margin = max(10.0 / max(unit_factor, 1e-9), gap * 0.1)
    for entity in elements:
        if _etype(entity) != "ARC":
            continue
        if not _same_route_bridge_style(entity, line_a, line_b):
            continue
        bbox = entity.get("bbox")
        if isinstance(bbox, dict) and _point_in_unordered_bbox(point_a, bbox, margin) and _point_in_unordered_bbox(point_b, bbox, margin):
            return True
    return False


def _entity_label_blob(entity: dict | None) -> str:
    if not entity:
        return ""
    return " ".join(
        str(entity.get(k) or "")
        for k in ("layer", "name", "effective_name", "block_name", "standard_name", "tag_name")
    )


def _connection_gap_message(
    seg: dict,
    handle_to_element: dict[str, dict],
) -> tuple[str, str, str, str]:
    handle_a = str(seg.get("handle_a") or "")
    handle_b = str(seg.get("handle_b") or "")
    gap = float(seg.get("gap_mm") or 0)
    blob = " ".join([
        _entity_label_blob(handle_to_element.get(handle_a)),
        _entity_label_blob(handle_to_element.get(handle_b)),
    ])
    if _OUTLET_CONNECTION_HINT_RE.search(blob):
        return (
            f"콘센트 연결선이 인접한 회로선과 이어지지 않습니다 (간격 {gap:.0f}mm). "
            "이 상태에서는 해당 콘센트가 회로에서 분리됩니다.",
            "콘센트 회로 배선 연결 확인",
            "콘센트 연결선을 끊어진 회로선 끝점까지 이어 주십시오.",
            "콘센트 연결선 연속 연결",
        )
    if _DEVICE_CONNECTION_LAYER_RE.search(blob):
        return (
            f"전기 기구 연결선이 인접한 회로선과 이어지지 않습니다 (간격 {gap:.0f}mm). "
            "기구에서 나온 연결선이 회로 배선과 분리된 상태입니다.",
            "전기 기구 회로 배선 연결 확인",
            "기구 연결선을 끊어진 회로선 끝점까지 이어 주십시오.",
            "기구 연결선 연속 연결",
        )
    return (
        f"배선이 끊어져 있습니다 (gap {gap:.0f}mm). "
        "미결선 상태로는 설비에 전원이 공급되지 않습니다.",
        "KEC 232.3 배선의 연속성",
        "끊어진 배선을 연결하거나 누락된 선분을 추가하십시오.",
        "연속 연결",
    )


def run_deterministic_checks(
    elements: list[dict],
    extracted_attrs: dict[str, dict],
    topology: dict,
    unit_factor: float = 1.0,
    qa_reference_elements: list[dict] | None = None,
) -> list[dict]:
    violations: list[dict] = []
    drawing_intent = str((topology.get("summary") or {}).get("drawing_intent") or topology.get("drawing_intent") or "")

    handle_linetype: dict[str, str] = {
        str(e.get("handle") or ""): str(e.get("linetype") or "").strip().upper()
        for e in elements
    }
    handle_to_element: dict[str, dict] = {
        str(e.get("handle") or ""): e
        for e in elements
        if e.get("handle")
    }

    qa_context = build_geometry_qa_context(
        elements,
        topology,
        unit_factor,
        qa_reference_elements=qa_reference_elements,
    )
    topology["drawing_internal_standards"] = qa_context["drawing_internal_standards"]
    topology["terminal_debug"] = qa_context["terminal_debug"]
    violations.extend(_run_terminal_geometry_qa(topology, unit_factor, qa_context))
    violations.extend(_run_orphan_terminal_check(elements, topology, qa_context))

    # 1. 단선
    for seg in ([] if drawing_intent == "DETAIL_DRAWING" else topology.get("broken_segments", [])):
        ha = seg.get("handle_a", "")
        hb = seg.get("handle_b", "")
        lt_a = handle_linetype.get(ha, "")
        lt_b = handle_linetype.get(hb, "")
        if (lt_a and lt_b
                and lt_a not in ("CONTINUOUS", "BYLAYER", "BYBLOCK")
                and lt_b not in ("CONTINUOUS", "BYLAYER", "BYBLOCK")
                and bool(_DASHED_LINETYPE_RE.match(lt_a))
                and bool(_DASHED_LINETYPE_RE.match(lt_b))):
            _log.debug(
                "[DeterministicChecker] open_circuit_error 억제 (다형 선종): %s(%s) ↔ %s(%s)",
                ha, lt_a, hb, lt_b,
            )
            continue
        if _arc_bridges_topology_segment(seg, handle_to_element, elements, unit_factor):
            _log.debug(
                "[DeterministicChecker] open_circuit_error suppressed by bridge ARC: %s <-> %s",
                ha, hb,
            )
            continue
        reason, legal_reference, suggestion, required_value = _connection_gap_message(seg, handle_to_element)
        gap_mm = float(seg.get("gap_mm") or 0)
        violations.append({
            "object_id": seg.get("handle_a", ""),
            "violation_type": "open_circuit_error",
            "category": "topology_qa",
            "severity": "Critical",
            "reason": reason,
            "legal_reference": legal_reference,
            "suggestion": suggestion,
            "current_value": f"gap {gap_mm:.0f}mm",
            "required_value": required_value,
            "midpoint": seg.get("midpoint"),
            "handle_b": seg.get("handle_b", ""),
            "target_handles": [h for h in (seg.get("handle_a"), seg.get("handle_b")) if h],
        })

    # 1-b. 고립된 디바이스 (배선과 연결되지 않은 설비)
    for dev in ([] if drawing_intent == "DETAIL_DRAWING" else topology.get("isolated_devices", [])):
        gap_mm = dev.get("gap_mm")
        try:
            gap_value = float(gap_mm) if gap_mm is not None else None
        except (TypeError, ValueError):
            gap_value = None
        if gap_value is not None and gap_value > _ISOLATED_DEVICE_MAX_GAP_MM and not dev.get("is_near_dangling"):
            _log.debug(
                "[DeterministicChecker] isolated device suppressed by distant route gap: %s gap=%.1fmm",
                dev.get("handle", ""),
                gap_value,
            )
            continue
        gap_mm = gap_value
        category_label = dev.get("category") or dev.get("name") or "설비"
        if gap_mm is not None:
            reason = (
                f"{category_label} 기기(handle:{dev.get('handle','')})가 배선과 연결되어 있지 않습니다. "
                f"가장 가까운 배선 끝점까지 {gap_mm:.0f}mm 떨어져 있습니다."
            )
        else:
            reason = (
                f"{category_label} 기기(handle:{dev.get('handle','')})가 배선과 연결되어 있지 않습니다. "
                "근처에 배선이 없습니다."
            )
        violations.append({
            "object_id": dev.get("handle", ""),
            "violation_type": "open_circuit_error",
            "category": "topology_qa",
            "severity": "Critical",
            "reason": reason,
            "legal_reference": "KEC 232.3 배선의 연속성",
            "suggestion": "해당 기기에 배선을 연결하세요.",
            "current_value": f"gap {gap_mm:.0f}mm" if gap_mm is not None else "미연결",
            "required_value": "배선 연결 필요",
        })

    # 1-c. E-OUTLET 줄기선 고립 감지
    # "콘센트 그려줘"로 생성된 E-OUTLET 레이어 줄기선이 회로 배선에 연결 안 된 경우
    if drawing_intent != "DETAIL_DRAWING":
        broken_gap_handles = {
            str(h)
            for seg in (topology.get("broken_segments") or [])
            for h in (seg.get("handle_a"), seg.get("handle_b"))
            if h
        }
        _OUTLET_STEM_LAYER_RE = re.compile(r"^E[-_]?(?:OUTLET|SOCKET)", re.IGNORECASE)
        _handle_to_layer: dict[str, str] = {
            str(e.get("handle") or ""): str(e.get("layer") or "")
            for e in elements
            if e.get("handle")
        }
        _reported_outlet_runs: set[str] = set()
        for run in topology.get("circuit_runs", []):
            handles = run.get("handles") or []
            if not handles:
                continue
            if broken_gap_handles.intersection(str(h) for h in handles):
                continue
            # 이 run에서 E-OUTLET 레이어 줄기선과 그 외 배선 분류
            outlet_hs = [h for h in handles if _OUTLET_STEM_LAYER_RE.search(_handle_to_layer.get(h, ""))]
            other_hs  = [h for h in handles if h not in outlet_hs]
            # E-OUTLET 줄기선만 있고 다른 배선·패널에 연결 안 된 run → 고립된 콘센트
            if outlet_hs and not other_hs and not run.get("connected_panels"):
                run_key = outlet_hs[0]
                if run_key not in _reported_outlet_runs:
                    _reported_outlet_runs.add(run_key)
                    violations.append({
                        "object_id": run_key,
                        "violation_type": "open_circuit_error",
                        "category": "topology_qa",
                        "severity": "Critical",
                        "reason": (
                            "콘센트 줄기선이 회로 배선과 연결되어 있지 않습니다. "
                            "전원이 공급되지 않는 상태입니다."
                        ),
                        "legal_reference": "KEC 232.3 배선의 연속성",
                        "suggestion": "콘센트 줄기선을 분전반 또는 회로 배선에 연결하세요.",
                        "current_value": "미연결 (E-OUTLET 단독 회로)",
                        "required_value": "회로 배선 연결 필요",
                    })

    if drawing_intent != "DETAIL_DRAWING":
        violations.extend(_run_wire_overlap_checks(elements, topology, unit_factor))

    # 2. 전압 속성 모순
    for bh, attrs in extracted_attrs.items():
        v = attrs.get("voltage_v")
        v_alt = attrs.get("voltage_alt_v")
        if v and v_alt and abs(v - v_alt) > 100:
            if min(v, v_alt) < 300 and max(v, v_alt) >= 380:
                violations.append({
                    "object_id": bh,
                    "violation_type": "voltage_mismatch",
                    "reason": f"Equipment {bh} has conflicting voltage annotations.",
                    "legal_reference": "Electrical semantic QA",
                    "suggestion": "Use one consistent voltage annotation.",
                    "current_value": f"{v}V / {v_alt}V",
                    "required_value": "consistent voltage",
                })

    # 3. 전선 굵기 vs 차단기 용량 불일치
    for bh, attrs in extracted_attrs.items():
        sq = attrs.get("cable_sqmm")
        cb = attrs.get("circuit_breaker_a")

        if not (sq and cb):
            continue

        try:
            sq_val = float(sq)
            cb_val = int(float(cb))
        except (TypeError, ValueError):
            continue

        closest_cb = min(_CB_MIN_SQ.keys(), key=lambda k: abs(k - cb_val))
        min_sq = _CB_MIN_SQ.get(closest_cb, 0)

        if sq_val < min_sq:
            violations.append({
                "object_id": bh,
                "violation_type": "undersized_cable",
                "reason": f"Cable size {sq_val}SQ is below the reference size for {cb_val}A.",
                "legal_reference": "Electrical semantic QA",
                "suggestion": f"Review cable size and use at least {min_sq}SQ when applicable.",
                "current_value": f"{sq_val}SQ",
                "required_value": f"{min_sq}SQ",
            })

    # 4. 차단기 극수 vs 상수 불일치
    _pole_re = re.compile(r"^(\d+)P$", re.IGNORECASE)
    for bh, attrs in extracted_attrs.items():
        phase = attrs.get("phase")
        pole_options = attrs.get("pole_options") or []
        if not phase or not pole_options:
            continue
        try:
            phase_val = int(phase)
        except (TypeError, ValueError):
            continue
        expected_poles = _PHASE_EXPECTED_POLES.get(phase_val)
        if not expected_poles:
            continue
        for pole_str in pole_options:
            m = _pole_re.match(str(pole_str).upper())
            if not m:
                continue
            pole_num = int(m.group(1))
            if pole_num not in expected_poles:
                phase_label = "단상" if phase_val == 1 else "삼상"
                expected_str = "/".join(f"{p}P" for p in sorted(expected_poles))
                violations.append({
                    "object_id": bh,
                    "violation_type": "breaker_pole_mismatch",
                    "reason": f"{phase_label} 회로에 {pole_str} 차단기가 표기되어 있습니다.",
                    "legal_reference": "전기 설비 의미 QA",
                    "suggestion": f"{phase_label} 회로에는 {expected_str} 차단기를 사용하십시오.",
                    "current_value": pole_str,
                    "required_value": expected_str,
                })
                break

    # 5. 비표준 전압 표기
    for bh, attrs in extracted_attrs.items():
        voltage = attrs.get("voltage_v")
        if not voltage:
            continue
        try:
            volt_val = int(voltage)
        except (TypeError, ValueError):
            continue
        if not any(abs(volt_val - std) <= _VOLTAGE_TOLERANCE for std in _STANDARD_KR_VOLTAGES):
            violations.append({
                "object_id": bh,
                "violation_type": "nonstandard_voltage",
                "reason": f"비표준 전압 {volt_val}V가 표기되어 있습니다.",
                "legal_reference": "전기 설비 의미 QA",
                "suggestion": f"한국 표준 전압({', '.join(str(v)+'V' for v in sorted(_STANDARD_KR_VOLTAGES))}) 여부를 확인하십시오.",
                "current_value": f"{volt_val}V",
                "required_value": "표준 전압",
            })

    # 6. 과도한 분기 회로
    # topology.py에서 추가한 connected_devices 기준으로 검사한다.
    # handles의 전선 LINE 개수만으로 기기를 세는 방식은 오탐 가능성이 있다.
    for run in topology.get("circuit_runs", []):
        device_count = len(run.get("connected_devices", []))

        if device_count > _MAX_DEVICES_PER_CIRCUIT:
            panel_list = run.get("connected_panels", [])
            violations.append({
                "object_id": panel_list[0] if panel_list else run.get("run_id", ""),
                "violation_type": "overcrowded_circuit",
                "reason": f"One circuit run has {device_count} connected devices.",
                "legal_reference": "Electrical topology QA",
                "suggestion": "Review whether the circuit should be split.",
                "current_value": f"{device_count} devices",
                "required_value": f"{_MAX_DEVICES_PER_CIRCUIT} devices or fewer",
            })

    # 7. 단일 회로 배선 과다
    for run in topology.get("circuit_runs", []):
        total_mm = run.get("total_length_mm") or (
            run.get("total_length", 0) * unit_factor
        )
        voltage = run.get("voltage", 0)

        if voltage and 200 <= voltage <= 240 and total_mm > _MAX_SINGLE_CIRCUIT_LEN_MM:
            violations.append({
                "object_id": run.get("run_id", ""),
                "violation_type": "excessive_circuit_length",
                "reason": f"Single 220V circuit route is long: {total_mm / 1000:.1f}m.",
                "legal_reference": "Electrical topology QA",
                "suggestion": "Review voltage drop or route segmentation.",
                "current_value": f"{total_mm / 1000:.1f}m",
                "required_value": f"{_MAX_SINGLE_CIRCUIT_LEN_MM / 1000:.0f}m",
            })

    # 8. 전기기기 bbox 중복 배치
    # 원본 elements를 제거하지 않고, block_overlap 검사는 전기 블록에만 제한한다.
    block_entities = [
        e for e in elements
        if str(e.get("raw_type") or e.get("type") or "").upper() in ("INSERT", "BLOCK")
        and e.get("bbox")
        and _is_elec_block(e)
    ]

    reported_pairs: set[frozenset] = set()

    for i in range(len(block_entities)):
        bi = block_entities[i]
        bbox_i = _bbox(bi)
        if not bbox_i:
            continue

        for j in range(i + 1, len(block_entities)):
            bj = block_entities[j]
            bbox_j = _bbox(bj)
            if not bbox_j:
                continue

            if _overlaps(bbox_i, bbox_j, _OVERLAP_TOL / max(unit_factor, 1e-9)):
                hi = str(bi.get("handle") or "")
                hj = str(bj.get("handle") or "")

                key = frozenset({hi, hj})
                if key in reported_pairs:
                    continue

                reported_pairs.add(key)
                violations.append({
                    "object_id": hi,
                    "violation_type": "block_overlap",
                    "reason": f"Electrical equipment {hi} and {hj} have overlapping bounding boxes.",
                    "legal_reference": "Electrical geometry QA",
                    "suggestion": "Review the overlapping equipment placement.",
                    "current_value": "bbox overlap",
                    "required_value": "non-overlapping placement",
                })

    violations.extend(_run_electrical_symbol_overlap_checks(elements, qa_context, unit_factor))
    violations.extend(_run_grounding_rod_count_check(elements, unit_factor, qa_reference_elements))
    violations.extend(_run_grounding_rod_spacing_check(elements, unit_factor, qa_reference_elements))
    violations.extend(_run_outlet_height_check(elements, unit_factor, qa_reference_elements))
    violations.extend(_run_wire_count_check(elements, unit_factor, qa_reference_elements))

    _log.info(
        "[DeterministicChecker] 확정적 위반 %d건 "
        "(broken=%d, isolated_dev=%d, voltage=%d, cable=%d, pole=%d, nonstandard_v=%d, "
        "gnd_count=%d, gnd_spacing=%d, "
        "outlet_height=%d, wire_count=%d, "
        "crowd=%d, length=%d, block_overlap=%d, symbol_overlap=%d)",
        len(violations),
        len(topology.get("broken_segments", [])),
        len(topology.get("isolated_devices", [])),
        sum(1 for v in violations if v["violation_type"] == "voltage_mismatch"),
        sum(1 for v in violations if v["violation_type"] == "undersized_cable"),
        sum(1 for v in violations if v["violation_type"] == "breaker_pole_mismatch"),
        sum(1 for v in violations if v["violation_type"] == "nonstandard_voltage"),
        sum(1 for v in violations if v["violation_type"] == "grounding_rod_count_mismatch"),
        sum(1 for v in violations if v["violation_type"] == "grounding_rod_spacing_violation"),
        sum(1 for v in violations if v["violation_type"] == "outlet_height_violation"),
        sum(1 for v in violations if v["violation_type"] == "wire_count_violation"),
        sum(1 for v in violations if v["violation_type"] == "overcrowded_circuit"),
        sum(1 for v in violations if v["violation_type"] == "excessive_circuit_length"),
        sum(1 for v in violations if v["violation_type"] == "block_overlap"),
        sum(1 for v in violations if v["violation_type"] == "electrical_symbol_overlap"),
    )

    if drawing_intent == "DETAIL_DRAWING":
        violations = [
            v for v in violations
            if str(v.get("violation_type") or "") not in _CIRCUIT_VIOLATION_TYPES
        ]

    return violations


def _points_bbox(points: list[tuple[float, float]], radii: list[float]) -> dict:
    r = max(radii) if radii else 0.0
    return {
        "x1": min(p[0] for p in points) - r,
        "y1": min(p[1] for p in points) - r,
        "x2": max(p[0] for p in points) + r,
        "y2": max(p[1] for p in points) + r,
    }


def _expected_grid_points(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    xs = sorted(p[0] for p in points)
    ys = sorted(p[1] for p in points)
    x_cols = [sum(xs[:2]) / 2.0, sum(xs[-2:]) / 2.0]
    y_rows = [sum(ys[:2]) / 2.0, sum(ys[-2:]) / 2.0]
    return [(x, y) for x in x_cols for y in y_rows]


def _max_grid_deviation(points: list[tuple[float, float]], expected: list[tuple[float, float]]) -> float:
    if not points or not expected:
        return 0.0
    return max(min(math.hypot(px - ex, py - ey) for ex, ey in expected) for px, py in points)


def _median_spacing(points: list[tuple[float, float]]) -> float:
    dists = sorted(
        math.hypot(a[0] - b[0], a[1] - b[1])
        for i, a in enumerate(points)
        for b in points[i + 1:]
    )
    return dists[len(dists) // 2] if dists else 0.0


def _spacing_cv(points: list[tuple[float, float]]) -> float:
    nearest = []
    for i, p in enumerate(points):
        dists = [
            math.hypot(p[0] - other[0], p[1] - other[1])
            for j, other in enumerate(points)
            if i != j
        ]
        if dists:
            nearest.append(min(dists))
    if not nearest:
        return 0.0
    mean = sum(nearest) / len(nearest)
    if mean <= 1e-9:
        return 0.0
    variance = sum((d - mean) ** 2 for d in nearest) / len(nearest)
    return math.sqrt(variance) / mean


def _symmetry_score(points: list[tuple[float, float]]) -> float:
    cx = sum(p[0] for p in points) / len(points)
    cy = sum(p[1] for p in points) / len(points)
    scale = max(
        max(p[0] for p in points) - min(p[0] for p in points),
        max(p[1] for p in points) - min(p[1] for p in points),
        1.0,
    )
    tol = scale * 0.15
    matched = 0
    for x, y in points:
        mirror = (2 * cx - x, 2 * cy - y)
        if any(math.hypot(mirror[0] - ox, mirror[1] - oy) <= tol for ox, oy in points):
            matched += 1
    return matched / len(points)


def _outside_bbox_circles(circles: list[dict], bbox: dict, tolerance: float) -> list[str]:
    outside = []
    for circle in circles:
        center = circle.get("center") or {}
        try:
            x = float(center["x"])
            y = float(center["y"])
            r = float(circle.get("radius") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if (
            x - r < float(bbox["x1"]) - tolerance
            or x + r > float(bbox["x2"]) + tolerance
            or y - r < float(bbox["y1"]) - tolerance
            or y + r > float(bbox["y2"]) + tolerance
        ):
            outside.append(str(circle.get("handle") or ""))
    return outside


def _dedupe_terminal_violations(violations: list[dict]) -> list[dict]:
    seen: set[tuple[str, str]] = set()
    result = []
    for violation in violations:
        key = (str(violation.get("object_id") or ""), str(violation.get("violation_type") or ""))
        if key in seen:
            continue
        seen.add(key)
        result.append(violation)
    return result


def _run_wire_overlap_checks(elements: list[dict], topology: dict, unit_factor: float) -> list[dict]:
    wire_handles = set((topology.get("summary") or {}).get("wire_filter", {}).get("wire_candidate_handles") or [])
    if not wire_handles:
        wire_handles = set(topology.get("wire_candidate_handles") or [])
    if not wire_handles:
        return []

    lines = []
    for entity in elements:
        handle = str(entity.get("handle") or "")
        if handle not in wire_handles:
            continue
        segment = _line_segment(entity)
        if segment:
            lines.append((handle, segment))

    violations: list[dict] = []
    reported: set[frozenset[str]] = set()
    for i, (ha, sa) in enumerate(lines):
        for hb, sb in lines[i + 1:]:
            key = frozenset({ha, hb})
            if key in reported:
                continue
            overlap = _collinear_overlap(sa, sb)
            if not overlap:
                continue
            reported.add(key)
            exact_duplicate = overlap.get("duplicate", False)
            violations.append({
                "object_id": ha,
                "handle_b": hb,
                "violation_type": "wire_overlap",
                "category": "topology_qa",
                "severity": "warning",
                "reason": "전선 후보 선분이 서로 겹쳐 배선 경로 확인이 필요합니다.",
                "legal_reference": "전기 topology QA",
                "suggestion": "중복된 선분은 정리하고, 의도된 병렬 배선이면 레이어나 주석으로 구분하십시오.",
                "current_value": "wire segment overlap",
                "required_value": "non-overlapping or clearly annotated route",
                "target_handles": [ha, hb],
                "affected_handles": [ha, hb],
                "overlap_segment": overlap.get("segment"),
                "duplicate": exact_duplicate,
                "auto_fix_kind": "duplicate_cleanup" if exact_duplicate else "manual_review",
            })
    return violations


def _collapse_concentric_circles(circle_list: list[dict]) -> list[dict]:
    """동심원 쌍에서 내부 원을 제거하고 외부 원만 반환한다 (접지봉 심볼 이중원 처리)."""
    if len(circle_list) <= 1:
        return circle_list
    cr_data = [(e, _circle_center_radius(e)) for e in circle_list]
    cr_data = [(e, cr) for e, cr in cr_data if cr]
    cr_data.sort(key=lambda x: x[1][1], reverse=True)  # 반지름 큰 순
    kept: list[tuple[dict, tuple[tuple[float, float], float]]] = []
    for e, (center, radius) in cr_data:
        is_inner = any(
            k_radius > radius * 1.15
            and math.hypot(center[0] - k_center[0], center[1] - k_center[1]) <= max(k_radius * 0.15, 1.0)
            for _, (k_center, k_radius) in kept
        )
        if not is_inner:
            kept.append((e, (center, radius)))
    return [e for e, _ in kept]


def _run_grounding_rod_spacing_check(
    elements: list[dict],
    unit_factor: float,
    qa_reference_elements: list[dict] | None = None,
) -> list[dict]:
    """KEC 140.6 — 봉형 접지극 이격거리 검사.

    텍스트 레이블에서 봉 길이(L)를 추출하고, 심볼 간격이 L×2 미만이면 위반.
    """
    all_elements = [*elements, *(qa_reference_elements or [])]

    text_entities = [
        (e, _entity_point(e), _entity_text(e))
        for e in all_elements
        if _etype(e) in {"TEXT", "MTEXT"} and _entity_text(e)
    ]
    if not text_entities:
        return []

    circle_candidates = [
        e for e in elements
        if _etype(e) == "CIRCLE" and _circle_center_radius(e)
    ]

    violations: list[dict] = []
    processed: set[frozenset[str]] = set()

    for _anchor_e, anchor_pos, anchor_txt in text_entities:
        if not anchor_pos or not _GROUNDING_ROD_TEXT_RE.search(anchor_txt):
            continue

        len_match = _GROUNDING_ROD_LENGTH_RE.search(anchor_txt)
        if not len_match:
            continue  # 봉 길이 정보 없으면 검사 불가

        rod_length_mm = float(len_match.group(1))
        min_spacing_mm = rod_length_mm * 2.0  # KEC 140.6

        ax, ay = anchor_pos
        search_r = max(min_spacing_mm * 2.0, 6000.0) / max(unit_factor, 1e-9)

        nearby = [
            e for e in circle_candidates
            if (cr := _circle_center_radius(e)) and
            math.hypot(cr[0][0] - ax, cr[0][1] - ay) <= search_r
        ]
        if len(nearby) < 2:
            continue

        rods = _collapse_concentric_circles(nearby)
        if len(rods) < 2:
            continue

        cluster_key = frozenset(str(e.get("handle") or "") for e in rods)
        if cluster_key in processed:
            continue
        processed.add(cluster_key)

        # 봉 중심 목록 및 최소 이격거리 계산
        rod_centers: list[tuple[float, float, str]] = []
        for r in rods:
            cr2 = _circle_center_radius(r)
            if cr2:
                (cx, cy), _ = cr2
                rod_centers.append((cx, cy, str(r.get("handle") or "")))

        min_dist_mm: float | None = None
        for i in range(len(rod_centers)):
            for j in range(i + 1, len(rod_centers)):
                d = math.hypot(
                    rod_centers[i][0] - rod_centers[j][0],
                    rod_centers[i][1] - rod_centers[j][1],
                ) * unit_factor
                if min_dist_mm is None or d < min_dist_mm:
                    min_dist_mm = d

        if min_dist_mm is None or min_dist_mm >= min_spacing_mm * 0.9:
            continue

        handles = [str(e.get("handle") or "") for e in rods if e.get("handle")]
        violations.append({
            "object_id": handles[0] if handles else "",
            "violation_type": "grounding_rod_spacing_violation",
            "category": "semantic_qa",
            "severity": "Major",
            "reason": (
                f"접지봉(봉 길이 {rod_length_mm:.0f}mm) 간 이격거리 {min_dist_mm:.0f}mm가 "
                f"KEC 140.6 기준({min_spacing_mm:.0f}mm = 봉 길이×2) 미만입니다."
            ),
            "legal_reference": "KEC 140.6 접지극 시설",
            "suggestion": (
                f"봉형 접지극 이격거리를 봉 길이({rod_length_mm:.0f}mm)의 2배인 "
                f"{min_spacing_mm:.0f}mm 이상으로 재배치하십시오."
            ),
            "current_value": f"{min_dist_mm:.0f}mm",
            "required_value": f"≥{min_spacing_mm:.0f}mm",
            "affected_handles": handles,
        })

    return violations




def _run_outlet_height_check(
    elements: list[dict],
    unit_factor: float,
    qa_reference_elements: list[dict] | None = None,
) -> list[dict]:
    """KEC 232.56 — 콘센트 설치 높이(MH) 기준 검사.

    텍스트에서 'MH:Nnn' 패턴을 찾아 최솟값 미달 여부를 판정한다.
    위험 구역(보일러실 등) 인근이면 더 높은 기준(500mm)을 적용한다.
    레이어명에 무관하게 모든 텍스트 엔티티를 검색한다.
    """
    all_elements = [*elements, *(qa_reference_elements or [])]
    text_entities = [
        (e, _entity_point(e), _entity_text(e))
        for e in all_elements
        if _etype(e) in {"TEXT", "MTEXT"} and _entity_text(e)
    ]

    violations: list[dict] = []
    reported: set[str] = set()

    for anchor_e, anchor_pos, anchor_txt in text_entities:
        if not anchor_pos:
            continue
        mh_match = _MH_VALUE_RE.search(anchor_txt)
        if not mh_match:
            continue

        mh_value = int(mh_match.group(1))
        h = str(anchor_e.get("handle") or "")
        if h in reported:
            continue

        # 위험 구역 여부: 텍스트 자체 또는 인근 3m 내 위험 키워드
        is_hazardous = bool(_HAZARDOUS_AREA_TEXT_RE.search(anchor_txt))
        if not is_hazardous:
            ax, ay = anchor_pos
            sr = 3000.0 / max(unit_factor, 1e-9)
            for _, pos2, txt2 in text_entities:
                if pos2 and math.hypot(pos2[0] - ax, pos2[1] - ay) <= sr:
                    if _HAZARDOUS_AREA_TEXT_RE.search(txt2):
                        is_hazardous = True
                        break

        min_mh = _MH_MIN_HAZARDOUS if is_hazardous else _MH_MIN_GENERAL

        if mh_value < min_mh:
            reported.add(h)
            zone = "위험 구역(가연성 가스)" if is_hazardous else "일반"
            violations.append({
                "object_id": h,
                "violation_type": "outlet_height_violation",
                "category": "semantic_qa",
                "severity": "Major",
                "reason": (
                    f"콘센트 설치 높이 MH:{mh_value}mm가 {zone} 최솟값 "
                    f"{min_mh}mm 미만입니다. KEC 232.56 위반."
                ),
                "legal_reference": "KEC 232.56 콘센트의 시설",
                "suggestion": (
                    f"콘센트를 바닥에서 {min_mh}mm 이상 높이에 설치하십시오."
                ),
                "current_value": f"MH:{mh_value}mm",
                "required_value": f"MH≥{min_mh}mm",
                "affected_handles": [h] if h else [],
            })

    return violations


def _run_wire_count_check(
    elements: list[dict],
    unit_factor: float,
    qa_reference_elements: list[dict] | None = None,
) -> list[dict]:
    """전선 가닥 수 표기 검사.

    ①-⑨ 원문자 텍스트를 찾고, 인근에 회로 타입 표기가 있으면 기대 가닥 수와 비교.
    회로 타입 표기가 없어도 ①(1가닥)은 AC 회로 최솟값 위반으로 무조건 잡는다.
    레이어명에 무관하게 모든 텍스트 엔티티를 검색한다.
    """
    all_elements = [*elements, *(qa_reference_elements or [])]
    text_entities = [
        (e, _entity_point(e), _entity_text(e))
        for e in all_elements
        if _etype(e) in {"TEXT", "MTEXT"} and _entity_text(e)
    ]

    violations: list[dict] = []
    reported: set[str] = set()

    for anchor_e, anchor_pos, anchor_txt in text_entities:
        if not anchor_pos:
            continue

        circle_match = _WIRE_CIRCLE_NUM_RE.search(anchor_txt)
        if not circle_match:
            continue

        wire_count = _WIRE_CIRCLE_MAP[circle_match.group()]
        h = str(anchor_e.get("handle") or "")
        if h in reported:
            continue

        ax, ay = anchor_pos
        sr = 5000.0 / max(unit_factor, 1e-9)

        # 인근 텍스트 blob에서 회로 타입 탐색
        nearby_blob = " ".join(
            txt for _, pos, txt in text_entities
            if pos and math.hypot(pos[0] - ax, pos[1] - ay) <= sr
        )
        ct_match = _CIRCUIT_TYPE_RE.search(nearby_blob)
        expected: int | None = None
        circuit_label = ""
        if ct_match:
            phase_raw = ct_match.group("phase").replace("φ", "φ").replace("Φ", "φ")
            wire_raw  = ct_match.group("wire")
            # 정규화: 단상→1φ, 3상→3φ
            phase_key = "1φ" if "단상" in phase_raw or "1" in phase_raw else "3φ"
            lookup_key = f"{phase_key}{wire_raw}"
            expected = _CIRCUIT_WIRE_COUNT.get(lookup_key)
            circuit_label = ct_match.group()

        # 판정 1: ①은 AC 회로 불가 (최솟값 위반)
        if wire_count == 1:
            reported.add(h)
            violations.append({
                "object_id": h,
                "violation_type": "wire_count_violation",
                "category": "semantic_qa",
                "severity": "Critical",
                "reason": (
                    "배선 가닥 수 ①(1가닥) 표기는 AC 회로에서 불가합니다. "
                    "단상 2선식 최소 2가닥이 필요합니다."
                ),
                "legal_reference": "KEC 232 배선설비",
                "suggestion": "최소 ②(2가닥) 이상으로 수정하십시오.",
                "current_value": f"①(1가닥)",
                "required_value": "②(2가닥) 이상",
                "affected_handles": [h] if h else [],
            })
            continue

        # 판정 2: 회로 타입이 있고 기대값과 불일치
        if expected is not None and wire_count != expected:
            reported.add(h)
            violations.append({
                "object_id": h,
                "violation_type": "wire_count_violation",
                "category": "semantic_qa",
                "severity": "Major",
                "reason": (
                    f"배선 가닥 수 표기 {circle_match.group()}({wire_count}가닥)가 "
                    f"회로 타입 '{circuit_label}' 기준 기대값({expected}가닥)과 불일치합니다."
                ),
                "legal_reference": "KEC 232 배선설비",
                "suggestion": (
                    f"'{circuit_label}' 회로에 맞는 가닥 수 {expected}가닥으로 수정하십시오."
                ),
                "current_value": f"{circle_match.group()}({wire_count}가닥)",
                "required_value": f"{expected}가닥",
                "affected_handles": [h] if h else [],
            })

    return violations


def _run_grounding_rod_count_check(
    elements: list[dict],
    unit_factor: float,
    qa_reference_elements: list[dict] | None = None,
) -> list[dict]:
    """접지봉 수량 표기(E태그·xNEA 주석)와 실제 심볼 개수 불일치 검사.

    탐지 흐름:
      1. '접지봉' 키워드를 포함한 TEXT 앵커를 찾는다 (elements + qa_reference_elements 모두 검색).
      2. 앵커 주변의 접지봉 심볼을 수집한다.
         - CIRCLE: 동심원 쌍을 합쳐서 실제 봉 개수를 센다.
         - BLOCK/INSERT: block_name·layer에 '접지봉' 키워드가 있는 것만 센다.
      3. 같은 반경의 모든 텍스트에서 E-태그(E2→2) 및 수량 표기(x3EA→3)를 파싱한다.
      4. 실제 개수 vs E-번호 vs xNEA 중 불일치가 있으면 위반으로 기록한다.
    """
    all_elements = [*elements, *(qa_reference_elements or [])]

    text_entities = [
        (e, _entity_point(e), _entity_text(e))
        for e in all_elements
        if _etype(e) in {"TEXT", "MTEXT"} and _entity_text(e)
    ]
    if not text_entities:
        return []

    # 심볼 후보: CIRCLE 또는 접지봉 관련 BLOCK
    sym_candidates = [
        e for e in elements  # elements만 대상 (실제 심볼은 arch_ref에 없음)
        if _etype(e) in {"CIRCLE", "INSERT", "BLOCK"}
    ]

    violations: list[dict] = []
    processed: set[frozenset[str]] = set()

    for _anchor_e, anchor_pos, anchor_txt in text_entities:
        if not anchor_pos or not _GROUNDING_ROD_TEXT_RE.search(anchor_txt):
            continue

        ax, ay = anchor_pos
        search_r = 5000.0 / max(unit_factor, 1e-9)

        # 근처 심볼 수집
        nearby_circles: list[dict] = []
        nearby_blocks: list[dict] = []
        for ce in sym_candidates:
            pos = _entity_point(ce)
            if not pos or math.hypot(pos[0] - ax, pos[1] - ay) > search_r:
                continue
            etype = _etype(ce)
            if etype == "CIRCLE":
                if _circle_center_radius(ce):
                    nearby_circles.append(ce)
            elif etype in {"INSERT", "BLOCK"}:
                name_blob = " ".join([
                    str(ce.get("block_name") or ""),
                    str(ce.get("effective_name") or ""),
                    str(ce.get("layer") or ""),
                ])
                if _GROUNDING_ROD_TEXT_RE.search(name_blob):
                    nearby_blocks.append(ce)

        # 블록이 있으면 블록 기준, 없으면 원 기준
        if nearby_blocks:
            symbols = nearby_blocks
            actual = len(symbols)
        elif len(nearby_circles) >= 2:
            symbols = _collapse_concentric_circles(nearby_circles)
            actual = len(symbols)
        else:
            continue

        cluster_key = frozenset(str(e.get("handle") or "") for e in symbols)
        if cluster_key in processed:
            continue
        processed.add(cluster_key)

        # 근처 텍스트 blob (reference 포함)
        nearby_texts = [
            txt for _e, pos, txt in text_entities
            if pos and math.hypot(pos[0] - ax, pos[1] - ay) <= search_r
        ]
        blob = " ".join(nearby_texts)

        e_match = _GROUNDING_E_LABEL_RE.search(blob)
        e_num = int(e_match.group(1)) if e_match else None
        qty_match = _GROUNDING_QTY_RE.search(blob)
        qty_num = int(qty_match.group(1)) if qty_match else None

        if e_num is None and qty_num is None:
            continue

        mismatches: list[str] = []
        if e_num is not None and e_num != actual:
            mismatches.append(f"E{e_num} 태그 기대({e_num}개) ≠ 실제 심볼({actual}개)")
        if qty_num is not None and qty_num != actual:
            mismatches.append(f"수량 표기({qty_num}EA) ≠ 실제 심볼({actual}개)")
        if e_num is not None and qty_num is not None and e_num != qty_num:
            mismatches.append(f"E{e_num} 태그 ≠ 수량 표기({qty_num}EA)")

        if not mismatches:
            continue

        handles = [str(e.get("handle") or "") for e in symbols if e.get("handle")]
        object_id = handles[0] if handles else ""

        # E-태그 엔티티 핸들 탐색: 가장 가까운 "E숫자" 텍스트 엔티티
        authoritative_count = qty_num if qty_num is not None else actual
        e_tag_handle: str | None = None
        e_tag_old: str | None = None
        if e_num is not None:
            best_dist = float("inf")
            cluster_cx = sum(_entity_point(e)[0] for e in symbols if _entity_point(e)) / max(actual, 1)
            cluster_cy = sum(_entity_point(e)[1] for e in symbols if _entity_point(e)) / max(actual, 1)
            for et_e, et_pos, et_txt in text_entities:
                if not et_pos or not _GROUNDING_E_LABEL_RE.search(et_txt):
                    continue
                if not _GROUNDING_E_LABEL_RE.search(et_txt):
                    continue
                dist = math.hypot(et_pos[0] - cluster_cx, et_pos[1] - cluster_cy)
                if dist < best_dist:
                    best_dist = dist
                    e_tag_handle = str(et_e.get("handle") or "")
                    e_tag_old = f"E{e_num}"

        violations.append({
            "object_id": object_id,
            "violation_type": "grounding_rod_count_mismatch",
            "category": "semantic_qa",
            "severity": "Major",
            "reason": f"접지봉 수량 불일치: {'; '.join(mismatches)}.",
            "legal_reference": "전기 설비 의미 QA",
            "suggestion": "E 태그 번호, 수량 주석(xNEA), 실제 접지봉 심볼 개수를 모두 일치시키십시오.",
            "current_value": (
                f"심볼 {actual}개"
                + (f" / E{e_num}" if e_num is not None else "")
                + (f" / {qty_num}EA" if qty_num is not None else "")
            ),
            "required_value": "E태그·수량주석·심볼 개수 일치",
            "affected_handles": handles,
            "target_handles": handles,
            "e_tag_handle": e_tag_handle,
            "e_tag_old": e_tag_old,
            "e_tag_new": f"E{authoritative_count}" if e_tag_handle else None,
        })

    return violations


def _run_electrical_symbol_overlap_checks(
    elements: list[dict],
    qa_context: dict,
    unit_factor: float,
) -> list[dict]:
    """전기 심볼 역할을 가진 원형 객체끼리 실제 면적이 겹치는지 검사한다."""
    circle_contexts = qa_context.get("circle_contexts") or {}
    candidates: list[dict] = []
    for entity in elements:
        if _etype(entity) != "CIRCLE":
            continue
        handle = str(entity.get("handle") or "")
        cr = _circle_center_radius(entity)
        if not handle or not cr:
            continue
        context = circle_contexts.get(handle) or {}
        if not _is_electrical_circle_symbol(entity, context):
            continue
        candidates.append(entity)
    candidates = [
        entity for entity in candidates
        if not _has_concentric_outer_circle(entity, candidates, unit_factor)
    ]

    violations: list[dict] = []
    reported: set[frozenset[str]] = set()
    tol = _CIRCLE_OVERLAP_TOL / max(unit_factor, 1e-9)
    for i, ca in enumerate(candidates):
        data_a = _circle_center_radius(ca)
        if not data_a:
            continue
        (ax, ay), ar = data_a
        for cb in candidates[i + 1:]:
            data_b = _circle_center_radius(cb)
            if not data_b:
                continue
            (bx, by), br = data_b
            center_distance = math.hypot(ax - bx, ay - by)
            overlap_depth = ar + br - center_distance
            if overlap_depth <= tol:
                continue
            if _is_nested_composite_circle((ax, ay), ar, (bx, by), br, tol):
                continue

            ha = str(ca.get("handle") or "")
            hb = str(cb.get("handle") or "")
            key = frozenset({ha, hb})
            if key in reported:
                continue
            reported.add(key)

            target_bbox = _circle_bbox_dict((ax, ay), ar)
            ref_bbox = _circle_bbox_dict((bx, by), br)
            violations.append({
                "object_id": ha,
                "handle_b": hb,
                "violation_type": "electrical_symbol_overlap",
                "category": "geometry_qa",
                "severity": "warning",
                "reason": "전기 원형 심볼끼리 겹쳐 배치되어 심볼 또는 단자 판독이 불안정합니다.",
                "legal_reference": "전기 설비 Geometry QA",
                "reference_rule": "전기 설비 Geometry QA",
                "suggestion": "겹친 원형 심볼 중복 여부를 확인하고 의도되지 않은 객체는 이동하거나 정리하십시오.",
                "current_value": f"circle overlap depth={round(overlap_depth * unit_factor, 3)}",
                "required_value": "non-overlapping electrical symbol placement",
                "target_handles": [ha, hb],
                "affected_handles": [ha, hb],
                "target_bbox": target_bbox,
                "ref_bbox": ref_bbox,
                "bbox": _union_bbox_dict(target_bbox, ref_bbox),
            })
    return violations


def _line_segment(entity: dict) -> tuple[tuple[float, float], tuple[float, float]] | None:
    start = entity.get("start") or entity.get("start_point")
    end = entity.get("end") or entity.get("end_point")
    if not isinstance(start, dict) or not isinstance(end, dict):
        return None
    try:
        return (float(start["x"]), float(start["y"])), (float(end["x"]), float(end["y"]))
    except (KeyError, TypeError, ValueError):
        return None


def _collinear_overlap(
    a: tuple[tuple[float, float], tuple[float, float]],
    b: tuple[tuple[float, float], tuple[float, float]],
) -> dict | None:
    (ax1, ay1), (ax2, ay2) = a
    (bx1, by1), (bx2, by2) = b
    av = (ax2 - ax1, ay2 - ay1)
    bv = (bx2 - bx1, by2 - by1)
    alen = math.hypot(av[0], av[1])
    blen = math.hypot(bv[0], bv[1])
    if alen <= 1e-9 or blen <= 1e-9:
        return None
    cross_dir = abs(av[0] * bv[1] - av[1] * bv[0])
    if cross_dir > max(alen * blen * 0.01, 1e-6):
        return None
    cross_pos = abs(av[0] * (by1 - ay1) - av[1] * (bx1 - ax1))
    if cross_pos / alen > 2.0:
        return None

    ux, uy = av[0] / alen, av[1] / alen
    a0, a1 = 0.0, alen
    b0 = (bx1 - ax1) * ux + (by1 - ay1) * uy
    b1 = (bx2 - ax1) * ux + (by2 - ay1) * uy
    lo = max(min(a0, a1), min(b0, b1))
    hi = min(max(a0, a1), max(b0, b1))
    if hi - lo <= 2.0:
        return None
    p0 = {"x": round(ax1 + ux * lo, 4), "y": round(ay1 + uy * lo, 4)}
    p1 = {"x": round(ax1 + ux * hi, 4), "y": round(ay1 + uy * hi, 4)}
    duplicate = abs(hi - lo - min(alen, blen)) <= 2.0 and abs(alen - blen) <= 2.0
    return {"segment": {"start": p0, "end": p1}, "duplicate": duplicate}


def _collect_context_reference_texts(
    elements: list[dict],
    reference_elements: list[dict],
    circles: list[dict],
    topology: dict,
    unit_factor: float,
) -> list[dict]:
    """Return reference texts that are safe to use only as ELEC QA evidence."""
    if not reference_elements or not circles:
        return []

    drawing_intent = str(
        (topology.get("summary") or {}).get("drawing_intent")
        or topology.get("drawing_intent")
        or ""
    )
    text_pool = [
        e for e in [*elements, *reference_elements]
        if _etype(e) in {"TEXT", "MTEXT"} and _entity_point(e) and _entity_text(e)
    ]
    circle_data = []
    for circle in circles:
        data = _circle_center_radius(circle)
        if data:
            circle_data.append((circle, data[0], data[1]))

    safe_texts: list[dict] = []
    seen: set[str] = set()
    for ref in reference_elements:
        if _etype(ref) not in {"TEXT", "MTEXT"}:
            continue
        text = _entity_text(ref)
        if not text or not _STANDALONE_TEXT_RE.search(text):
            continue
        pos = _entity_point(ref)
        if not pos:
            continue

        nearest = _nearest_circle_for_text(pos, circle_data, unit_factor)
        if not nearest:
            continue
        circle, distance = nearest
        cluster_count = _nearby_circle_count(pos, circle_data, unit_factor)
        is_test_text = bool(_TEST_TEXT_RE.search(text))
        has_grounding_context = (
            drawing_intent == "GROUNDING_PLAN"
            or bool(_GROUND_CONTEXT_TEXT_RE.search(text))
            or _has_nearby_grounding_context(pos, text_pool, unit_factor)
        )
        if is_test_text and (not has_grounding_context or cluster_count < 2):
            continue

        handle = str(ref.get("handle") or f"ref_text_{len(safe_texts)}")
        if handle in seen:
            continue
        seen.add(handle)

        row = dict(ref)
        row["elec_qa_reference_text"] = True
        row["elec_qa_reference_reason"] = (
            "test_near_circle_grounding_context"
            if is_test_text else "standalone_text_near_elec_circle"
        )
        row["elec_qa_near_circle_handle"] = str(circle.get("handle") or "")
        row["elec_qa_near_circle_distance"] = round(distance * unit_factor, 3)
        safe_texts.append(row)

    return safe_texts


def _nearest_circle_for_text(
    pos: tuple[float, float],
    circle_data: list[tuple[dict, tuple[float, float], float]],
    unit_factor: float,
) -> tuple[dict, float] | None:
    best: tuple[dict, float] | None = None
    for circle, center, radius in circle_data:
        if radius <= 0:
            continue
        distance = math.hypot(center[0] - pos[0], center[1] - pos[1])
        max_distance = max(radius * 8.0, 450.0 / max(unit_factor, 1e-9))
        if distance > max_distance:
            continue
        if best is None or distance < best[1]:
            best = (circle, distance)
    return best


def _nearby_circle_count(
    pos: tuple[float, float],
    circle_data: list[tuple[dict, tuple[float, float], float]],
    unit_factor: float,
) -> int:
    count = 0
    for _circle, center, radius in circle_data:
        if radius <= 0:
            continue
        max_distance = max(radius * 10.0, 550.0 / max(unit_factor, 1e-9))
        if math.hypot(center[0] - pos[0], center[1] - pos[1]) <= max_distance:
            count += 1
    return count


def _has_nearby_grounding_context(
    pos: tuple[float, float],
    text_pool: list[dict],
    unit_factor: float,
) -> bool:
    search = 1800.0 / max(unit_factor, 1e-9)
    for entity in text_pool:
        text = _entity_text(entity)
        if not text or not _GROUND_CONTEXT_TEXT_RE.search(text):
            continue
        other = _entity_point(entity)
        if other and math.hypot(other[0] - pos[0], other[1] - pos[1]) <= search:
            return True
    return False


def build_geometry_qa_context(
    elements: list[dict],
    topology: dict,
    unit_factor: float = 1.0,
    qa_reference_elements: list[dict] | None = None,
) -> dict:
    terminal_candidates = topology.get("terminal_candidates") or []
    assigned_handles = {
        h
        for cand in terminal_candidates
        for circle in cand.get("circles") or []
        for h in _expanded_circle_handles([circle])
    }
    all_circles = [e for e in elements if _etype(e) == "CIRCLE" and _circle_center_radius(e)]
    standards = compute_drawing_internal_standards(elements, terminal_candidates, unit_factor)
    context_reference_texts = _collect_context_reference_texts(
        elements,
        qa_reference_elements or [],
        all_circles,
        topology,
        unit_factor,
    )
    context_elements = [*elements, *context_reference_texts]
    contexts = {
        str(c.get("handle") or ""): classify_circle_context(c, context_elements, topology, standards)
        for c in all_circles
        if c.get("handle")
    }
    standalone = [
        {"handle": h, "category": ctx.get("symbol_category"), "reasons": ctx["reasons"], "nearby_texts": ctx["nearby_texts"]}
        for h, ctx in contexts.items()
        if is_standalone_electrical_symbol(ctx)
    ]
    grounding_symbols = [
        item for item in standalone
        if any("GROUND" in str(t).upper() or "GND" in str(t).upper() or "GRD" in str(t).upper() or "\uc811\uc9c0" in str(t)
               for t in item.get("nearby_texts", []))
        or "standalone_layer" in item.get("reasons", [])
    ]
    test_points = [
        item for item in standalone
        if any("TEST" in str(t).upper() or "E1" in str(t).upper() or "E2" in str(t).upper() or "\ud14c\uc2a4\ud2b8" in str(t)
               for t in item.get("nearby_texts", []))
    ]
    unassigned = [str(c.get("handle") or "") for c in all_circles if str(c.get("handle") or "") not in assigned_handles]
    terminal_debug = {
        "all_circle_count": len(all_circles),
        "terminal_candidate_count": len(terminal_candidates),
        "assigned_circle_handles": sorted(assigned_handles),
        "unassigned_circle_handles": sorted(h for h in unassigned if h),
        "standalone_symbol_candidates": standalone,
        "grounding_symbols": grounding_symbols,
        "test_points": test_points,
        "context_reference_texts": [
            {
                "handle": str(item.get("handle") or ""),
                "text": _entity_text(item),
                "reason": item.get("elec_qa_reference_reason"),
                "near_circle_handle": item.get("elec_qa_near_circle_handle"),
                "distance": item.get("elec_qa_near_circle_distance"),
            }
            for item in context_reference_texts
        ],
        "orphan_candidates": [],
        "suppressed_orphan_candidates": [],
        "filtered_reasons": [],
    }
    return {
        "circle_contexts": contexts,
        "drawing_internal_standards": standards,
        "terminal_debug": terminal_debug,
    }


def compute_drawing_internal_standards(
    elements: list[dict],
    terminal_candidates: list[dict],
    unit_factor: float = 1.0,
) -> dict:
    radii: list[float] = []
    spacings: list[float] = []
    alignment_errors: list[float] = []
    for cand in terminal_candidates or []:
        circles = cand.get("circles") or []
        pts: list[tuple[float, float]] = []
        for circle in circles:
            cr = _circle_center_radius(circle)
            if not cr:
                continue
            center, radius = cr
            pts.append(center)
            radii.append(radius)
        if len(pts) >= 2:
            spacings.append(_median_spacing(pts))
        if len(pts) == 4:
            expected = _expected_grid_points(pts)
            alignment_errors.extend(min(math.hypot(p[0] - e[0], p[1] - e[1]) for e in expected) for p in pts)

    radius_m = _median(radii)
    spacing_m = _median(spacings)
    align_m = _median(alignment_errors)
    align_mad = _mad(alignment_errors, align_m)
    fallback = max((spacing_m or 0) * _TERMINAL_GRID_TOL_RATIO, (radius_m or 0) * 1.5, 1.0)
    adaptive = max(align_m + align_mad * 3.0, fallback)
    return {
        "terminal_radius_median": round(radius_m * unit_factor, 4),
        "terminal_spacing_median": round(spacing_m * unit_factor, 4),
        "alignment_error_median": round(align_m * unit_factor, 4),
        "alignment_error_mad": round(align_mad * unit_factor, 4),
        "adaptive_alignment_threshold": round(adaptive * unit_factor, 4),
        "_raw_terminal_radius_median": radius_m,
        "_raw_terminal_spacing_median": spacing_m,
        "_raw_adaptive_alignment_threshold": adaptive,
    }


def classify_circle_context(circle: dict, elements: list[dict], topology: dict, standards: dict) -> dict:
    cr = _circle_center_radius(circle)
    handle = str(circle.get("handle") or "")
    layer = str(circle.get("layer") or "")
    if not cr:
        return {"handle": handle, "nearby_texts": [], "reasons": ["invalid_circle"], "confidence": 0.0}
    center, radius = cr
    search = max(radius * 8.0, (standards.get("_raw_terminal_spacing_median") or 0) * 0.8, 10.0)
    nearby_texts = []
    for el in elements:
        if _etype(el) not in {"TEXT", "MTEXT"}:
            continue
        pos = _entity_point(el)
        text = _entity_text(el)
        if pos and text and math.hypot(center[0] - pos[0], center[1] - pos[1]) <= search:
            nearby_texts.append(text)

    nearby_body = _has_nearby_body(center, radius, elements, search)
    same_pattern_count = _same_circle_pattern_count(circle, elements, standards)
    reasons: list[str] = []
    if nearby_texts and any(_STANDALONE_TEXT_RE.search(t) for t in nearby_texts):
        reasons.append("standalone_text")
    if _GROUND_TEST_LAYER_RE.search(layer):
        reasons.append("standalone_layer")
    if _LOW_SIGNAL_LAYER_RE.search(layer):
        reasons.append("low_signal_layer")
    if same_pattern_count >= 2:
        reasons.append("repeated_standalone_pattern")
    if nearby_body:
        reasons.append("nearby_body")
    symbol_category = _standalone_symbol_category(nearby_texts, layer)
    return {
        "handle": handle,
        "layer": layer,
        "center": {"x": center[0], "y": center[1]},
        "radius": radius,
        "symbol_category": symbol_category,
        "nearby_texts": nearby_texts,
        "nearby_body": nearby_body,
        "same_pattern_count": same_pattern_count,
        "reasons": reasons,
        "confidence": 0.0,
    }


def is_standalone_electrical_symbol(circle_context: dict) -> bool:
    reasons = set(circle_context.get("reasons") or [])
    return (
        "standalone_text" in reasons
        or "standalone_layer" in reasons
        or ("repeated_standalone_pattern" in reasons and "low_signal_layer" in reasons)
    )


def _standalone_symbol_category(nearby_texts: list[str], layer: str) -> str:
    blob = " ".join(nearby_texts + [layer]).upper()
    if "TEST" in blob:
        return "TEST_POINT"
    if "GROUND" in blob or "GND" in blob or "GRD" in blob or "EARTH" in blob or "\uc811\uc9c0" in blob:
        return "GROUND_POINT"
    if "ROD" in blob:
        return "GROUND_ROD"
    if "MEASURE" in blob or "\uce21\uc815" in blob:
        return "MEASUREMENT_POINT"
    if "E1" in blob or "E2" in blob:
        return "STANDALONE_NODE"
    return "STANDALONE_NODE"


def suppress_false_positive_orphan(finding: dict, context: dict) -> tuple[bool, str]:
    circle_context = finding.get("circle_context") or {}
    if is_standalone_electrical_symbol(circle_context):
        return True, "standalone electrical symbol context"
    if "low_signal_layer" in set(circle_context.get("reasons") or []):
        return True, "dimension/annotation/reference layer"
    confidence = float(finding.get("confidence") or 0.0)
    if confidence < 0.62:
        return True, "low orphan confidence"
    return False, ""


def normalize_geometry_qa_findings(raw_findings: list[dict], drawing_intent: str = "") -> list[dict]:
    violations: list[dict] = []
    for finding in raw_findings:
        vtype = str(finding.get("violation_type") or "")
        bbox = finding.get("bbox")
        handle = str(finding.get("handle") or "")
        if vtype == "terminal_orphan_circle":
            violations.append({
                "object_id": handle,
                "equipment_id": handle,
                "terminal_candidate_id": finding.get("terminal_candidate_id") or handle,
                "violation_type": vtype,
                "category": "geometry_qa",
                "severity": "info" if drawing_intent == "DETAIL_DRAWING" else "warning",
                "reason": "원형 단자가 주변 단자 패턴에서 분리된 것으로 보입니다.",
                "legal_reference": "전기 설비 Geometry QA",
                "reference_rule": "전기 설비 Geometry QA",
                "suggestion": "단자 배치가 의도된 독립 심볼인지 확인하십시오.",
                "current_value": "",
                "required_value": "",
                "affected_handles": [handle],
                "target_handles": [handle],
                "actual_center": finding.get("actual_center"),
                "bbox": bbox,
            })
    return violations


def _expected_4th_point(compact_3: list[tuple[float, float]]) -> tuple[float, float]:
    """Given 3 corners of a 2횞2 grid, return the expected 4th corner.

    Tries all 3 parallelogram completions (D = A+B-C etc.) and picks the one
    closest to the centroid of the 3 known points.  This works because the
    correct 4th corner lies "inside" the cluster, while the two wrong candidates
    always end up on the opposite side.
    """
    A, B, C = compact_3
    candidates: list[tuple[float, float]] = [
        (A[0] + B[0] - C[0], A[1] + B[1] - C[1]),
        (A[0] + C[0] - B[0], A[1] + C[1] - B[1]),
        (B[0] + C[0] - A[0], B[1] + C[1] - A[1]),
    ]
    gx = (A[0] + B[0] + C[0]) / 3
    gy = (A[1] + B[1] + C[1]) / 3
    return min(candidates, key=lambda d: math.hypot(d[0] - gx, d[1] - gy))


def _compute_alignment_moves(
    circle_data: list[tuple[dict, tuple[float, float], float]],
    expected: list[tuple[float, float]],
    tolerance: float,
) -> list[dict]:
    """Match each circle to its nearest unassigned expected grid point; return move instructions for misaligned ones."""
    assigned = [False] * len(expected)
    instructions: list[dict] = []
    for circle, actual_pt, _ in circle_data:
        candidates = [(i, expected[i]) for i in range(len(expected)) if not assigned[i]]
        if not candidates:
            continue
        best_i, exp_pt = min(candidates, key=lambda t: math.hypot(actual_pt[0] - t[1][0], actual_pt[1] - t[1][1]))
        assigned[best_i] = True
        if math.hypot(actual_pt[0] - exp_pt[0], actual_pt[1] - exp_pt[1]) <= tolerance:
            continue
        h = next((_h for _h in _expanded_circle_handles([circle]) if _h), "")
        instructions.append({
            "handle": h,
            "actual_center": {"x": round(actual_pt[0], 4), "y": round(actual_pt[1], 4)},
            "expected_center": {"x": round(exp_pt[0], 4), "y": round(exp_pt[1], 4)},
        })
    return instructions


def _attach_move_info(violation: dict, move_instructions: list[dict]) -> None:
    """Attach actual/expected center and target_handles to a violation dict in-place."""
    if not move_instructions:
        return
    violation["move_instructions"] = move_instructions
    worst = max(
        move_instructions,
        key=lambda i: math.hypot(
            i["expected_center"]["x"] - i["actual_center"]["x"],
            i["expected_center"]["y"] - i["actual_center"]["y"],
        ),
    )
    violation["actual_center"] = worst["actual_center"]
    violation["expected_center"] = worst["expected_center"]
    violation["target_handles"] = [i["handle"] for i in move_instructions if i["handle"]]


def _candidate_nearby_text_values(candidate: dict) -> list[str]:
    values: list[str] = []
    for item in candidate.get("nearby_texts") or []:
        if isinstance(item, dict):
            text = item.get("text") or item.get("value") or item.get("raw_text")
        else:
            text = item
        if text:
            values.append(str(text))
    return values


def _candidate_has_standalone_context(candidate: dict) -> bool:
    """Ground/test/E1/E2 circle strings are standalone symbols, not terminal blocks."""
    if any(_STANDALONE_TEXT_RE.search(text) for text in _candidate_nearby_text_values(candidate)):
        return True
    label = " ".join(str(candidate.get(k) or "") for k in ("label", "name", "category", "symbol_category"))
    return bool(label and _STANDALONE_TEXT_RE.search(label))


def _has_symmetric_peer(candidate: dict, all_candidates: list[dict]) -> bool:
    """동일한 circle count·bbox 크기의 후보가 같은 Y축 선상에 수평 반복될 때 True.

    이미지상 좌/우 동일 패턴처럼 반복 배치된 경우 detached circle 위반은 오탐이다.
    """
    n = candidate.get("circle_count", 0)
    bbox = candidate.get("bbox") or {}
    w = (bbox.get("x2") or 0.0) - (bbox.get("x1") or 0.0)
    h = (bbox.get("y2") or 0.0) - (bbox.get("y1") or 0.0)
    if n < 4 or w <= 0 or h <= 0:
        return False
    cx = ((bbox.get("x1") or 0.0) + (bbox.get("x2") or 0.0)) / 2
    cy = ((bbox.get("y1") or 0.0) + (bbox.get("y2") or 0.0)) / 2
    for other in all_candidates:
        if other is candidate:
            continue
        if other.get("circle_count", 0) != n:
            continue
        ob = other.get("bbox") or {}
        ow = (ob.get("x2") or 0.0) - (ob.get("x1") or 0.0)
        oh = (ob.get("y2") or 0.0) - (ob.get("y1") or 0.0)
        if w > 0 and abs(ow - w) > w * 0.3:
            continue
        if h > 0 and abs(oh - h) > h * 0.3:
            continue
        ocx = ((ob.get("x1") or 0.0) + (ob.get("x2") or 0.0)) / 2
        ocy = ((ob.get("y1") or 0.0) + (ob.get("y2") or 0.0)) / 2
        # 같은 Y 라인에서 수평으로 떨어진 반복 패턴
        if abs(ocy - cy) < h * 0.5 and abs(ocx - cx) > w * 0.5:
            return True
    return False


def _record_suppressed_terminal_candidate(candidate: dict, qa_context: dict | None, reason: str) -> None:
    if not qa_context:
        return
    terminal_debug = qa_context.get("terminal_debug")
    if not isinstance(terminal_debug, dict):
        return
    handles = _expanded_circle_handles(candidate.get("circles") or [])
    terminal_debug.setdefault("suppressed_orphan_candidates", []).append({
        "handle": ",".join(h for h in handles if h),
        "reason": reason,
        "nearby_texts": _candidate_nearby_text_values(candidate),
    })
    terminal_debug.setdefault("filtered_reasons", []).append(reason)


def _run_terminal_geometry_qa(topology: dict, unit_factor: float, qa_context: dict | None = None) -> list[dict]:
    violations: list[dict] = []
    all_candidates: list[dict] = topology.get("terminal_candidates", []) or []
    for candidate in all_candidates:
        circles = candidate.get("circles") or []
        if len(circles) < 4:
            continue
        if _candidate_has_standalone_context(candidate):
            _record_suppressed_terminal_candidate(candidate, qa_context, "standalone grounding/test label near circle cluster")
            continue

        body_bbox = candidate.get("body_bbox") or candidate.get("qa_body_bbox")
        pattern_source = str(candidate.get("expected_pattern_source") or "").lower()
        has_explicit_reference = bool(body_bbox) or pattern_source in {"baseline", "template", "block_definition"}
        if not has_explicit_reference:
            # A single detail drawing does not prove that a circle cluster must be
            # a perfect 2x2 grid. Without an explicit body/template/baseline,
            # flagging symmetry or inferred-grid drift is too aggressive. The one
            # safe local check is a detached circle: 3 terminals form a compact
            # majority area and one same-pattern circle is clearly outside it.

            # 동일 크기·개수의 cluster가 수평 반복되면 정상 반복 패턴 → 오탐 억제
            if _has_symmetric_peer(candidate, all_candidates):
                _record_suppressed_terminal_candidate(
                    candidate, qa_context,
                    "symmetric peer cluster detected — repeated pattern, not detached terminal"
                )
                continue

            detached = _detached_circle_violation(candidate, unit_factor)
            if detached:
                violations.append(detached)
            continue

        handles = _expanded_circle_handles(circles)
        # Keep circle-to-point correspondence for per-handle move instruction generation
        circle_data: list[tuple[dict, tuple[float, float], float]] = []
        for circle in circles:
            center = circle.get("center") or {}
            try:
                circle_data.append((circle, (float(center["x"]), float(center["y"])), float(circle.get("radius") or 0)))
            except (KeyError, TypeError, ValueError):
                continue
        if len(circle_data) < 4:
            continue

        points = [pt for _, pt, _ in circle_data]
        radii = [r for _, _, r in circle_data]

        object_id = next((h for h in handles if h), "terminal_candidate")
        candidate_id = ",".join(h for h in handles if h) or object_id
        bbox = body_bbox or candidate.get("bbox") or _points_bbox(points, radii)
        expected = _expected_grid_points(points)
        expected_bbox = body_bbox or (_points_bbox(expected, radii) if expected else bbox)
        spacing = _median_spacing(points)
        tolerance = max(spacing * _TERMINAL_GRID_TOL_RATIO, (max(radii) if radii else 0) * 1.5, 1.0)
        max_dev = _max_grid_deviation(points, expected)
        symmetry = float(candidate.get("symmetry_score") or _symmetry_score(points))

        outside = _outside_bbox_circles(circles, expected_bbox, tolerance * 0.5)
        if outside:
            outside_bbox = _bbox_for_circle_handles(circles, outside) or expected_bbox
            outside_set = set(outside)
            v = _terminal_violation(
                object_id,
                candidate_id,
                "terminal_outside_body",
                "원형 단자가 기준 배치 영역 밖으로 벗어났습니다.",
                outside,
                current_value=f"outside_handles={outside}",
                required_value="모든 원형 단자는 기준 단자 영역 안에 있어야 합니다.",
                bbox=outside_bbox,
                target_bbox=expected_bbox,
            )
            move_instrs = _compute_alignment_moves(
                [(c, pt, r) for c, pt, r in circle_data
                 if any(h in outside_set for h in _expanded_circle_handles([c]))],
                expected, tolerance=0.0,
            )
            _attach_move_info(v, move_instrs)
            violations.append(v)

        if symmetry < _TERMINAL_SYMMETRY_MIN:
            violations.append(_terminal_violation(
                object_id,
                candidate_id,
                "terminal_symmetry_broken",
                "원형 단자 배열의 대칭성이 기준 패턴과 다릅니다.",
                handles,
                current_value="단자 배열 대칭성 낮음",
                required_value="대칭 배열 유지",
                bbox=bbox,
                target_bbox=expected_bbox,
            ))

        if max_dev > tolerance:
            v = _terminal_violation(
                object_id,
                candidate_id,
                "terminal_alignment_error",
                "원형 단자가 기준 배열 위치에서 벗어났습니다.",
                handles,
                current_value="기준 배열 위치 이탈",
                required_value="기준 배열 위치",
                bbox=bbox,
                target_bbox=expected_bbox,
            )
            move_instrs = _compute_alignment_moves(circle_data, expected, tolerance)
            _attach_move_info(v, move_instrs)
            violations.append(v)

        spacing_cv = _spacing_cv(points)
        if spacing_cv > 0.35:
            violations.append(_terminal_violation(
                object_id,
                candidate_id,
                "terminal_spacing_inconsistent",
                "원형 단자 간격 패턴이 일정하지 않습니다.",
                handles,
                current_value="단자 간격 불균일",
                required_value="일정한 단자 간격",
                bbox=bbox,
                target_bbox=expected_bbox,
            ))

    return _dedupe_terminal_violations(violations)


def _terminal_violation(
    object_id: str,
    candidate_id: str,
    violation_type: str,
    reason: str,
    handles: list[str],
    *,
    current_value: str,
    required_value: str,
    bbox: dict | None = None,
    target_bbox: dict | None = None,
) -> dict:
    row = {
        "object_id": object_id,
        "equipment_id": object_id,
        "terminal_candidate_id": candidate_id,
        "violation_type": violation_type,
        "category": "geometry_qa",
        "severity": "warning",
        "reason": reason,
        "legal_reference": "전기 설비 Geometry QA",
        "reference_rule": "전기 설비 Geometry QA",
        "suggestion": "단자 유닛의 배치가 의도된 위치인지 확인하십시오.",
        "current_value": current_value,
        "required_value": required_value,
        "affected_handles": handles,
    }
    normalized_bbox = _normalize_bbox(bbox)
    normalized_target_bbox = _normalize_bbox(target_bbox or bbox)
    readable_reasons = {
        "terminal_detached_circle": "단자 유닛이 주변 단자 배열에서 벗어났습니다.",
        "terminal_orphan_circle": "원형 단자가 주변 단자 패턴에서 분리된 것으로 보입니다.",
        "terminal_outside_body": "원형 단자가 단자 housing 영역 밖에 있어 배치 확인이 필요합니다.",
        "terminal_alignment_error": "단자 유닛이 기준 배열 위치에서 벗어났습니다.",
        "terminal_symmetry_broken": "단자 배열의 대칭성이 기준 패턴과 다릅니다.",
        "terminal_spacing_inconsistent": "단자 간격 패턴이 일정하지 않습니다.",
    }
    row["reason"] = readable_reasons.get(violation_type, row["reason"])
    row["legal_reference"] = "전기 설비 Geometry QA"
    row["reference_rule"] = "전기 설비 Geometry QA"
    row["suggestion"] = "단자 유닛의 배치가 의도된 위치인지 확인하십시오."
    if normalized_bbox:
        row["bbox"] = normalized_bbox
        row["target_bbox"] = normalized_target_bbox or normalized_bbox
        row["midpoint"] = _bbox_midpoint(normalized_bbox)
    return row


def _detached_circle_violation(candidate: dict, unit_factor: float) -> dict | None:
    if _candidate_has_standalone_context(candidate):
        return None

    circles = candidate.get("circles") or []
    n = len(circles)
    # Handle exactly-4 (classic detached) and 5 (4-circle proper cluster + 1 orphan got grouped in)
    if n not in (4, 5):
        return None

    # 모든 circle에 housing body 요소가 존재하면 compound 장식 심볼 → 단순 기하 outlier 판단 금지
    circle_body_map: dict[str, list] = candidate.get("circle_body_map") or {}
    handles_in_cluster = [str(c.get("handle") or "") for c in circles]
    circles_with_bodies = sum(1 for h in handles_in_cluster if circle_body_map.get(h))
    if circles_with_bodies == len(circles):
        return None

    parsed: list[tuple[int, dict, tuple[float, float], float]] = []
    for idx, circle in enumerate(circles):
        center = circle.get("center") or {}
        try:
            parsed.append((idx, circle, (float(center["x"]), float(center["y"])), float(circle.get("radius") or 0)))
        except (KeyError, TypeError, ValueError):
            continue
    if len(parsed) != n:
        return None

    radii = [r for _, _, _, r in parsed]
    median_radius = sorted(radii)[len(radii) // 2]
    if median_radius <= 0:
        return None
    if max(abs(r - median_radius) for r in radii) > median_radius * 0.35:
        return None

    points = [p for _, _, p, _ in parsed]
    global_spacing = _median_spacing(points)
    if global_spacing <= 0:
        return None

    best: tuple[float, int, dict, list[int]] | None = None
    for excluded_idx, _circle, point, _radius in parsed:
        majority = [(idx, p, r) for idx, _c, p, r in parsed if idx != excluded_idx]
        majority_points = [p for _idx, p, _r in majority]
        majority_radii = [r for _idx, _p, r in majority]
        majority_bbox = _points_bbox(majority_points, majority_radii)
        area = max(majority_bbox["x2"] - majority_bbox["x1"], 0) * max(majority_bbox["y2"] - majority_bbox["y1"], 0)
        if best is None or area < best[0]:
            best = (area, excluded_idx, majority_bbox, [idx for idx, _p, _r in majority])

    if best is None:
        return None

    _area, outlier_idx, majority_bbox, majority_idxs = best
    outlier = next(item for item in parsed if item[0] == outlier_idx)
    _idx, outlier_circle, outlier_point, outlier_radius = outlier

    compact_points = [p for idx, _c, p, _r in parsed if idx in majority_idxs]
    compact_width = max(p[0] for p in compact_points) - min(p[0] for p in compact_points)
    compact_height = max(p[1] for p in compact_points) - min(p[1] for p in compact_points)
    if min(compact_width, compact_height) <= median_radius * 2.0:
        return None

    compact_spacing = _median_spacing(compact_points)
    if compact_spacing <= 0 or compact_spacing > global_spacing * 1.25:
        return None

    margin = max(median_radius * 1.75, compact_spacing * 0.25)
    outside_majority = not _point_in_bbox(outlier_point, majority_bbox, margin)
    if not outside_majority:
        return None

    majority_center = (
        sum(p[0] for p in compact_points) / len(compact_points),
        sum(p[1] for p in compact_points) / len(compact_points),
    )
    distance = math.hypot(outlier_point[0] - majority_center[0], outlier_point[1] - majority_center[1])
    if distance <= compact_spacing * 1.35:
        return None

    handles = _expanded_circle_handles(circles)
    outlier_handles = _expanded_circle_handles([outlier_circle])
    object_id = next((h for h in outlier_handles if h), next((h for h in handles if h), "terminal_candidate"))
    candidate_id = ",".join(h for h in handles if h) or object_id
    outlier_bbox = _bbox_for_circle_handles(circles, outlier_handles) or _points_bbox([outlier_point], [outlier_radius])

    # Use only the 3 majority points to reconstruct the correct 4th grid corner.
    # Using all 4 points (including the outlier) biases _expected_grid_points and
    # produces a wrong target position.
    expected_center = _expected_4th_point(compact_points)

    # Collect body geometry (lines/polylines) belonging to the detached sub-terminal
    # so the MOVE action moves the whole housing unit, not just the circle.
    circle_body_map: dict[str, list[str]] = candidate.get("circle_body_map") or {}
    outlier_rep_handle = str(outlier_circle.get("handle") or "")
    body_handles = circle_body_map.get(outlier_rep_handle, [])
    full_target_handles = list(dict.fromkeys(outlier_handles + body_handles))

    body_count = len(body_handles)

    # n==5: 4 circles form a proper cluster, 1 is an extra orphan grouped in by topology
    if n == 5:
        reason = "정상 단자 배열 주변에 추가 원형 단자가 분리되어 있습니다."
        v = _terminal_violation(
            object_id,
            candidate_id,
            "terminal_orphan_circle",
            reason,
            full_target_handles,
            current_value="추가 원형 단자 감지",
            required_value="기준 단자 배열",
            bbox=outlier_bbox,
            target_bbox=majority_bbox,
        )
        v["actual_center"] = {"x": round(outlier_point[0], 4), "y": round(outlier_point[1], 4)}
        v["target_handles"] = full_target_handles
        return v

    # n==4: classic detached case. Use the 3 compact points to reconstruct the correct 4th grid corner.
    expected_center = _expected_4th_point(compact_points)

    reason = (
        f"원형 단자와 연결된 housing 요소 {body_count}개가 기준 단자 배열에서 이탈했습니다." if body_count
        else "원형 단자 1개가 같은 패턴의 단자 배열에서 이탈했습니다."
    )

    v = _terminal_violation(
        object_id,
        candidate_id,
        "terminal_detached_circle",
        reason,
        full_target_handles,
        current_value="단자 위치 이탈",
        required_value="기준 단자 배열 위치",
        bbox=outlier_bbox,
        target_bbox=majority_bbox,
    )
    v["actual_center"] = {"x": round(outlier_point[0], 4), "y": round(outlier_point[1], 4)}
    v["expected_center"] = {"x": round(expected_center[0], 4), "y": round(expected_center[1], 4)}
    v["target_handles"] = full_target_handles
    return v


def _run_orphan_terminal_check(elements: list[dict], topology: dict, qa_context: dict | None = None) -> list[dict]:
    """Flag CIRCLE entities matching terminal circle radius but absent from every terminal_candidate.

    Root problem: _cluster_circles uses distance thresholds, so a circle displaced far
    from the 4-circle cluster forms its own 1-circle group which is immediately discarded
    by detect_terminal_candidates (len < 4). This check re-scans raw elements so those
    escaped circles are never silently dropped.
    """
    terminal_candidates = topology.get("terminal_candidates") or []
    if not terminal_candidates:
        return []

    known_handles: set[str] = set()
    cluster_data: list[tuple[tuple[float, float], float]] = []

    for cand in terminal_candidates:
        for circle in (cand.get("circles") or []):
            for h in _expanded_circle_handles([circle]):
                known_handles.add(h)
            center = circle.get("center") or {}
            try:
                cluster_data.append(((float(center["x"]), float(center["y"])), float(circle.get("radius") or 0)))
            except (KeyError, TypeError, ValueError):
                pass

    if not cluster_data:
        return []

    cluster_radii = sorted(r for _, r in cluster_data)
    median_r = cluster_radii[len(cluster_radii) // 2]
    r_tol = median_r * 0.35

    cluster_centers = [c for c, _ in cluster_data]
    pair_dists = sorted(
        math.hypot(a[0] - b[0], a[1] - b[1])
        for i, a in enumerate(cluster_centers)
        for b in cluster_centers[i + 1:]
    )
    median_spacing = pair_dists[len(pair_dists) // 2] if pair_dists else 0.0
    max_dist = max(median_spacing * 12, median_r * 30, 1.0)

    # 텍스트 위치 캐시: orphan 억제 시 nearby standalone text 검색에 사용
    text_entities = [
        (e, _entity_point(e), str(e.get("text") or e.get("content") or ""))
        for e in elements
        if _etype(e) in {"TEXT", "MTEXT"}
    ]

    violations = []
    for entity in elements:
        raw_type = str(entity.get("raw_type") or entity.get("type") or "").upper()
        if raw_type != "CIRCLE":
            continue

        handle = str(entity.get("handle") or "")
        if not handle or handle in known_handles:
            continue

        try:
            radius = float(entity.get("radius") or 0)
        except (TypeError, ValueError):
            continue
        if radius <= 0 or not (median_r - r_tol <= radius <= median_r + r_tol):
            continue

        center = entity.get("center") or entity.get("position") or {}
        try:
            cx = float(center["x"])
            cy = float(center["y"])
        except (KeyError, TypeError, ValueError):
            continue

        nearest_dist = min(math.hypot(cx - kx, cy - ky) for (kx, ky) in cluster_centers)
        if nearest_dist > max_dist:
            continue

        # standalone 텍스트(TEST/E1/E2/접지 등)가 근처에 있으면 독립 심볼 → 억제
        # 탐색 반경: terminal spacing의 1.5배 or radius * 12 (어느 쪽이든 크게)
        search_r = max(median_spacing * 1.5, radius * 12, 1.0)
        has_standalone_label = any(
            _STANDALONE_TEXT_RE.search(txt)
            for _, pos, txt in text_entities
            if pos and math.hypot(pos[0] - cx, pos[1] - cy) <= search_r
        )
        if has_standalone_label:
            if qa_context:
                td = qa_context.get("terminal_debug")
                if isinstance(td, dict):
                    td.setdefault("suppressed_orphan_candidates", []).append({
                        "handle": handle,
                        "reason": "orphan suppressed: standalone text found nearby",
                    })
                    td.setdefault("filtered_reasons", []).append("orphan suppressed: standalone text found nearby")
            continue

        orphan_bbox = _normalize_bbox({"x1": cx - radius, "y1": cy - radius, "x2": cx + radius, "y2": cy + radius})
        violations.append({
            "object_id": handle,
            "equipment_id": handle,
            "terminal_candidate_id": handle,
            "violation_type": "terminal_orphan_circle",
            "category": "geometry_qa",
            "severity": "warning",
            "reason": (
                f"terminal 단자 크기(반지름 {round(radius, 1)})와 유사한 원형 단자가 "
                f"모든 terminal 클러스터 밖에 고립되어 있습니다 "
                f"(최근 단자까지 {round(nearest_dist, 1)})"
            ),
            "legal_reference": "전기 상세도 Geometry QA",
            "suggestion": "고립된 원형 단자를 terminal 클러스터 grid 위치로 복원하십시오.",
            "current_value": f"orphan_distance={round(nearest_dist, 1)}",
            "required_value": "terminal 클러스터 내 배치",
            "affected_handles": [handle],
            "target_handles": [handle],
            "actual_center": {"x": round(cx, 4), "y": round(cy, 4)},
            "bbox": orphan_bbox,
        })

    return violations


def _point_in_bbox(point: tuple[float, float], bbox: dict, margin: float = 0.0) -> bool:
    return (
        float(bbox["x1"]) - margin <= point[0] <= float(bbox["x2"]) + margin
        and float(bbox["y1"]) - margin <= point[1] <= float(bbox["y2"]) + margin
    )


def _expanded_circle_handles(circles: list[dict]) -> list[str]:
    handles: list[str] = []
    for circle in circles:
        members = circle.get("member_handles")
        if isinstance(members, list) and members:
            handles.extend(str(h) for h in members if h)
        elif circle.get("handle"):
            handles.append(str(circle.get("handle")))
    return list(dict.fromkeys(handles))


def _circle_handle_matches(circle: dict, handle: str) -> bool:
    if str(circle.get("handle") or "") == handle:
        return True
    return handle in {str(h) for h in (circle.get("member_handles") or [])}


def _bbox_for_circle_handles(circles: list[dict], handles: list[str]) -> dict | None:
    wanted = {str(h) for h in handles if h}
    selected: list[tuple[tuple[float, float], float]] = []
    for circle in circles:
        if not any(_circle_handle_matches(circle, handle) for handle in wanted):
            continue
        center = circle.get("center") or {}
        try:
            selected.append(((float(center["x"]), float(center["y"])), float(circle.get("radius") or 0)))
        except (KeyError, TypeError, ValueError):
            continue
    if not selected:
        return None
    return _points_bbox([point for point, _ in selected], [radius for _, radius in selected])


def _normalize_bbox(bbox: dict | None) -> dict | None:
    if not isinstance(bbox, dict):
        return None
    try:
        return {
            "x1": round(float(bbox["x1"]), 4),
            "y1": round(float(bbox["y1"]), 4),
            "x2": round(float(bbox["x2"]), 4),
            "y2": round(float(bbox["y2"]), 4),
        }
    except (KeyError, TypeError, ValueError):
        return None


def _bbox_midpoint(bbox: dict | None) -> dict | None:
    if not isinstance(bbox, dict):
        return None
    return {
        "x": (float(bbox["x1"]) + float(bbox["x2"])) / 2.0,
        "y": (float(bbox["y1"]) + float(bbox["y2"])) / 2.0,
    }


def _is_elec_block(e: dict) -> bool:
    """block_overlap 검사 대상이 되는 전기 블록인지 판단한다."""
    if str(e.get("domain") or "").upper() == "ELEC":
        return True

    if e.get("category"):
        return True

    elec_attrs = e.get("elec_attrs") or {}
    if isinstance(elec_attrs, dict) and elec_attrs.get("category"):
        return True

    name = " ".join([
        str(e.get("effective_name") or ""),
        str(e.get("block_name") or ""),
        str(e.get("layer") or ""),
        str(e.get("type") or ""),
    ]).upper()

    elec_keywords = (
        "LIGHT", "LAMP", "EXIT", "SWITCH", "SOCKET", "OUTLET",
        "PNL", "PANEL", "MCCB", "MCB", "ELB", "ACB", "BREAKER",
        "CABLE", "WIRE", "GND", "GROUND", "ELEC", "E-"
    )

    return any(k in name for k in elec_keywords)


def _is_electrical_circle_symbol(entity: dict, circle_context: dict | None = None) -> bool:
    """CIRCLE을 단순 도형이 아니라 전기 심볼 후보로 볼 수 있는지 판단한다."""
    circle_context = circle_context or {}
    reasons = set(circle_context.get("reasons") or [])
    if "low_signal_layer" in reasons:
        return False
    if is_standalone_electrical_symbol(circle_context):
        return True
    if str(entity.get("domain") or "").upper() == "ELEC":
        return True
    role = str(entity.get("electric_review_scope") or "")
    try:
        score = int(entity.get("role_score") or 0)
    except (TypeError, ValueError):
        score = 0
    if role in {ELECTRIC_CORE, ELECTRIC_CONTEXT} and score >= 2:
        return True

    classified = classify_entity_role(entity, {})
    return classified.role in {ELECTRIC_CORE, ELECTRIC_CONTEXT} and classified.score >= 2


def _circle_bbox_dict(center: tuple[float, float], radius: float) -> dict:
    return _normalize_bbox({
        "x1": center[0] - radius,
        "y1": center[1] - radius,
        "x2": center[0] + radius,
        "y2": center[1] + radius,
    }) or {}


def _is_nested_composite_circle(
    center_a: tuple[float, float],
    radius_a: float,
    center_b: tuple[float, float],
    radius_b: float,
    tol: float,
) -> bool:
    """같은 심볼을 구성하는 동심/포함 원은 겹침 위반에서 제외한다."""
    if radius_a <= 0 or radius_b <= 0:
        return False
    center_distance = math.hypot(center_a[0] - center_b[0], center_a[1] - center_b[1])
    small = min(radius_a, radius_b)
    large = max(radius_a, radius_b)
    if small <= 0:
        return False

    # 완전 중복 원은 중복 작성일 수 있으므로 제외하지 않는다.
    radius_ratio = small / large
    if radius_ratio > 0.85:
        return False

    concentric_tol = max(tol, small * 0.15)
    if center_distance <= concentric_tol:
        return True

    # 중심이 약간 어긋나도 작은 원이 큰 원 내부에 있으면 복합 심볼로 본다.
    return center_distance + small <= large + max(tol, large * 0.03)


def _has_concentric_outer_circle(entity: dict, candidates: list[dict], unit_factor: float) -> bool:
    data = _circle_center_radius(entity)
    if not data:
        return False
    center, radius = data
    if radius <= 0:
        return False

    tol = max(_CIRCLE_OVERLAP_TOL / max(unit_factor, 1e-9), radius * 0.15)
    handle = str(entity.get("handle") or "")
    for other in candidates:
        if handle and str(other.get("handle") or "") == handle:
            continue
        other_data = _circle_center_radius(other)
        if not other_data:
            continue
        other_center, other_radius = other_data
        if other_radius <= radius * 1.15:
            continue
        if math.hypot(center[0] - other_center[0], center[1] - other_center[1]) <= tol:
            return True
    return False


def _union_bbox_dict(a: dict, b: dict) -> dict:
    return _normalize_bbox({
        "x1": min(float(a["x1"]), float(b["x1"])),
        "y1": min(float(a["y1"]), float(b["y1"])),
        "x2": max(float(a["x2"]), float(b["x2"])),
        "y2": max(float(a["y2"]), float(b["y2"])),
    }) or {}


def _bbox(e: dict) -> tuple[float, float, float, float] | None:
    b = e.get("bbox")
    if not isinstance(b, dict):
        return None

    try:
        if "x1" in b:
            return float(b["x1"]), float(b["y1"]), float(b["x2"]), float(b["y2"])
        if "min_x" in b:
            return float(b["min_x"]), float(b["min_y"]), float(b["max_x"]), float(b["max_y"])
    except (TypeError, ValueError, KeyError):
        return None

    return None


def _overlaps(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
    tol: float = 0.0,
) -> bool:
    ax1, ay1, ax2, ay2 = min(a[0], a[2]), min(a[1], a[3]), max(a[0], a[2]), max(a[1], a[3])
    bx1, by1, bx2, by2 = min(b[0], b[2]), min(b[1], b[3]), max(b[0], b[2]), max(b[1], b[3])

    return not (
        ax2 + tol < bx1
        or bx2 + tol < ax1
        or ay2 + tol < by1
        or by2 + tol < ay1
    )


def _has_wire_endpoint_near(cx: float, cy: float, radius: float, elements: list[dict]) -> bool:
    """True if a wire LINE/POLYLINE endpoint is within 1.5× radius of the circle center."""
    snap = radius * 1.5
    for el in elements:
        etype = _etype(el)
        if etype == "LINE":
            for key in ("start", "end"):
                pt = el.get(key)
                if isinstance(pt, dict):
                    try:
                        if math.hypot(float(pt["x"]) - cx, float(pt["y"]) - cy) <= snap:
                            return True
                    except (KeyError, TypeError, ValueError):
                        pass
        elif etype in {"LWPOLYLINE", "POLYLINE", "SPLINE"}:
            for pt in (el.get("vertices") or el.get("points") or []):
                if isinstance(pt, dict):
                    try:
                        px = float(pt.get("x") or pt.get("X") or 0)
                        py = float(pt.get("y") or pt.get("Y") or 0)
                        if math.hypot(px - cx, py - cy) <= snap:
                            return True
                    except (TypeError, ValueError):
                        pass
    return False


def _forms_peer_row_pattern(centers: list[tuple[float, float]], median_r: float) -> bool:
    """True if 2+ orphan candidates form a regular horizontal row or vertical column.

    Consistent spacing (max/min gap < 1.8) in the dominant axis indicates these circles
    are an intentional repeating symbol group, not an error.
    """
    if len(centers) < 2:
        return False
    xs = sorted(c[0] for c in centers)
    ys = sorted(c[1] for c in centers)
    tol = median_r * 3.0

    if max(ys) - min(ys) <= tol:  # same horizontal row
        gaps = [xs[i + 1] - xs[i] for i in range(len(xs) - 1)]
        if gaps and max(gaps) / max(min(gaps), 1e-6) < 1.8:
            return True

    if max(xs) - min(xs) <= tol:  # same vertical column
        gaps = [ys[i + 1] - ys[i] for i in range(len(ys) - 1)]
        if gaps and max(gaps) / max(min(gaps), 1e-6) < 1.8:
            return True

    return False


# Context-aware orphan rule. Defined last on purpose so it overrides the legacy
# radius-only orphan implementation above without touching unrelated checker code.
def _run_orphan_terminal_check(elements: list[dict], topology: dict, qa_context: dict | None = None) -> list[dict]:
    terminal_candidates = topology.get("terminal_candidates") or []
    if not terminal_candidates:
        return []

    qa_context = qa_context or build_geometry_qa_context(elements, topology)
    terminal_debug = qa_context.get("terminal_debug") or {}
    circle_contexts = qa_context.get("circle_contexts") or {}
    drawing_intent = str((topology.get("summary") or {}).get("drawing_intent") or "")

    known_handles: set[str] = set()
    cluster_data: list[tuple[tuple[float, float], float]] = []
    for cand in terminal_candidates:
        for circle in (cand.get("circles") or []):
            known_handles.update(_expanded_circle_handles([circle]))
            cr = _circle_center_radius(circle)
            if cr:
                cluster_data.append(cr)
    if not cluster_data:
        return []

    cluster_radii = sorted(r for _, r in cluster_data)
    median_r = cluster_radii[len(cluster_radii) // 2]
    r_tol = median_r * 0.35
    cluster_centers = [c for c, _ in cluster_data]
    pair_dists = sorted(
        math.hypot(a[0] - b[0], a[1] - b[1])
        for i, a in enumerate(cluster_centers)
        for b in cluster_centers[i + 1:]
    )
    median_spacing = pair_dists[len(pair_dists) // 2] if pair_dists else 0.0
    max_dist = max(median_spacing * 12, median_r * 30, 1.0)

    # 텍스트 위치 캐시: standalone 심볼 억제 시 사용 (radius * 12 탐색)
    text_entities = [
        (e, _entity_point(e), str(e.get("text") or e.get("content") or ""))
        for e in elements
        if _etype(e) in {"TEXT", "MTEXT"}
    ]

    raw_findings: list[dict] = []
    for entity in elements:
        if _etype(entity) != "CIRCLE":
            continue
        handle = str(entity.get("handle") or "")
        if not handle or handle in known_handles:
            continue
        cr = _circle_center_radius(entity)
        if not cr:
            continue
        (cx, cy), radius = cr
        if radius <= 0 or not (median_r - r_tol <= radius <= median_r + r_tol):
            continue
        nearest_dist = min(math.hypot(cx - kx, cy - ky) for (kx, ky) in cluster_centers)
        if nearest_dist > max_dist:
            continue

        # ① standalone 텍스트(TEST/E1/E2/접지 등)가 근처에 있으면 독립 심볼 → 즉시 억제
        search_r = max(median_spacing * 1.5, radius * 12, 1.0)
        if any(
            _STANDALONE_TEXT_RE.search(txt)
            for _, pos, txt in text_entities
            if pos and math.hypot(pos[0] - cx, pos[1] - cy) <= search_r
        ):
            terminal_debug.setdefault("suppressed_orphan_candidates", []).append({
                "handle": handle,
                "reason": "orphan suppressed: standalone text found nearby",
            })
            terminal_debug.setdefault("filtered_reasons", []).append("orphan suppressed: standalone text found nearby")
            continue

        # ② wire endpoint 연결 확인: 연결된 원형은 의도적 단자/접지 심볼이므로 억제
        if _has_wire_endpoint_near(cx, cy, radius, elements):
            terminal_debug.setdefault("suppressed_orphan_candidates", []).append({
                "handle": handle,
                "reason": "orphan suppressed: wire endpoint connected",
            })
            terminal_debug.setdefault("filtered_reasons", []).append("orphan suppressed: wire endpoint connected")
            continue

        circle_context = circle_contexts.get(handle) or classify_circle_context(
            entity, elements, topology, qa_context.get("drawing_internal_standards") or {}
        )
        confidence = 0.35
        if nearest_dist <= max(median_spacing * 4.0, median_r * 12.0, 1.0):
            confidence += 0.25
        if circle_context.get("nearby_body"):
            confidence += 0.2
        if not is_standalone_electrical_symbol(circle_context):
            confidence += 0.1

        finding = {
            "handle": handle,
            "terminal_candidate_id": handle,
            "violation_type": "terminal_orphan_circle",
            "actual_center": {"x": round(cx, 4), "y": round(cy, 4)},
            "bbox": _normalize_bbox({"x1": cx - radius, "y1": cy - radius, "x2": cx + radius, "y2": cy + radius}),
            "circle_context": circle_context,
            "confidence": round(confidence, 3),
        }
        suppressed, reason = suppress_false_positive_orphan(finding, qa_context)
        if suppressed:
            terminal_debug.setdefault("suppressed_orphan_candidates", []).append({
                "handle": handle,
                "reason": reason,
                "nearby_texts": circle_context.get("nearby_texts") or [],
                "context_reasons": circle_context.get("reasons") or [],
            })
            terminal_debug.setdefault("filtered_reasons", []).append(reason)
            continue

        terminal_debug.setdefault("orphan_candidates", []).append({
            "handle": handle,
            "confidence": round(confidence, 3),
            "nearest_distance": round(nearest_dist, 3),
        })
        raw_findings.append(finding)

    # ③ 반복/대칭 패턴 확인: 2개 이상의 orphan이 규칙적 행/열을 이루면 의도적 심볼 그룹 → 전체 억제
    if len(raw_findings) >= 2:
        centers = [
            (f["actual_center"]["x"], f["actual_center"]["y"])
            for f in raw_findings
            if f.get("actual_center")
        ]
        if _forms_peer_row_pattern(centers, median_r):
            for f in raw_findings:
                terminal_debug.setdefault("suppressed_orphan_candidates", []).append({
                    "handle": f["handle"],
                    "reason": "orphan suppressed: regular peer pattern detected",
                })
            terminal_debug.setdefault("filtered_reasons", []).append("orphan suppressed: regular peer pattern detected")
            return []

    return normalize_geometry_qa_findings(raw_findings, drawing_intent)


def _etype(entity: dict) -> str:
    return str(entity.get("raw_type") or entity.get("type") or "").upper()


def _circle_center_radius(entity: dict) -> tuple[tuple[float, float], float] | None:
    center = entity.get("center") or entity.get("position") or {}
    try:
        return (float(center["x"]), float(center["y"])), float(entity.get("radius") or 0)
    except (KeyError, TypeError, ValueError):
        return None


def _entity_point(entity: dict) -> tuple[float, float] | None:
    for key in ("insert_point", "position", "center", "start"):
        p = entity.get(key)
        if isinstance(p, dict):
            try:
                return float(p["x"]), float(p["y"])
            except (KeyError, TypeError, ValueError):
                pass
    bbox = entity.get("bbox")
    if isinstance(bbox, dict):
        try:
            return (float(bbox["x1"]) + float(bbox["x2"])) / 2.0, (float(bbox["y1"]) + float(bbox["y2"])) / 2.0
        except (KeyError, TypeError, ValueError):
            pass
    return None


def _entity_text(entity: dict) -> str:
    return str(entity.get("text") or entity.get("content") or entity.get("value") or "").strip()


def _median(values: list[float]) -> float:
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return 0.0
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0


def _mad(values: list[float], center: float | None = None) -> float:
    if not values:
        return 0.0
    c = _median(values) if center is None else float(center)
    return _median([abs(float(v) - c) for v in values])


def _has_nearby_body(center: tuple[float, float], radius: float, elements: list[dict], search: float) -> bool:
    box = {
        "x1": center[0] - max(search, radius * 4.0),
        "y1": center[1] - max(search, radius * 4.0),
        "x2": center[0] + max(search, radius * 4.0),
        "y2": center[1] + max(search, radius * 4.0),
    }
    for entity in elements:
        if _etype(entity) not in {"LINE", "POLYLINE", "LWPOLYLINE"}:
            continue
        pt = _entity_point(entity)
        if pt and _point_in_bbox(pt, box, 0):
            return True
    return False


def _same_circle_pattern_count(circle: dict, elements: list[dict], standards: dict) -> int:
    cr = _circle_center_radius(circle)
    if not cr:
        return 0
    (_center, radius) = cr
    target_layer = str(circle.get("layer") or "")
    terminal_r = float(standards.get("_raw_terminal_radius_median") or radius)
    r_tol = max(terminal_r * 0.35, radius * 0.25, 0.1)
    count = 0
    for entity in elements:
        if _etype(entity) != "CIRCLE":
            continue
        other = _circle_center_radius(entity)
        if not other:
            continue
        _p, r = other
        if abs(r - radius) <= r_tol and str(entity.get("layer") or "") == target_layer:
            count += 1
    return count
