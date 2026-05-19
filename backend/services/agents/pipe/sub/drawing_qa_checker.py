"""
CAD drawing-quality checks for pipe drawings.

This checker is intentionally separate from legal/spec compliance. It reports
drafting issues such as dangling pipe endpoints, suspicious annotation gaps, and
near-miss connections so users can review drawing quality without treating every
ambiguous drafting convention as a regulation violation.
"""
from __future__ import annotations

import math
import re
from typing import Any


_LINE_TYPES = frozenset({"LINE", "ARC", "POLYLINE", "LWPOLYLINE", "SPLINE"})
_TEXT_TYPES = frozenset({"TEXT", "MTEXT", "MLEADER"})
_MIN_DANGLING_LENGTH_MM = 300.0
_ORPHAN_ANNOTATION_NEAR_MM = 250.0
_PIPE_ANNOTATION_SUPPRESS_NEAR_MM = 300.0
_GENERIC_ANNOTATION_SEGMENT_NEAR_MM = 700.0
_GENERIC_CONNECTABLE_MIN_MM = 1500.0
_PIPE_AXIS_FIX_COS_TOL = 0.98
_PIPE_AXIS_POINT_TOL_MM = 2.0
_EXISTING_CONNECTION_TOL_MM = 2.0
_WEAK_TOPOLOGY_EVIDENCE = frozenset({"layer_annotation_style", "near_pipe_annotation"})
_QA_FIX_LAYER = "AI_PIPE_QA_FIX"
_PIPE_LAYER_RE = re.compile(
    r"GAS|PIPE|PIPING|배관|급수|급탕|배수|위생|소화|SPRINK|CWS|HWS|FIRE|"
    r"^P[-_]|^M[-_]",
    re.IGNORECASE,
)
_GENERIC_LAYER_RE = re.compile(r"^(?:L\d+|LAYER\d*|\d+)$", re.IGNORECASE)
_PIPE_SIZE_TEXT_RE = re.compile(r"^\s*\d{2,3}(?:\s+\d{2,3}){0,2}\s*$")
_SANITARY_SYSTEM_TEXT_RE = re.compile(r"^\s*(?:D|S|V|SD|FD|VP|CW|HW|CWS|HWS)\s*$", re.IGNORECASE)


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
    p = _bbox_center(el.get("bbox")) if _raw_type(el) in _TEXT_TYPES else None
    if p is None:
        p = _pt(el, "position", "insert_point", "center", "start")
    if p is None:
        p = _bbox_center(el.get("bbox"))
    if p is None:
        return None
    return {"x": round(p[0], 3), "y": round(p[1], 3)}


def _bbox_points(bbox: dict | None) -> list[tuple[float, float]]:
    if not isinstance(bbox, dict):
        return []
    try:
        if {"x1", "x2", "y1", "y2"}.issubset(bbox):
            x1, x2 = float(bbox["x1"]), float(bbox["x2"])
            y1, y2 = float(bbox["y1"]), float(bbox["y2"])
        elif {"min_x", "max_x", "min_y", "max_y"}.issubset(bbox):
            x1, x2 = float(bbox["min_x"]), float(bbox["max_x"])
            y1, y2 = float(bbox["min_y"]), float(bbox["max_y"])
        else:
            return []
    except (TypeError, ValueError):
        return []
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    return [
        (cx, cy),
        (x1, y1), (x1, y2), (x2, y1), (x2, y2),
        (cx, y1), (cx, y2), (x1, cy), (x2, cy),
    ]


def _annotation_measure_points(el: dict) -> list[tuple[float, float]]:
    pts = _bbox_points(el.get("bbox"))
    for p in (
        _pt(el, "center"),
        _pt(el, "position"),
        _pt(el, "insert_point"),
        _pt(el, "start"),
        _pt(el, "end"),
        _pt(el, "text_position"),
    ):
        if p and p not in pts:
            pts.append(p)
    return pts


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


def _unit_vector(
    a: tuple[float, float] | None,
    b: tuple[float, float] | None,
) -> tuple[float, float] | None:
    if not a or not b:
        return None
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    if length <= 1e-9:
        return None
    return dx / length, dy / length


def _line_axis(el: dict) -> tuple[float, float] | None:
    start, end = _endpoints(el)
    return _unit_vector(start, end)


def _axis_aligned(
    axis: tuple[float, float] | None,
    a: tuple[float, float] | None,
    b: tuple[float, float] | None,
    *,
    unit_factor: float = 1.0,
) -> bool:
    move_axis = _unit_vector(a, b)
    if not axis or not move_axis or not a or not b:
        return False
    dot_ok = abs(axis[0] * move_axis[0] + axis[1] * move_axis[1]) >= _PIPE_AXIS_FIX_COS_TOL
    dx, dy = b[0] - a[0], b[1] - a[1]
    offset_mm = abs(dx * axis[1] - dy * axis[0]) * unit_factor
    return dot_ok and offset_mm <= _PIPE_AXIS_POINT_TOL_MM


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


def _is_strong_pipe_note_text(text: str) -> bool:
    compact = str(text or "").replace(" ", "").upper()
    values = [float(v) for v in re.findall(r"\d+(?:\.\d+)?", text or "")]
    return bool(
        compact in {"G", "GAS", "LPG", "LNG"}
        or _SANITARY_SYSTEM_TEXT_RE.match(text or "")
        or compact.startswith("DN")
        or compact.endswith("A") and compact[:-1].replace(".", "", 1).isdigit()
        or (
            _PIPE_SIZE_TEXT_RE.match(text or "")
            and values
            and all(10.0 <= v <= 300.0 for v in values)
        )
    )


def _pipe_annotation_points(elements: list[dict]) -> list[tuple[str, tuple[float, float]]]:
    points: list[tuple[str, tuple[float, float]]] = []
    for el in elements or []:
        if not isinstance(el, dict) or _raw_type(el) not in _TEXT_TYPES:
            continue
        text = _text_value(el)
        if not _is_strong_pipe_note_text(text):
            continue
        handle = _handle(el)
        for point in _annotation_measure_points(el):
            points.append((handle, point))
    return points


def _is_generic_non_pipe_layer(el: dict) -> bool:
    layer = str(el.get("layer") or "").strip()
    role = str(el.get("layer_role") or "").lower()
    if role == "mep" or bool(el.get("flag_for_piping_agent")):
        return False
    return bool(_GENERIC_LAYER_RE.match(layer) and not _PIPE_LAYER_RE.search(layer))


def _is_pipe_symbol_fragment(el: dict) -> bool:
    return str(el.get("geometry_role") or "").lower() == "pipe_symbol"


def _dict_point(point: dict | None) -> tuple[float, float] | None:
    if not isinstance(point, dict):
        return None
    try:
        return float(point["x"]), float(point["y"])
    except (KeyError, TypeError, ValueError):
        return None


def _near_pipe_annotation(
    points: list[tuple[float, float] | None],
    annotation_points: list[tuple[str, tuple[float, float]]],
    unit_factor: float,
    *,
    near_mm: float = _PIPE_ANNOTATION_SUPPRESS_NEAR_MM,
) -> bool:
    if not annotation_points:
        return False
    limit = near_mm / max(unit_factor, 1e-9)
    return any(
        p is not None and _dist(p, ann_point) <= limit
        for p in points
        for _ann_handle, ann_point in annotation_points
    )


def _line_endpoint_near_pipe_annotation(
    el: dict,
    annotation_points: list[tuple[str, tuple[float, float]]],
    unit_factor: float,
) -> bool:
    start, end = _endpoints(el)
    return _near_pipe_annotation([start, end], annotation_points, unit_factor)


def _line_segment_near_pipe_annotation(
    el: dict,
    annotation_points: list[tuple[str, tuple[float, float]]],
    unit_factor: float,
    *,
    near_mm: float = _GENERIC_ANNOTATION_SEGMENT_NEAR_MM,
) -> bool:
    start, end = _endpoints(el)
    if not start or not end or not annotation_points:
        return False
    limit = near_mm / max(unit_factor, 1e-9)
    return any(
        _point_segment_distance(ann_point, start, end) <= limit
        for _ann_handle, ann_point in annotation_points
    )


def _generic_line_near_pipe_annotation(
    el: dict,
    annotation_points: list[tuple[str, tuple[float, float]]],
    unit_factor: float,
) -> bool:
    if not _is_generic_non_pipe_layer(el):
        return False
    return (
        _line_endpoint_near_pipe_annotation(el, annotation_points, unit_factor)
        or _line_segment_near_pipe_annotation(el, annotation_points, unit_factor)
    )


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


def _has_element_pipe_evidence(el: dict) -> bool:
    attrs = el.get("attributes") or el.get("properties") or {}
    material = str(el.get("material") or "").upper()
    role = str(el.get("layer_role") or "").lower()
    return bool(
        role == "mep"
        or el.get("flag_for_piping_agent")
        or el.get("diameter_mm")
        or material not in {"", "UNKNOWN", "NONE"}
        or attrs.get("SIZE")
        or attrs.get("DIAMETER")
        or attrs.get("MATERIAL")
        or attrs.get("TAG_NAME")
    )


def _has_local_pipe_attrs(el: dict) -> bool:
    """Return True for pipe evidence attached to the entity itself.

    Upstream layer statistics are useful context, but user-visible continuity
    QA needs evidence attached to the entity itself or to a trusted pipe layer.
    """
    attrs = el.get("attributes") or el.get("properties") or {}
    material = str(el.get("material") or "").upper()
    return bool(
        el.get("flag_for_piping_agent")
        or el.get("diameter_mm")
        or material not in {"", "UNKNOWN", "NONE"}
        or attrs.get("SIZE")
        or attrs.get("DIAMETER")
        or attrs.get("MATERIAL")
        or attrs.get("TAG_NAME")
    )


def _has_only_annotation_style_evidence(el: dict, run: dict | None = None) -> bool:
    topology_evidence = bool(
        run
        and _handle(el) in {str(h) for h in (run.get("handles") or [])}
        and str(el.get("topology_pipe_evidence") or "") in _WEAK_TOPOLOGY_EVIDENCE
    )
    return bool(topology_evidence and not _has_local_pipe_attrs(el))


def _has_strong_pipe_evidence(el: dict, run: dict | None = None) -> bool:
    attrs = el.get("attributes") or el.get("properties") or {}
    layer = str(el.get("layer") or "").strip()
    role = str(el.get("layer_role") or "").lower()
    material = str(el.get("material") or "").upper()
    run_material = str((run or {}).get("material") or "").upper()
    has_local_attrs = bool(
        el.get("flag_for_piping_agent")
        or el.get("diameter_mm")
        or material not in {"", "UNKNOWN", "NONE"}
        or attrs.get("SIZE")
        or attrs.get("DIAMETER")
        or attrs.get("MATERIAL")
        or attrs.get("TAG_NAME")
    )
    if has_local_attrs or _PIPE_LAYER_RE.search(layer):
        return True
    if _is_generic_non_pipe_layer(el):
        return False
    if (run or {}).get("connected_blocks"):
        return True
    return bool(
        role == "mep"
        or (run or {}).get("diameter_mm")
        or run_material not in {"", "UNKNOWN", "NONE"}
    )


def _is_weak_generic_pipe_candidate(el: dict, run: dict | None = None) -> bool:
    return bool(_is_generic_non_pipe_layer(el) and not _has_strong_pipe_evidence(el, run))


def _has_weak_topology_pipe_context(
    el: dict,
    *,
    unit_factor: float = 1.0,
) -> bool:
    if _is_pipe_symbol_fragment(el):
        return False
    return bool(
        str(el.get("topology_pipe_evidence") or "") in _WEAK_TOPOLOGY_EVIDENCE
        and _line_length_mm(el, unit_factor) >= _MIN_DANGLING_LENGTH_MM
    )


def _allow_weak_context_for_qa_pair(
    a_el: dict,
    b_el: dict,
    a_run: dict | None = None,
    b_run: dict | None = None,
    *,
    unit_factor: float = 1.0,
) -> bool:
    """Allow weak generic pipe candidates when paired with strong pipe evidence.

    User CAD layers are often named L2/L3/TEX, so one side of a physical gas
    pipe gap may only carry topology's layer/annotation-style hint. We keep
    purely weak pairs suppressed, but do not drop a same-candidate gap when the
    other side has explicit pipe evidence such as GAS/diameter attributes.
    """
    return bool(
        (
            _has_strong_pipe_evidence(a_el, a_run)
            and _has_weak_topology_pipe_context(b_el, unit_factor=unit_factor)
        )
        or (
            _has_strong_pipe_evidence(b_el, b_run)
            and _has_weak_topology_pipe_context(a_el, unit_factor=unit_factor)
        )
    )


def _is_connectable_pipe_geometry(
    el: dict,
    run: dict | None = None,
    *,
    unit_factor: float = 1.0,
    allow_weak_annotation_context: bool = False,
) -> bool:
    """Return True only for geometry that should participate in pipe continuity QA.

    A symbol/leader can be pipe-related, especially after nearby text such as
    "G" or "20A" enriches it with pipe attributes. That is useful context for
    topology, but weak inferred context should not by itself make a symbol or
    leader a pipe centerline whose endpoints must connect.
    """
    if _raw_type(el) not in _LINE_TYPES or _is_arch_or_aux(el):
        return False
    if el.get("exclude_from_pipe_topology") or str(el.get("connectivity_role") or "").lower() == "symbol":
        return False

    source = str(el.get("source_attributes") or "").lower()
    layer = str(el.get("layer") or "").strip()
    role = str(el.get("layer_role") or "").lower()
    pipe_layer = bool(_PIPE_LAYER_RE.search(layer))

    if _is_generic_non_pipe_layer(el) and not pipe_layer:
        # User-drawn symbols often live on ambiguous project layers. Layer
        # mapping can still mark them as MEP, so require real pipe evidence for
        # QA. Topology's layer_annotation_style hint is intentionally weak: it
        # may help build candidate runs, but it must not create user-visible
        # gap/mismatch/dangling issues by itself.
        length_mm = _line_length_mm(el, unit_factor)
        # For weak inferred candidates, run-level material can be inherited from a
        # nearby note after topology groups weak lines. Treat only element-local
        # evidence as strong enough to emit QA.
        explicit_evidence = _has_element_pipe_evidence(el)
        role_pipe_evidence = role == "mep" or bool(el.get("flag_for_piping_agent")) or pipe_layer
        if (
            source == "text_extracted"
            and not role_pipe_evidence
            and length_mm < _GENERIC_CONNECTABLE_MIN_MM
        ):
            return False
        if (
            allow_weak_annotation_context
            and str(el.get("topology_pipe_evidence") or "") in _WEAK_TOPOLOGY_EVIDENCE
            and length_mm >= _MIN_DANGLING_LENGTH_MM
        ):
            return True
        if length_mm < _GENERIC_CONNECTABLE_MIN_MM and not explicit_evidence:
            return False
        return bool(explicit_evidence)

    if role == "mep" or bool(el.get("flag_for_piping_agent")) or pipe_layer:
        return True

    element_explicit = _has_element_pipe_evidence(el)

    if not element_explicit and not _has_explicit_pipe_evidence(el, run):
        return False

    return True


def _is_arch_or_aux(el: dict) -> bool:
    return str(el.get("layer_role") or "").lower() in {"arch", "aux"}


def _fix_point(point: dict | None) -> dict[str, float] | None:
    if not isinstance(point, dict):
        return None
    try:
        return {"x": round(float(point["x"]), 3), "y": round(float(point["y"]), 3)}
    except (KeyError, TypeError, ValueError):
        return None


def _has_existing_line_connection(
    point: dict | None,
    el_map: dict[str, dict],
    *,
    owner_handle: str,
    target_handle: str,
    unit_factor: float,
) -> bool:
    """Return True when the point is already connected to another line entity."""
    p = _dict_point(point)
    if not p:
        return False
    tol = _EXISTING_CONNECTION_TOL_MM / max(unit_factor, 1e-9)
    excluded = {str(owner_handle or ""), str(target_handle or "")}
    for handle, el in el_map.items():
        if str(handle) in excluded:
            continue
        if _raw_type(el) not in _LINE_TYPES or _is_arch_or_aux(el):
            continue
        if el.get("exclude_from_pipe_topology") or str(el.get("connectivity_role") or "").lower() == "symbol":
            continue
        start, end = _endpoints(el)
        if _point_segment_distance(p, start, end) <= tol:
            return True
    return False


def _pipe_layer(el: dict, fallback: str = _QA_FIX_LAYER) -> str:
    layer = str(el.get("layer") or "").strip()
    if layer and layer.upper() not in {"0", "DEFPOINTS"}:
        return layer
    return fallback


def _text_value(el: dict) -> str:
    return str(el.get("text") or el.get("content") or "").strip()


def _is_orphan_pipe_annotation_text(text: str) -> bool:
    compact = str(text or "").replace(" ", "").upper()
    values = [float(v) for v in re.findall(r"\d+(?:\.\d+)?", text or "")]
    return bool(
        compact in {"GAS", "LPG", "LNG"}
        or _SANITARY_SYSTEM_TEXT_RE.match(text or "")
        or compact.startswith("DN")
        or (compact.endswith("A") and compact[:-1].replace(".", "", 1).isdigit())
        or (
            _PIPE_SIZE_TEXT_RE.match(text or "")
            and values
            and all(10.0 <= v <= 300.0 for v in values)
        )
    )


def _create_line_fix(
    *,
    start: dict | None,
    end: dict | None,
    layer: str,
    reason: str,
) -> dict[str, Any] | None:
    s = _fix_point(start)
    e = _fix_point(end)
    if not s or not e:
        return None
    return {
        "type": "CREATE_ENTITY",
        "new_start": s,
        "new_end": e,
        "new_layer": layer or _QA_FIX_LAYER,
        "reason": reason,
    }


def _create_aligned_gap_fix(
    a_el: dict,
    b_el: dict,
    *,
    start: dict | None,
    end: dict | None,
    layer: str,
    reason: str,
    unit_factor: float,
) -> dict[str, Any] | None:
    s = _dict_point(start)
    e = _dict_point(end)
    if not s or not e:
        return None
    if not (
        _axis_aligned(_line_axis(a_el), s, e, unit_factor=unit_factor)
        and _axis_aligned(_line_axis(b_el), s, e, unit_factor=unit_factor)
    ):
        return None
    return _create_line_fix(start=start, end=end, layer=layer, reason=reason)


def _manual_connection_review_action(
    *,
    start: dict | None,
    end: dict | None,
    reason: str,
) -> dict[str, Any]:
    return {
        "type": "MANUAL_REVIEW",
        "reason": reason,
        "cloud_shape": "SEGMENT",
        "cloud_from": start,
        "cloud_to": end,
    }


def _create_aligned_endpoint_geometry_fix(
    el: dict,
    *,
    endpoint: dict | None,
    target: dict | None,
    reason: str,
    unit_factor: float,
) -> dict[str, Any] | None:
    ep = _dict_point(endpoint)
    tp = _dict_point(target)
    if not ep or not tp or not _axis_aligned(_line_axis(el), ep, tp, unit_factor=unit_factor):
        return None

    rt = _raw_type(el)
    start, end = _endpoints(el)
    target_point = {"x": round(tp[0], 3), "y": round(tp[1], 3)}
    tol = 0.01
    base: dict[str, Any] = {
        "type": "GEOMETRY",
        "modification_tier": 2,
        "reason": reason,
    }

    if rt == "LINE":
        if _dist(ep, start) <= tol:
            return {**base, "new_start": target_point}
        if _dist(ep, end) <= tol:
            return {**base, "new_end": target_point}
        return None

    if rt in {"POLYLINE", "LWPOLYLINE"}:
        raw_vertices = el.get("vertices") or []
        vertices: list[dict[str, float]] = []
        for v in raw_vertices:
            p = _pt({"p": v}, "p") if isinstance(v, dict) else None
            if not p:
                return None
            try:
                bulge = float(v.get("bulge") or 0.0)
            except (TypeError, ValueError):
                bulge = 0.0
            vertices.append({"x": round(p[0], 3), "y": round(p[1], 3), "bulge": round(bulge, 6)})
        if len(vertices) < 2:
            return None
        first = (vertices[0]["x"], vertices[0]["y"])
        last = (vertices[-1]["x"], vertices[-1]["y"])
        if _dist(ep, first) <= tol:
            vertices[0]["x"], vertices[0]["y"] = target_point["x"], target_point["y"]
            return {**base, "new_vertices": vertices}
        if _dist(ep, last) <= tol:
            vertices[-1]["x"], vertices[-1]["y"] = target_point["x"], target_point["y"]
            return {**base, "new_vertices": vertices}
    return None


def _create_overshoot_trim_fix(
    el: dict,
    *,
    touch_point: dict | None,
    overshoot_end: dict | None,
    unit_factor: float,
) -> dict[str, Any] | None:
    touch = _fix_point(touch_point)
    tail = _dict_point(overshoot_end)
    if not touch or not tail:
        return None
    touch_tuple = (touch["x"], touch["y"])
    if not _axis_aligned(_line_axis(el), tail, touch_tuple, unit_factor=unit_factor):
        return None

    rt = _raw_type(el)
    start, end = _endpoints(el)
    tol = 0.01
    base: dict[str, Any] = {
        "type": "GEOMETRY",
        "modification_tier": 2,
        "reason": "Trim overshoot endpoint back to the detected connection point.",
    }

    if rt == "LINE":
        if _dist(tail, start) <= tol:
            return {**base, "new_start": touch}
        if _dist(tail, end) <= tol:
            return {**base, "new_end": touch}
        if start or end:
            start_dist = _dist(tail, start)
            end_dist = _dist(tail, end)
            if start_dist <= end_dist:
                return {**base, "new_start": touch}
            return {**base, "new_end": touch}
        return None

    if rt in {"POLYLINE", "LWPOLYLINE"}:
        raw_vertices = el.get("vertices") or []
        vertices: list[dict[str, float]] = []
        for v in raw_vertices:
            p = _pt({"p": v}, "p") if isinstance(v, dict) else None
            if not p:
                return None
            try:
                bulge = float(v.get("bulge") or 0.0)
            except (TypeError, ValueError):
                bulge = 0.0
            vertices.append({"x": round(p[0], 3), "y": round(p[1], 3), "bulge": round(bulge, 6)})
        if len(vertices) < 2:
            return None
        first = (vertices[0]["x"], vertices[0]["y"])
        last = (vertices[-1]["x"], vertices[-1]["y"])
        if _dist(tail, first) <= tol:
            vertices[0]["x"], vertices[0]["y"] = touch["x"], touch["y"]
            return {**base, "new_vertices": vertices}
        if _dist(tail, last) <= tol:
            vertices[-1]["x"], vertices[-1]["y"] = touch["x"], touch["y"]
            return {**base, "new_vertices": vertices}
        return None

    return None


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
    group_id: str | None = None,
    display_object_id: str | None = None,
    confidence_reason: str = "",
    proposed_action: dict[str, Any] | None = None,
    _weak_annotation_context_allowed: bool = False,
) -> dict[str, Any]:
    issue = {
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
        "proposed_action": proposed_action or {
            "type": "MANUAL_REVIEW",
            "reason": "도면 작성 의도 확인 후 배관 끝점/주석/접속부를 수정하세요.",
        },
    }
    if group_id:
        issue["group_id"] = group_id
    if display_object_id:
        issue["display_object_id"] = display_object_id
    if _weak_annotation_context_allowed:
        issue["_weak_annotation_context_allowed"] = True
    return issue


def make_pipe_annotation_text_issue(
    el: dict,
    corrected_text: str,
    *,
    reason: str,
    confidence_score: float = 0.82,
    confidence_reason: str = "pipe_annotation_text_llm_normalization",
) -> dict[str, Any] | None:
    original = str(el.get("_qa_original_text") or _text_value(el)).strip()
    corrected = str(corrected_text or "").strip()
    if not original or not corrected or corrected == original:
        return None
    h = _handle(el)
    return _issue(
        equipment_id=h,
        issue_type="drawing_quality_pipe_annotation_text",
        reason=(
            f"배관 주석 {original!r}에 표기 오류 후보가 있습니다. "
            f"자동 검수 결과 {corrected!r} 표기가 더 적절합니다. ({reason})"
        ),
        current_value=original,
        required_value=corrected,
        confidence_score=confidence_score,
        position=_position(el),
        related_handles=[h] if h else [],
        confidence_reason=confidence_reason,
        proposed_action={
            "type": "TEXT_CONTENT",
            "new_text": corrected,
            "modification_tier": 1,
            "reason": "배관 주석 문구를 검수 결과에 맞게 정리합니다.",
        },
    )


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

    # 1) Annotation-bridged gaps are normal drafting notation. A "G", "20A",
    # "DN20" etc. can intentionally interrupt a plotted pipe line so the text
    # stays readable; these exact pairs should not become QA gap/mismatch issues.
    virtual_pairs: set[frozenset[str]] = set()
    virtual_handles: set[str] = set()
    virtual_annotation_handles: set[str] = set()
    for vc in topology.get("virtual_connections", []) or []:
        ha = str(vc.get("from_handle") or "")
        hb = str(vc.get("to_handle") or "")
        if ha and hb:
            virtual_pairs.add(frozenset((ha, hb)))
            virtual_handles.update((ha, hb))
        virtual_annotation_handles.update(
            str(h)
            for h in (vc.get("annotation_handles") or [])
            if h
        )
    annotation_points = _pipe_annotation_points(elements or [])

    # 2) Near-miss candidates from topology are drafting QA issues even when the
    # legal checker refuses to make a hard regulation claim.
    for gap in topology.get("broken_gaps", []) or []:
        ha = str(gap.get("from_handle") or "")
        hb = str(gap.get("to_handle") or "")
        if frozenset((ha, hb)) in virtual_pairs:
            continue
        ea = el_map.get(ha, {})
        eb = el_map.get(hb, {})
        run_a = run_by_handle.get(ha)
        run_b = run_by_handle.get(hb)
        if _is_pipe_symbol_fragment(ea) or _is_pipe_symbol_fragment(eb):
            continue
        allow_weak_annotation_context = _allow_weak_context_for_qa_pair(
            ea,
            eb,
            run_a,
            run_b,
            unit_factor=unit_factor,
        )
        if not (
            _is_connectable_pipe_geometry(
                ea,
                run_a,
                unit_factor=unit_factor,
                allow_weak_annotation_context=allow_weak_annotation_context,
            )
            and _is_connectable_pipe_geometry(
                eb,
                run_b,
                unit_factor=unit_factor,
                allow_weak_annotation_context=allow_weak_annotation_context,
            )
        ):
            continue
        gap_mm = float(gap.get("gap_mm") or 0)
        if gap_mm <= 0:
            continue
        weak_generic_pair = (
            _is_weak_generic_pipe_candidate(ea, run_a)
            or _is_weak_generic_pipe_candidate(eb, run_b)
        )
        if (
            weak_generic_pair
            and allow_weak_annotation_context
            and gap_mm < _MIN_DANGLING_LENGTH_MM
        ):
            continue
        proposed = _create_aligned_gap_fix(
            ea,
            eb,
            start=gap.get("from_point"),
            end=gap.get("to_point"),
            layer=_pipe_layer(ea),
            reason="끊어진 배관 끝점 사이에 연결 선분을 생성합니다.",
            unit_factor=unit_factor,
        )
        if proposed:
            proposed.update({
                "cloud_shape": "SEGMENT",
                "cloud_from": gap.get("from_point"),
                "cloud_to": gap.get("to_point"),
            })
        else:
            if weak_generic_pair:
                continue
            proposed = _manual_connection_review_action(
                start=gap.get("from_point"),
                end=gap.get("to_point"),
                reason=(
                    "Detected pipe endpoints are close, but the gap is not aligned "
                    "with both pipe axes. Review endpoint and angle before fixing."
                ),
            )
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
            group_id=f"pipe_gap:{ha}:{hb}",
            display_object_id=f"{ha} <-> {hb}",
            confidence_reason=(
                "topology_broken_gap_aligned_weak_generic"
                if weak_generic_pair
                else "topology_broken_gap"
            ),
            proposed_action=proposed,
        ))

    for item in topology.get("connection_mismatches", []) or []:
        ha = str(item.get("endpoint_handle") or "")
        hb = str(item.get("segment_handle") or "")
        if frozenset((ha, hb)) in virtual_pairs:
            continue
        ea = el_map.get(ha, {})
        eb = el_map.get(hb, {})
        run_a = run_by_handle.get(ha)
        run_b = run_by_handle.get(hb)
        if _is_pipe_symbol_fragment(ea) or _is_pipe_symbol_fragment(eb):
            continue
        allow_weak_annotation_context = (
            ha in virtual_handles
            or hb in virtual_handles
            or _allow_weak_context_for_qa_pair(
                ea,
                eb,
                run_a,
                run_b,
                unit_factor=unit_factor,
            )
        )
        if not (
            _is_connectable_pipe_geometry(
                ea,
                run_a,
                unit_factor=unit_factor,
                allow_weak_annotation_context=allow_weak_annotation_context,
            )
            and _is_connectable_pipe_geometry(
                eb,
                run_b,
                unit_factor=unit_factor,
                allow_weak_annotation_context=allow_weak_annotation_context,
            )
        ):
            continue
        offset_mm = float(item.get("offset_mm") or 0)
        if offset_mm <= 0:
            continue
        if _has_existing_line_connection(
            item.get("endpoint"),
            el_map,
            owner_handle=ha,
            target_handle=hb,
            unit_factor=unit_factor,
        ):
            continue
        if (
            _has_only_annotation_style_evidence(ea, run_a)
            or _has_only_annotation_style_evidence(eb, run_b)
        ) and not allow_weak_annotation_context:
            continue
        weak_generic_pair = (
            _is_weak_generic_pipe_candidate(ea, run_a)
            or _is_weak_generic_pipe_candidate(eb, run_b)
        )
        proposed = _create_aligned_endpoint_geometry_fix(
            ea,
            endpoint=item.get("endpoint"),
            target=item.get("nearest_point"),
            reason="Extend the pipe endpoint along its existing axis to the detected connection point.",
            unit_factor=unit_factor,
        )
        if proposed:
            proposed.update({
                "cloud_shape": "SEGMENT",
                "cloud_from": item.get("endpoint"),
                "cloud_to": item.get("nearest_point"),
            })
        else:
            if weak_generic_pair:
                continue
            proposed = _manual_connection_review_action(
                start=item.get("endpoint"),
                end=item.get("nearest_point"),
                reason=(
                    "Detected connection point is not aligned with the existing "
                    "pipe axis. Review endpoint and angle before fixing."
                ),
            )
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
            group_id=f"pipe_connection:{ha}:{hb}",
            display_object_id=f"{ha} <-> {hb}",
            confidence_reason=(
                "topology_connection_mismatch_aligned_weak_generic"
                if weak_generic_pair
                else "topology_connection_mismatch"
            ),
            proposed_action=proposed,
        ))

    for item in topology.get("connection_overshoots", []) or []:
        ha = str(item.get("connection_handle") or "")
        hb = str(item.get("overshoot_handle") or "")
        if frozenset((ha, hb)) in virtual_pairs:
            continue
        ea = el_map.get(ha, {})
        eb = el_map.get(hb, {})
        run_a = run_by_handle.get(ha)
        run_b = run_by_handle.get(hb)
        if _is_pipe_symbol_fragment(ea) or _is_pipe_symbol_fragment(eb):
            continue
        allow_weak_annotation_context = (
            ha in virtual_handles
            or hb in virtual_handles
            or _allow_weak_context_for_qa_pair(
                ea,
                eb,
                run_a,
                run_b,
                unit_factor=unit_factor,
            )
        )
        if not (
            _is_connectable_pipe_geometry(
                ea,
                run_a,
                unit_factor=unit_factor,
                allow_weak_annotation_context=allow_weak_annotation_context,
            )
            and _is_connectable_pipe_geometry(
                eb,
                run_b,
                unit_factor=unit_factor,
                allow_weak_annotation_context=allow_weak_annotation_context,
            )
        ):
            continue
        overshoot_mm = float(item.get("overshoot_mm") or 0)
        if overshoot_mm <= 0:
            continue
        if _has_existing_line_connection(
            item.get("overshoot_end"),
            el_map,
            owner_handle=hb,
            target_handle=ha,
            unit_factor=unit_factor,
        ):
            continue
        if (
            _has_only_annotation_style_evidence(ea, run_a)
            or _has_only_annotation_style_evidence(eb, run_b)
        ) and not allow_weak_annotation_context:
            continue
        weak_generic_pair = (
            _is_weak_generic_pipe_candidate(ea, run_a)
            or _is_weak_generic_pipe_candidate(eb, run_b)
        )
        proposed_action = _create_overshoot_trim_fix(
            eb,
            touch_point=item.get("touch_point"),
            overshoot_end=item.get("overshoot_end"),
            unit_factor=unit_factor,
        )
        if not proposed_action and weak_generic_pair:
            continue
        proposed_action = proposed_action or {
            "type": "MANUAL_REVIEW",
            "reason": "Review whether the short tail should be trimmed at the connection point.",
        }
        proposed_action.update({
            "cloud_shape": "L",
            "connection_handle": ha,
            "overshoot_handle": hb,
            "touch_point": item.get("touch_point"),
            "overshoot_end": item.get("overshoot_end"),
        })
        issues.append(_issue(
            equipment_id=hb,
            issue_type="drawing_quality_connection_overshoot",
            reason=(
                f"배관 접속부에서 {hb!r} 선분이 {ha!r} 접점 밖으로 "
                f"{overshoot_mm:.0f}mm 돌출되어 있습니다."
            ),
            current_value=f"접속부 돌출 {overshoot_mm:.0f}mm",
            required_value="접점 기준으로 불필요한 짧은 돌출부 제거 또는 의도적 분기 확인",
            confidence_score=0.76,
            position=_position(eb),
            related_handles=[ha, hb],
            group_id=f"pipe_overshoot:{ha}:{hb}",
            display_object_id=f"{ha} <-> {hb}",
            confidence_reason="topology_connection_overshoot",
            proposed_action=proposed_action,
            _weak_annotation_context_allowed=allow_weak_annotation_context,
        ))

    # 3) Dangling single-line runs. Keep confidence lower unless explicit pipe
    # evidence exists, because endpoints can be intentional drawing boundaries.
    gap_or_mismatch_handles = {
        str(h)
        for item in [
            *(topology.get("broken_gaps") or []),
            *(topology.get("connection_mismatches") or []),
            *(topology.get("connection_overshoots") or []),
        ]
        for h in (
            item.get("from_handle"),
            item.get("to_handle"),
            item.get("endpoint_handle"),
            item.get("segment_handle"),
            item.get("connection_handle"),
            item.get("overshoot_handle"),
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
        if not el or not _is_connectable_pipe_geometry(el, run, unit_factor=unit_factor):
            continue
        if _is_pipe_symbol_fragment(el):
            continue
        if _generic_line_near_pipe_annotation(el, annotation_points, unit_factor):
            continue
        length_mm = float(run.get("total_length_mm") or 0) or _line_length_mm(el, unit_factor)
        if length_mm < _MIN_DANGLING_LENGTH_MM:
            continue
        if (
            length_mm < _GENERIC_CONNECTABLE_MIN_MM
            and _has_only_annotation_style_evidence(el, run)
        ):
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
        if not _is_orphan_pipe_annotation_text(text):
            continue
        h = _handle(el)
        if h in virtual_annotation_handles:
            continue
        if not line_segments:
            continue
        pos_candidates = _annotation_measure_points(el)
        if not pos_candidates:
            continue
        nearest = min(
            (
                _point_segment_distance(pos, eps[0], eps[1]) * unit_factor
                for eps, _line in line_segments
                for pos in pos_candidates
            ),
            default=math.inf,
        )
        if nearest <= _ORPHAN_ANNOTATION_NEAR_MM:
            continue
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

    # Deduplicate by geometry group when available. A single handle can have
    # multiple independent gap/mismatch issues, so handle+type alone is too
    # coarse for topology QA.
    def _dedupe_key(issue: dict) -> tuple:
        issue_type = str(issue.get("issue_type") or issue.get("violation_type") or "")
        group_id = str(issue.get("group_id") or "")
        if group_id:
            return ("group", issue_type, group_id)
        related = tuple(
            sorted(str(h) for h in (issue.get("related_handles") or []) if h)
        )
        if related:
            return ("related", issue_type, *related)
        return ("single", issue_type, str(issue.get("equipment_id") or ""))

    deduped: dict[tuple, dict] = {}
    for issue in issues:
        key = _dedupe_key(issue)
        prev = deduped.get(key)
        if prev is None or float(issue.get("confidence_score") or 0) > float(prev.get("confidence_score") or 0):
            deduped[key] = issue
    return list(deduped.values())
