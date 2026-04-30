"""
CAD drawing-quality checks for pipe drawings.

This checker is intentionally separate from legal/spec compliance. It reports
drafting issues such as dangling pipe endpoints, suspicious annotation gaps, and
near-miss connections so users can review drawing quality without treating every
ambiguous drafting convention as a regulation violation.
"""
from __future__ import annotations

import math
from typing import Any


_LINE_TYPES = frozenset({"LINE", "ARC", "POLYLINE", "LWPOLYLINE", "SPLINE"})
_TEXT_TYPES = frozenset({"TEXT", "MTEXT", "MLEADER"})
_MIN_DANGLING_LENGTH_MM = 300.0
_ORPHAN_ANNOTATION_NEAR_MM = 180.0


def _raw_type(el: dict) -> str:
    return str(el.get("raw_type") or el.get("type") or "").upper()


def _handle(el: dict) -> str:
    return str(el.get("handle") or el.get("id") or "")


def _pt(d: dict | None, *keys: str) -> tuple[float, float] | None:
    if not isinstance(d, dict):
        return None
    for key in keys:
        p = d.get(key)
        if isinstance(p, dict) and "x" in p and "y" in p:
            try:
                return float(p["x"]), float(p["y"])
            except (TypeError, ValueError):
                pass
    return None


def _bbox_center(bbox: dict | None) -> tuple[float, float] | None:
    if not isinstance(bbox, dict):
        return None
    try:
        if {"x1", "x2", "y1", "y2"}.issubset(bbox):
            return (
                (float(bbox["x1"]) + float(bbox["x2"])) / 2.0,
                (float(bbox["y1"]) + float(bbox["y2"])) / 2.0,
            )
        if {"min_x", "max_x", "min_y", "max_y"}.issubset(bbox):
            return (
                (float(bbox["min_x"]) + float(bbox["max_x"])) / 2.0,
                (float(bbox["min_y"]) + float(bbox["max_y"])) / 2.0,
            )
    except (TypeError, ValueError):
        return None
    return None


def _position(el: dict) -> dict[str, float] | None:
    p = _pt(el, "position", "insert_point", "center", "start")
    if p is None:
        p = _bbox_center(el.get("bbox"))
    if p is None:
        return None
    return {"x": round(p[0], 3), "y": round(p[1], 3)}


def _endpoints(el: dict) -> tuple[tuple[float, float] | None, tuple[float, float] | None]:
    rt = _raw_type(el)
    if rt in {"POLYLINE", "LWPOLYLINE", "SPLINE"}:
        vertices = el.get("vertices") or el.get("fit_points") or []
        pts: list[tuple[float, float]] = []
        for v in vertices:
            p = _pt({"p": v}, "p") if isinstance(v, dict) else None
            if p:
                pts.append(p)
        if len(pts) >= 2:
            return pts[0], pts[-1]
    return _pt(el, "start"), _pt(el, "end")


def _dist(a: tuple[float, float] | None, b: tuple[float, float] | None) -> float:
    if not a or not b:
        return math.inf
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _line_length_mm(el: dict, unit_factor: float) -> float:
    value = el.get("length")
    try:
        if value is not None:
            return float(value) * unit_factor
    except (TypeError, ValueError):
        pass
    a, b = _endpoints(el)
    return _dist(a, b) * unit_factor


def _point_segment_distance(
    p: tuple[float, float],
    a: tuple[float, float] | None,
    b: tuple[float, float] | None,
) -> float:
    if not a or not b:
        return math.inf
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    denom = dx * dx + dy * dy
    if denom <= 1e-12:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / denom))
    proj = (ax + t * dx, ay + t * dy)
    return math.hypot(px - proj[0], py - proj[1])


def _has_explicit_pipe_evidence(el: dict, run: dict | None = None) -> bool:
    attrs = el.get("attributes") or el.get("properties") or {}
    material = str(el.get("material") or (run or {}).get("material") or "").upper()
    role = str(el.get("layer_role") or "").lower()
    return bool(
        role == "mep"
        or el.get("flag_for_piping_agent")
        or el.get("diameter_mm")
        or (run or {}).get("diameter_mm")
        or material not in {"", "UNKNOWN", "NONE"}
        or attrs.get("SIZE")
        or attrs.get("DIAMETER")
        or attrs.get("MATERIAL")
        or attrs.get("TAG_NAME")
    )


def _is_arch_or_aux(el: dict) -> bool:
    return str(el.get("layer_role") or "").lower() in {"arch", "aux"}


def _issue(
    *,
    equipment_id: str,
    issue_type: str,
    reason: str,
    current_value: str,
    required_value: str,
    confidence_score: float,
    position: dict[str, float] | None = None,
    related_handles: list[str] | None = None,
    confidence_reason: str = "",
) -> dict[str, Any]:
    return {
        "equipment_id": equipment_id,
        "issue_type": issue_type,
        "violation_type": issue_type,
        "reference_rule": "도면 품질검사 — 배관 도면 작성 정합성 확인",
        "current_value": current_value,
        "required_value": required_value,
        "reason": reason,
        "position": position,
        "related_handles": related_handles or [],
        "confidence_score": round(confidence_score, 3),
        "confidence_reason": confidence_reason or "drawing_qa",
        "_source": "drawing_qa",
        "proposed_action": {
            "type": "MANUAL_REVIEW",
            "reason": "도면 작성 의도 확인 후 배관 끝점/주석/접속부를 수정하세요.",
        },
    }


def run_drawing_qa_checks(
    elements: list[dict],
    topology: dict,
    geo: dict | None = None,
    *,
    unit_factor: float = 1.0,
) -> list[dict]:
    """Return drawing QA issues in a violation-like schema."""
    del geo  # reserved for future clearance/overlap QA rules
    issues: list[dict] = []

    el_map = {
        _handle(el): el
        for el in elements or []
        if isinstance(el, dict) and _handle(el)
    }
    run_by_handle: dict[str, dict] = {}
    for run in topology.get("pipe_runs", []) or []:
        if not isinstance(run, dict):
            continue
        for h in run.get("handles") or []:
            run_by_handle[str(h)] = run

    # 1) Annotation-bridged gaps are accepted topology. A "G", "20", "DN20",
    # etc. between two pipe line segments is a drafting convention that means
    # the run continues through the annotation gap, so QA must not report it as
    # disconnected.
    virtual_pairs: set[frozenset[str]] = set()
    virtual_handles: set[str] = set()
    for vc in topology.get("virtual_connections", []) or []:
        ha = str(vc.get("from_handle") or "")
        hb = str(vc.get("to_handle") or "")
        if ha and hb:
            virtual_pairs.add(frozenset((ha, hb)))
            virtual_handles.update((ha, hb))

    # 2) Near-miss candidates from topology are drafting QA issues even when the
    # legal checker refuses to make a hard regulation claim.
    for gap in topology.get("broken_gaps", []) or []:
        ha = str(gap.get("from_handle") or "")
        hb = str(gap.get("to_handle") or "")
        if frozenset((ha, hb)) in virtual_pairs:
            continue
        ea = el_map.get(ha, {})
        eb = el_map.get(hb, {})
        if _is_arch_or_aux(ea) or _is_arch_or_aux(eb):
            continue
        gap_mm = float(gap.get("gap_mm") or 0)
        if gap_mm <= 0:
            continue
        issues.append(_issue(
            equipment_id=ha,
            issue_type="drawing_quality_pipe_gap",
            reason=(
                f"같은 축·스타일의 배관 후보 {ha!r}와 {hb!r} 사이에 "
                f"{gap_mm:.0f}mm 미연결 gap이 있습니다."
            ),
            current_value=f"미연결 gap {gap_mm:.0f}mm",
            required_value="배관 끝점 연결 또는 의도적 생략 주석 필요",
            confidence_score=0.80,
            position=_position(ea),
            related_handles=[ha, hb],
            confidence_reason="topology_broken_gap",
        ))

    for item in topology.get("connection_mismatches", []) or []:
        ha = str(item.get("endpoint_handle") or "")
        hb = str(item.get("segment_handle") or "")
        if frozenset((ha, hb)) in virtual_pairs:
            continue
        ea = el_map.get(ha, {})
        eb = el_map.get(hb, {})
        if _is_arch_or_aux(ea) or _is_arch_or_aux(eb):
            continue
        offset_mm = float(item.get("offset_mm") or 0)
        if offset_mm <= 0:
            continue
        issues.append(_issue(
            equipment_id=ha,
            issue_type="drawing_quality_connection_mismatch",
            reason=(
                f"배관 끝점 {ha!r}이 접속 대상 {hb!r}에서 {offset_mm:.0f}mm 벗어나 있습니다."
            ),
            current_value=f"접속점 offset {offset_mm:.0f}mm",
            required_value="끝점이 대상 배관/피팅과 허용 오차 내 접속",
            confidence_score=0.78,
            position=_position(ea),
            related_handles=[ha, hb],
            confidence_reason="topology_connection_mismatch",
        ))

    # 3) Dangling single-line runs. Keep confidence lower unless explicit pipe
    # evidence exists, because endpoints can be intentional drawing boundaries.
    gap_or_mismatch_handles = {
        str(h)
        for item in [
            *(topology.get("broken_gaps") or []),
            *(topology.get("connection_mismatches") or []),
        ]
        for h in (
            item.get("from_handle"),
            item.get("to_handle"),
            item.get("endpoint_handle"),
            item.get("segment_handle"),
        )
        if h
    }
    for run in topology.get("pipe_runs", []) or []:
        handles = [str(h) for h in (run.get("handles") or []) if h]
        if len(handles) != 1 or run.get("connected_blocks"):
            continue
        h = handles[0]
        if h in virtual_handles or h in gap_or_mismatch_handles:
            continue
        el = el_map.get(h, {})
        if not el or _is_arch_or_aux(el) or _raw_type(el) not in _LINE_TYPES:
            continue
        length_mm = float(run.get("total_length_mm") or 0) or _line_length_mm(el, unit_factor)
        if length_mm < _MIN_DANGLING_LENGTH_MM:
            continue
        explicit = _has_explicit_pipe_evidence(el, run)
        issues.append(_issue(
            equipment_id=h,
            issue_type="drawing_quality_dangling_pipe",
            reason=(
                f"배관 후보 선분 {h!r}의 양 끝이 다른 배관/장비와 연결되지 않았습니다 "
                f"(길이 {length_mm:.0f}mm). 도면 경계·상하 연결 생략인지 확인하세요."
            ),
            current_value=f"고립 배관 후보 {length_mm:.0f}mm",
            required_value="연결 의도 표기 또는 배관/장비 끝점 접속",
            confidence_score=0.74 if explicit else 0.64,
            position=_position(el),
            related_handles=[h],
            confidence_reason="single_line_run_explicit_pipe" if explicit else "single_line_run_weak_pipe",
        ))

    # 4) Orphan pipe annotations: text such as G, GAS, DN20, 20A that is not near
    # any accepted pipe run may mean the user selected/moved only the note.
    line_segments = [
        (_endpoints(el), el)
        for el in el_map.values()
        if _raw_type(el) in _LINE_TYPES and _handle(el) in run_by_handle
    ]
    for el in el_map.values():
        if _raw_type(el) not in _TEXT_TYPES:
            continue
        text = str(el.get("text") or el.get("content") or "").strip()
        if not text:
            continue
        compact = text.replace(" ", "").upper()
        is_pipe_note = (
            compact in {"G", "GAS", "LPG", "LNG"}
            or compact.startswith("DN")
            or compact.endswith("A") and compact[:-1].replace(".", "", 1).isdigit()
            or compact.replace(".", "", 1).isdigit()
        )
        if not is_pipe_note:
            continue
        pos_tuple = _pt(el, "position", "insert_point", "center") or _bbox_center(el.get("bbox"))
        if not pos_tuple:
            continue
        nearest = min(
            (
                _point_segment_distance(pos_tuple, eps[0], eps[1]) * unit_factor
                for eps, _line in line_segments
            ),
            default=math.inf,
        )
        if nearest <= _ORPHAN_ANNOTATION_NEAR_MM:
            continue
        h = _handle(el)
        issues.append(_issue(
            equipment_id=h,
            issue_type="drawing_quality_orphan_pipe_annotation",
            reason=(
                f"배관 주석 {text!r}이 인식된 배관 선분에서 {nearest:.0f}mm 이상 떨어져 있습니다. "
                "주석 위치 또는 관련 배관 선 선택/연결을 확인하세요."
            ),
            current_value=f"주석-배관 거리 {nearest:.0f}mm",
            required_value=f"{_ORPHAN_ANNOTATION_NEAR_MM:.0f}mm 이내 또는 명확한 리더/연결 표기",
            confidence_score=0.70,
            position=_position(el),
            related_handles=[h] if h else [],
            confidence_reason="orphan_pipe_annotation",
        ))

    # Deduplicate by handle/type.
    deduped: dict[tuple[str, str], dict] = {}
    for issue in issues:
        key = (str(issue.get("equipment_id") or ""), str(issue.get("issue_type") or ""))
        prev = deduped.get(key)
        if prev is None or float(issue.get("confidence_score") or 0) > float(prev.get("confidence_score") or 0):
            deduped[key] = issue
    return list(deduped.values())
