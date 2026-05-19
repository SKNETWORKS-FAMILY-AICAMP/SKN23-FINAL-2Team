"""
File    : backend/services/agents/piping/sub/topology.py
Author  : 송주엽
Create  : 2026-04-24
Description : CAD LINE 끝점 근접으로 배관 경로(pipe_run)를 구성하고,
              연결된 블록(밸브·장비)을 식별합니다.
              전기 에이전트 TopologyBuilder 패턴을 배관 도메인에 적용.

출력 스키마:
  pipe_runs: [
    {
      run_id         : int,
      handles        : [str, ...],          # 구성 LINE handle
      total_length   : float,               # 경로 총 길이 (도면 단위)
      total_length_mm: float,               # 경로 총 길이 (mm 정규화)
      connected_blocks: [str, ...],         # 끝점 근체 BLOCK handle
      material       : str,                 # 구성 엔티티에서 추론 (GAS 등)
      diameter_mm    : float,
    }, ...
  ]
  equipment_graph : {block_handle: [연결된 block_handle, ...]}
  summary : {run_count, total_lines, unconnected_lines, block_count, unit_factor}

Modification History :
    - 2026-04-29 (송주엽) : unit_factor 파라미터 추가, total_length_mm 출력
"""
from __future__ import annotations

import logging
import math
import re
from collections import defaultdict
from typing import Any

_log = logging.getLogger(__name__)

_MAX_LINES = 2000         # 초과 시 경고 후 첫 N개만 처리
_CONN_TOL = 1.0           # 선-선 끝점 허용오차(mm). 실제 gap은 QA로 드러나게 작게 둔다.
_BLOCK_CONN_TOL = 150.0   # 블록-끝점 허용오차(mm). 심볼 삽입점은 선 끝점과 약간 어긋날 수 있다.

_LINE_RAW  = frozenset({"LINE", "ARC", "POLYLINE", "LWPOLYLINE", "SPLINE"})
_BLOCK_RAW = frozenset({"INSERT", "BLOCK"})
_TEXT_RAW  = frozenset({"TEXT", "MTEXT", "MLEADER"})

_ANNOTATION_GAP_MAX_MM = 1500.0
_BROKEN_GAP_MAX_MM = 3000.0
_CONNECTION_OVERSHOOT_MAX_MM = 300.0
_CONNECTION_OVERSHOOT_MAIN_MIN_MM = 300.0
_ANNOTATION_NEAR_TOL_MM = 120.0
_ANGLE_COS_TOL = math.cos(math.radians(8.0))
_GAP_AXIS_COS_TOL = math.cos(math.radians(12.0))
_PIPE_ANNOTATION_RE = re.compile(
    r"^(?:G|GAS|LPG|LNG|DN\s*\d+(?:\.\d+)?|\d{1,4}(?:\.\d+)?\s*A)$",
    re.IGNORECASE,
)
_PIPE_SIZE_TEXT_RE = re.compile(r"^\s*\d{2,3}(?:\s+\d{2,3}){0,2}\s*$")
_SANITARY_SYSTEM_TEXT_RE = re.compile(r"^\s*(?:D|S|V|SD|FD|VP|CW|HW|CWS|HWS)\s*$", re.IGNORECASE)
_PIPE_LAYER_RE = re.compile(
    r"GAS|PIPE|PIPING|배관|급수|급탕|배수|위생|소화|SPRINK|CWS|HWS|FIRE|"
    r"^P[-_]|^M[-_]",
    re.IGNORECASE,
)
_NON_PIPE_LINE_LAYER_RE = re.compile(
    r"DIM|치수|ANNO|ANNOTATION|TXT[-_]?|TEXT[-_]?|TITLE|FRAME|BORDER|"
    r"LEADER|지시|인출|NOTE|LABEL|CENTER|CENTRE|CEN|HATCH|DEFPOINTS|"
    r"GRID|VIEWPORT|XREF",
    re.IGNORECASE,
)
_TITLE_TEXT_RE = re.compile(
    r"평면도|계통도|단면도|전개도|SCALE|DATE|도면번호|DWG\s*NO|PROJECT|도면명|축척",
    re.IGNORECASE,
)
_TITLE_TEXT_FALLBACK_RE = re.compile(
    r"평면도|도면명|도면번호|축척|SCALE|DRAWING|DWG\s*NO|PROJECT|DATE|A1\s*:|A3\s*:",
    re.IGNORECASE,
)
_TITLE_GRAPHIC_NEAR_TOL_MM = 120.0
_SYMBOL_LINE_MAX_MM = 80.0
_WEAK_UNKNOWN_LINE_MIN_MM = 300.0
_ANNOTATION_LINE_NEAR_MM = 300.0
_GENERIC_CONNECTABLE_MIN_MM = 1500.0
_GENERIC_HINT_LAYER_MAX_LINES = 200
_GENERIC_LAYER_RE = re.compile(r"^(?:L\d+|LAYER\d*|\d+)$", re.IGNORECASE)


# ── 좌표 헬퍼 ────────────────────────────────────────────────────────────────

def _pt(d: dict, *keys) -> tuple[float, float] | None:
    for k in keys:
        p = d.get(k)
        if isinstance(p, dict) and "x" in p:
            try:
                return float(p["x"]), float(p["y"])
            except (TypeError, ValueError):
                pass
    return None


def _dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _color_key(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        if {"r", "g", "b"}.issubset(value):
            return f"rgb({value.get('r')},{value.get('g')},{value.get('b')})"
        return str(sorted(value.items()))
    return str(value).strip()


def _sub(a: tuple[float, float], b: tuple[float, float]) -> tuple[float, float]:
    return a[0] - b[0], a[1] - b[1]


def _dot(a: tuple[float, float], b: tuple[float, float]) -> float:
    return a[0] * b[0] + a[1] * b[1]


def _cross(a: tuple[float, float], b: tuple[float, float]) -> float:
    return a[0] * b[1] - a[1] * b[0]


def _unit(v: tuple[float, float]) -> tuple[float, float] | None:
    # 입력은 mm 단위 좌표 차 벡터로 가정. float64 정밀도(~15자리)와 도면 mm 좌표
    # 범위(보통 0~수십만 mm) 하에서 절대 epsilon 1e-9 mm 는 충분히 보수적이며,
    # 정규화된 좌표(0~1)에서도 영벡터를 충분히 잡아낸다.
    length = math.hypot(v[0], v[1])
    if length <= 1e-9:
        return None
    return v[0] / length, v[1] / length


def _endpoints(e: dict) -> tuple[tuple[float, float] | None, tuple[float, float] | None]:
    """LINE / POLYLINE 엔티티에서 (시작점, 끝점) 추출."""
    rt = str(e.get("raw_type") or "").upper()
    if rt in ("POLYLINE", "LWPOLYLINE"):
        vs = e.get("vertices") or []
        if len(vs) >= 2:
            try:
                return (float(vs[0]["x"]), float(vs[0]["y"])), (float(vs[-1]["x"]), float(vs[-1]["y"]))
            except (KeyError, TypeError, ValueError):
                pass
    return _pt(e, "start"), _pt(e, "end")


def _line_axis(eps: tuple[tuple[float, float] | None, tuple[float, float] | None]) -> tuple[float, float] | None:
    s, e = eps
    if not s or not e:
        return None
    return _unit(_sub(e, s))


def _point_segment_distance(
    p: tuple[float, float],
    a: tuple[float, float],
    b: tuple[float, float],
) -> float:
    ab = _sub(b, a)
    ab2 = _dot(ab, ab)
    if ab2 <= 1e-9:
        return _dist(p, a)
    t = max(0.0, min(1.0, _dot(_sub(p, a), ab) / ab2))
    proj = (a[0] + ab[0] * t, a[1] + ab[1] * t)
    return _dist(p, proj)


def _point_segment_projection(
    p: tuple[float, float],
    a: tuple[float, float],
    b: tuple[float, float],
) -> tuple[float, tuple[float, float], float]:
    """Return distance, projected point, and segment parameter t in [0, 1]."""
    ab = _sub(b, a)
    ab2 = _dot(ab, ab)
    if ab2 <= 1e-9:
        return _dist(p, a), a, 0.0
    t = max(0.0, min(1.0, _dot(_sub(p, a), ab) / ab2))
    proj = (a[0] + ab[0] * t, a[1] + ab[1] * t)
    return _dist(p, proj), proj, t


def _line_intersection_params(
    a1: tuple[float, float],
    a2: tuple[float, float],
    b1: tuple[float, float],
    b2: tuple[float, float],
) -> tuple[float, float, tuple[float, float]] | None:
    av = _sub(a2, a1)
    bv = _sub(b2, b1)
    denom = _cross(av, bv)
    if abs(denom) <= 1e-12:
        return None
    delta = _sub(b1, a1)
    ta = _cross(delta, bv) / denom
    tb = _cross(delta, av) / denom
    point = (a1[0] + av[0] * ta, a1[1] + av[1] * ta)
    return ta, tb, point


def _segments_cross(
    a1: tuple[float, float],
    a2: tuple[float, float],
    b1: tuple[float, float],
    b2: tuple[float, float],
    tol: float,
) -> bool:
    hit = _line_intersection_params(a1, a2, b1, b2)
    if not hit:
        return False
    ta, tb, _point = hit
    a_len = max(_dist(a1, a2), 1e-9)
    b_len = max(_dist(b1, b2), 1e-9)
    a_slop = tol / a_len
    b_slop = tol / b_len
    return -a_slop <= ta <= 1.0 + a_slop and -b_slop <= tb <= 1.0 + b_slop


def _endpoint_axis_extension_to_segment(
    endpoint: tuple[float, float],
    other_endpoint: tuple[float, float],
    seg_a: tuple[float, float],
    seg_b: tuple[float, float],
    *,
    physical_tol: float,
    mismatch_max: float,
) -> tuple[float, tuple[float, float]] | None:
    hit = _line_intersection_params(other_endpoint, endpoint, seg_a, seg_b)
    if not hit:
        return None
    t_endpoint_axis, t_segment, point = hit
    axis_length = _dist(other_endpoint, endpoint)
    extension = (t_endpoint_axis - 1.0) * axis_length
    if not (physical_tol < extension <= mismatch_max):
        return None
    if not (0.02 < t_segment < 0.98):
        return None
    return extension, point


def _bbox_from_eps(
    eps: tuple[tuple[float, float] | None, tuple[float, float] | None],
) -> tuple[float, float, float, float] | None:
    pts = [p for p in eps if p is not None]
    if len(pts) < 2:
        return None
    return (
        min(p[0] for p in pts),
        min(p[1] for p in pts),
        max(p[0] for p in pts),
        max(p[1] for p in pts),
    )


def _bbox_near(
    a: tuple[float, float, float, float] | None,
    b: tuple[float, float, float, float] | None,
    tol: float,
) -> bool:
    if not a or not b:
        return False
    return not (
        a[2] + tol < b[0]
        or b[2] + tol < a[0]
        or a[3] + tol < b[1]
        or b[3] + tol < a[1]
    )


def _line_near_title_text(
    eps: tuple[tuple[float, float] | None, tuple[float, float] | None],
    title_extents: list[tuple[float, float, float, float]],
    tol: float,
) -> bool:
    line_bbox = _bbox_from_eps(eps)
    return any(_bbox_near(line_bbox, title_bbox, tol) for title_bbox in title_extents)


def _bbox_center(e: dict) -> tuple[float, float] | None:
    b = e.get("bbox")
    if not isinstance(b, dict):
        return None
    try:
        if "x1" in b:
            return (float(b["x1"]) + float(b["x2"])) / 2, (float(b["y1"]) + float(b["y2"])) / 2
        if "min_x" in b:
            return (float(b["min_x"]) + float(b["max_x"])) / 2, (float(b["min_y"]) + float(b["max_y"])) / 2
    except (KeyError, TypeError, ValueError):
        pass
    return None


def _bbox_extents(e: dict) -> tuple[float, float, float, float] | None:
    b = e.get("bbox")
    if isinstance(b, dict):
        try:
            if "x1" in b:
                return float(b["x1"]), float(b["y1"]), float(b["x2"]), float(b["y2"])
            if "min_x" in b:
                return float(b["min_x"]), float(b["min_y"]), float(b["max_x"]), float(b["max_y"])
        except (KeyError, TypeError, ValueError):
            return None

    p = _pt(e, "position", "insert_point", "center")
    if p:
        return p[0], p[1], p[0], p[1]
    return None


def _annotation_pos(e: dict) -> tuple[float, float] | None:
    # AutoCAD TEXT insertion points are often on the baseline/lower corner, so
    # use the visual bbox center first when available.
    return _bbox_center(e) or _pt(e, "center", "position", "insert_point")


def _annotation_text(e: dict) -> str:
    return str(e.get("text") or e.get("content") or "").strip()


def _is_title_text(e: dict) -> bool:
    text = _annotation_text(e)
    return bool(_TITLE_TEXT_RE.search(text) or _TITLE_TEXT_FALLBACK_RE.search(text))


def _normal_text(text: str) -> str:
    return re.sub(r"\s+", "", text).upper()


def _is_pipe_annotation(text: str) -> bool:
    if not text:
        return False
    if _PIPE_ANNOTATION_RE.match(_normal_text(text)):
        return True
    if _SANITARY_SYSTEM_TEXT_RE.match(text):
        return True
    if not _PIPE_SIZE_TEXT_RE.match(text):
        return False
    values = [float(v) for v in re.findall(r"\d+(?:\.\d+)?", text)]
    return bool(values) and all(10.0 <= v <= 300.0 for v in values)


def _annotation_material(texts: list[str]) -> str | None:
    for text in texts:
        normal = _normal_text(text)
        if normal.startswith(("G", "LPG", "LNG")):
            return "GAS"
        if normal in {"D", "S", "SD", "FD"}:
            return "DRAIN"
        if normal in {"CW", "CWS", "HW", "HWS"}:
            return "WATER"
    return None


def _annotation_diameter(texts: list[str]) -> float | None:
    for text in texts:
        m = re.search(r"(\d+(?:\.\d+)?)", text)
        if m:
            try:
                value = float(m.group(1))
            except ValueError:
                continue
            if 0 < value <= 1000:
                return value
    return None


def _line_length(
    eps: tuple[tuple[float, float] | None, tuple[float, float] | None],
) -> float:
    s, e = eps
    return _dist(s, e) if s and e else 0.0


def _has_explicit_pipe_attrs(e: dict) -> bool:
    attrs = e.get("attributes") or {}
    return bool(
        e.get("diameter_mm")
        or e.get("pressure_mpa")
        or e.get("slope_pct")
        or str(e.get("material") or "").upper() not in ("", "UNKNOWN", "NONE")
        or attrs.get("SIZE")
        or attrs.get("DIAMETER")
        or attrs.get("MATERIAL")
        or attrs.get("TAG_NAME")
        or attrs.get("PRESSURE")
        or attrs.get("SLOPE")
    )


def _is_closed_polyline_symbol(e: dict) -> bool:
    """Closed polylines are usually frames/symbol outlines, not pipe centerlines."""
    rt = str(e.get("raw_type") or e.get("type") or "").upper()
    if rt not in {"POLYLINE", "LWPOLYLINE"}:
        return False
    if bool(e.get("is_closed")):
        return True
    try:
        return float(e.get("area") or 0) > 0
    except (TypeError, ValueError):
        return False


def _is_generic_layer(layer: str) -> bool:
    return bool(_GENERIC_LAYER_RE.match((layer or "").strip()))


def _polyline_symbol_shape(
    e: dict,
    eps: tuple[tuple[float, float] | None, tuple[float, float] | None],
    unit_factor: float,
) -> bool:
    """Detect symbol/frame-like open polylines on ambiguous project layers.

    Project drawings use layer names differently per user. A nearby pipe note
    alone should not promote a rectangular or zig-zag symbol outline to a pipe
    centerline.
    """
    rt = str(e.get("raw_type") or e.get("type") or "").upper()
    if rt not in {"POLYLINE", "LWPOLYLINE"}:
        return False
    if _is_closed_polyline_symbol(e):
        return True

    vertices = e.get("vertices") or []
    if len(vertices) < 4:
        return False

    bbox = _bbox_extents(e)
    if not bbox:
        pts: list[tuple[float, float]] = []
        for v in vertices:
            if not isinstance(v, dict):
                continue
            p = _pt({"p": v}, "p")
            if p:
                pts.append(p)
        if pts:
            bbox = (
                min(p[0] for p in pts),
                min(p[1] for p in pts),
                max(p[0] for p in pts),
                max(p[1] for p in pts),
            )
    if not bbox:
        bbox = _bbox_from_eps(eps)
    if not bbox:
        return False
    w = abs(bbox[2] - bbox[0]) * unit_factor
    h = abs(bbox[3] - bbox[1]) * unit_factor
    if min(w, h) < 20.0:
        return False
    aspect = max(w, h) / max(min(w, h), 1e-9)
    return aspect < 8.0


def _line_near_pipe_annotation(
    e: dict,
    eps: tuple[tuple[float, float] | None, tuple[float, float] | None],
    annotations: list[dict],
    near_tol: float,
) -> bool:
    s, en = eps
    if not s or not en:
        return False
    for ann in annotations:
        pos = _annotation_pos(ann)
        if pos and _point_segment_distance(pos, s, en) <= near_tol:
            return True
    return False


def _build_layer_pipe_color_hints(
    raw_lines: list[dict],
    endpoints: dict[str, tuple[tuple[float, float] | None, tuple[float, float] | None]],
    annotations: list[dict],
    unit_factor: float,
    layer_line_counts: dict[str, int] | None = None,
) -> dict[str, set[str]]:
    near_tol = _ANNOTATION_LINE_NEAR_MM / max(unit_factor, 1e-9)
    hints: dict[str, set[str]] = defaultdict(set)
    for line in raw_lines:
        handle = str(line.get("handle") or "")
        eps = endpoints.get(handle)
        if not eps:
            continue
        color = _color_key(line.get("color"))
        if not color or _line_length(eps) * unit_factor < _SYMBOL_LINE_MAX_MM:
            continue
        if _line_near_pipe_annotation(line, eps, annotations, near_tol):
            layer = str(line.get("layer") or "")
            if (
                _is_generic_layer(layer)
                and layer_line_counts
                and layer_line_counts.get(layer, 0) > _GENERIC_HINT_LAYER_MAX_LINES
            ):
                continue
            if layer:
                hints[layer].add(color)
    return dict(hints)


def _is_pipe_run_line(
    e: dict,
    eps: tuple[tuple[float, float] | None, tuple[float, float] | None],
    *,
    unit_factor: float,
    annotations: list[dict],
    title_extents: list[tuple[float, float, float, float]],
    layer_color_hints: dict[str, set[str]],
    layer_line_counts: dict[str, int] | None = None,
) -> tuple[bool, str]:
    layer = str(e.get("layer") or "")
    role = str(e.get("layer_role") or "").lower()
    length_mm = _line_length(eps) * unit_factor
    explicit = _has_explicit_pipe_attrs(e)
    generic_layer = _is_generic_layer(layer)
    strong_pipe_layer = bool(_PIPE_LAYER_RE.search(layer))
    promoted = bool(e.get("flag_for_piping_agent"))
    pipe_layer = strong_pipe_layer or role == "mep" or promoted
    color = _color_key(e.get("color"))
    hinted_colors = layer_color_hints.get(layer) or set()
    near_annotation = _line_near_pipe_annotation(
        e,
        eps,
        annotations,
        _ANNOTATION_LINE_NEAR_MM / max(unit_factor, 1e-9),
    ) or str(e.get("topology_pipe_evidence") or "") == "near_pipe_annotation"
    color_mismatch = bool(hinted_colors and color and color not in hinted_colors)
    hinted_generic_pipe_style = bool(
        generic_layer
        and hinted_colors
        and color
        and color in hinted_colors
        and (layer_line_counts or {}).get(layer, 0) <= _GENERIC_HINT_LAYER_MAX_LINES
    )

    if not all(eps):
        return False, "missing_endpoints"
    if _is_closed_polyline_symbol(e) and not explicit:
        return False, "closed_polyline_symbol"
    if role in {"arch", "aux"} and not explicit:
        return False, f"layer_role_{role}"
    if layer and _NON_PIPE_LINE_LAYER_RE.search(layer) and not explicit:
        return False, "annotation_or_dim_layer"
    if length_mm <= 0:
        return False, "zero_length"
    if not explicit and _line_near_title_text(
        eps,
        title_extents,
        _TITLE_GRAPHIC_NEAR_TOL_MM / max(unit_factor, 1e-9),
    ):
        return False, "title_block_graphic"

    if generic_layer and not strong_pipe_layer:
        source = str(e.get("source_attributes") or "").lower()
        if source == "text_extracted" and not pipe_layer and length_mm < _GENERIC_CONNECTABLE_MIN_MM:
            return False, "generic_layer_text_extracted_symbol"
        if (
            length_mm < _GENERIC_CONNECTABLE_MIN_MM
            and not (pipe_layer or explicit or near_annotation or hinted_generic_pipe_style)
        ):
            return False, "generic_layer_short_symbol"
        if _polyline_symbol_shape(e, eps, unit_factor):
            return False, "generic_layer_polyline_symbol"

    has_pipe_evidence = pipe_layer or explicit or near_annotation or hinted_generic_pipe_style

    if (
        near_annotation
        and not pipe_layer
        and not explicit
        and generic_layer
        and _polyline_symbol_shape(e, eps, unit_factor)
    ):
        return False, "generic_layer_polyline_symbol_annotation_only"

    if length_mm < _SYMBOL_LINE_MAX_MM and not explicit and not near_annotation:
        return False, "short_symbol_line"

    if not has_pipe_evidence:
        if length_mm < _WEAK_UNKNOWN_LINE_MIN_MM:
            reason = "weak_unknown_color_mismatch_line" if color_mismatch else "weak_unknown_short_line"
            return False, reason
        return False, "weak_unknown_no_pipe_evidence"

    if not pipe_layer and not explicit and not near_annotation and length_mm < _WEAK_UNKNOWN_LINE_MIN_MM:
        reason = "weak_unknown_color_mismatch_line" if color_mismatch else "weak_unknown_short_line"
        return False, reason

    if hinted_generic_pipe_style:
        e["topology_pipe_evidence"] = "layer_annotation_style"

    if hinted_generic_pipe_style and not (pipe_layer or explicit or near_annotation):
        return True, "pipe_candidate_layer_annotation_style"
    return True, "pipe_candidate_promoted" if promoted else "pipe_candidate"


def _pipe_system_hint(e: dict) -> str:
    material = str(e.get("material") or "").strip().upper()
    if material and material not in {"UNKNOWN", "NONE"}:
        return material

    layer = str(e.get("layer") or "").upper()
    if "GAS" in layer or "LPG" in layer or "LNG" in layer:
        return "GAS"
    if "SPRINK" in layer or "FIRE" in layer or "소화" in layer:
        return "FIRE"
    if "CWS" in layer or "급수" in layer or "냉수" in layer:
        return "WATER_SUPPLY"
    if "HWS" in layer or "온수" in layer:
        return "HOT_WATER"
    if "배수" in layer or "DRAIN" in layer:
        return "DRAIN"
    return ""


def _same_pipe_style(a: dict, b: dict) -> bool:
    a_layer = str(a.get("layer") or "")
    b_layer = str(b.get("layer") or "")
    a_system = _pipe_system_hint(a)
    b_system = _pipe_system_hint(b)
    if a_system and b_system and a_system != b_system:
        return False

    # CAD standards vary by project. Adjacent fragments can land on different
    # user layers, so layer/color are weak hints; explicit system conflicts are
    # the only hard split here.
    for key in ("linetype", "lineweight"):
        av = a.get(key)
        bv = b.get(key)
        if av is not None and bv is not None and av != bv:
            return False
    return True


def _has_physical_pipe_touch(
    a: dict,
    b: dict,
    a_eps: tuple[tuple[float, float] | None, tuple[float, float] | None],
    b_eps: tuple[tuple[float, float] | None, tuple[float, float] | None],
    tol: float,
) -> bool:
    if not _same_pipe_style(a, b):
        return False

    a_pts = [p for p in a_eps if p is not None]
    b_pts = [p for p in b_eps if p is not None]
    if len(a_pts) < 2 or len(b_pts) < 2:
        return False

    if any(_dist(pa, pb) <= tol for pa in a_pts for pb in b_pts):
        return True

    # T-junction: one pipe endpoint lands on the middle of another pipe segment.
    if (
        any(_point_segment_distance(pa, b_pts[0], b_pts[1]) <= tol for pa in a_pts)
        or any(_point_segment_distance(pb, a_pts[0], a_pts[1]) <= tol for pb in b_pts)
    ):
        return True

    # Diagonal branches can meet in the middle of both CAD segments. Treat an
    # actual same-style segment crossing as a physical pipe touch so X/T/Y
    # branch geometry is not left as two disconnected runs.
    return _segments_cross(a_pts[0], a_pts[1], b_pts[0], b_pts[1], tol)


def _nearest_endpoint_pair(
    a_eps: tuple[tuple[float, float] | None, tuple[float, float] | None],
    b_eps: tuple[tuple[float, float] | None, tuple[float, float] | None],
) -> tuple[tuple[float, float], tuple[float, float], float] | None:
    pairs = [
        (pa, pb, _dist(pa, pb))
        for pa in a_eps
        for pb in b_eps
        if pa is not None and pb is not None
    ]
    if not pairs:
        return None
    return min(pairs, key=lambda x: x[2])


def _find_annotation_bridge(
    a: dict,
    b: dict,
    a_eps: tuple[tuple[float, float] | None, tuple[float, float] | None],
    b_eps: tuple[tuple[float, float] | None, tuple[float, float] | None],
    annotations: list[dict],
    *,
    physical_tol: float,
    gap_max: float,
    near_tol: float,
    unit_factor: float,
) -> dict | None:
    if not _same_pipe_style(a, b):
        return None

    axis_a = _line_axis(a_eps)
    axis_b = _line_axis(b_eps)
    if not axis_a or not axis_b or abs(_dot(axis_a, axis_b)) < _ANGLE_COS_TOL:
        return None

    nearest = _nearest_endpoint_pair(a_eps, b_eps)
    if not nearest:
        return None
    pa, pb, gap = nearest
    if gap <= physical_tol or gap > gap_max:
        return None

    gap_axis = _unit(_sub(pb, pa))
    if not gap_axis or abs(_dot(axis_a, gap_axis)) < _GAP_AXIS_COS_TOL:
        return None

    labels: list[str] = []
    handles: list[str] = []
    for ann in annotations:
        text = _annotation_text(ann)
        pos = _annotation_pos(ann)
        if not pos or not _is_pipe_annotation(text):
            continue
        if _point_segment_distance(pos, pa, pb) <= near_tol:
            labels.append(text)
            if ann.get("handle"):
                handles.append(str(ann["handle"]))

    if not labels:
        return None

    return {
        "from_handle": str(a.get("handle") or ""),
        "to_handle": str(b.get("handle") or ""),
        "annotation_handles": handles,
        "labels": labels,
        "gap": round(gap, 2),
        "gap_mm": round(gap * unit_factor, 2),
    }


def _find_broken_pipe_gap(
    a: dict,
    b: dict,
    a_eps: tuple[tuple[float, float] | None, tuple[float, float] | None],
    b_eps: tuple[tuple[float, float] | None, tuple[float, float] | None],
    *,
    physical_tol: float,
    gap_max: float,
    unit_factor: float,
) -> dict | None:
    """같은 스타일·같은 축의 배관 후보가 작은 간격으로 끊긴 경우를 찾는다."""
    if not _same_pipe_style(a, b):
        return None

    axis_a = _line_axis(a_eps)
    axis_b = _line_axis(b_eps)
    if not axis_a or not axis_b or abs(_dot(axis_a, axis_b)) < _ANGLE_COS_TOL:
        return None

    nearest = _nearest_endpoint_pair(a_eps, b_eps)
    if not nearest:
        return None
    pa, pb, gap = nearest
    if gap <= physical_tol or gap > gap_max:
        return None

    gap_axis = _unit(_sub(pb, pa))
    if not gap_axis or abs(_dot(axis_a, gap_axis)) < _GAP_AXIS_COS_TOL:
        return None

    return {
        "from_handle": str(a.get("handle") or ""),
        "to_handle": str(b.get("handle") or ""),
        "gap": round(gap, 2),
        "gap_mm": round(gap * unit_factor, 2),
        "from_point": {"x": round(pa[0], 3), "y": round(pa[1], 3)},
        "to_point": {"x": round(pb[0], 3), "y": round(pb[1], 3)},
    }


def _find_connection_mismatch(
    a: dict,
    b: dict,
    a_eps: tuple[tuple[float, float] | None, tuple[float, float] | None],
    b_eps: tuple[tuple[float, float] | None, tuple[float, float] | None],
    *,
    physical_tol: float,
    mismatch_max: float,
    unit_factor: float,
) -> dict | None:
    """Detect endpoint-to-segment near misses where pipes appear intended to connect."""
    if not _same_pipe_style(a, b):
        return None

    a_pts = [p for p in a_eps if p is not None]
    b_pts = [p for p in b_eps if p is not None]
    if len(a_pts) < 2 or len(b_pts) < 2:
        return None

    axis_a = _line_axis(a_eps)
    axis_b = _line_axis(b_eps)
    if not axis_a or not axis_b:
        return None
    # Parallel near misses are handled by broken_gaps. This catches T/L joints.
    if abs(_dot(axis_a, axis_b)) >= _ANGLE_COS_TOL:
        return None

    candidates: list[tuple[int, float, str, str, tuple[float, float], tuple[float, float], str]] = []

    # L-corner near miss: two perpendicular pipe endpoints should meet at the
    # elbow, but one endpoint stops a small distance before the other endpoint.
    # This is distinct from the endpoint-to-segment T/L case below because the
    # projected point lands on the segment end (t ~= 0 or 1).
    nearest = _nearest_endpoint_pair(a_eps, b_eps)
    if nearest:
        pa, pb, gap = nearest
        gap_axis = _unit(_sub(pb, pa))
        aligns_with_leg = bool(
            gap_axis
            and (
                abs(_dot(axis_a, gap_axis)) >= _GAP_AXIS_COS_TOL
                or abs(_dot(axis_b, gap_axis)) >= _GAP_AXIS_COS_TOL
            )
        )
        if physical_tol < gap <= mismatch_max and aligns_with_leg:
            candidates.append((
                0,
                gap,
                str(a.get("handle") or ""),
                str(b.get("handle") or ""),
                pa,
                pb,
                "corner_endpoint_gap",
            ))

    for endpoint in a_pts:
        other = a_pts[1] if endpoint == a_pts[0] else a_pts[0]
        extension = _endpoint_axis_extension_to_segment(
            endpoint,
            other,
            b_pts[0],
            b_pts[1],
            physical_tol=physical_tol,
            mismatch_max=mismatch_max,
        )
        d, proj, t = _point_segment_projection(endpoint, b_pts[0], b_pts[1])
        extension_added = False
        if extension and (d <= physical_tol or extension[0] <= d * 4.0):
            extension_d, point = extension
            candidates.append((
                0,
                extension_d,
                str(a.get("handle") or ""),
                str(b.get("handle") or ""),
                endpoint,
                point,
                "endpoint_axis_extension_to_segment",
            ))
            extension_added = True
        if physical_tol < d <= mismatch_max and 0.05 < t < 0.95:
            candidates.append((
                1 if extension_added else 0,
                d,
                str(a.get("handle") or ""),
                str(b.get("handle") or ""),
                endpoint,
                proj,
                "endpoint_to_segment",
            ))
    for endpoint in b_pts:
        other = b_pts[1] if endpoint == b_pts[0] else b_pts[0]
        extension = _endpoint_axis_extension_to_segment(
            endpoint,
            other,
            a_pts[0],
            a_pts[1],
            physical_tol=physical_tol,
            mismatch_max=mismatch_max,
        )
        d, proj, t = _point_segment_projection(endpoint, a_pts[0], a_pts[1])
        extension_added = False
        if extension and (d <= physical_tol or extension[0] <= d * 4.0):
            extension_d, point = extension
            candidates.append((
                0,
                extension_d,
                str(b.get("handle") or ""),
                str(a.get("handle") or ""),
                endpoint,
                point,
                "endpoint_axis_extension_to_segment",
            ))
            extension_added = True
        if physical_tol < d <= mismatch_max and 0.05 < t < 0.95:
            candidates.append((
                1 if extension_added else 0,
                d,
                str(b.get("handle") or ""),
                str(a.get("handle") or ""),
                endpoint,
                proj,
                "endpoint_to_segment",
            ))

    if not candidates:
        return None

    _rank, d, endpoint_handle, segment_handle, endpoint, proj, kind = min(candidates, key=lambda x: (x[0], x[1]))
    return {
        "endpoint_handle": endpoint_handle,
        "segment_handle": segment_handle,
        "mismatch_kind": kind,
        "offset": round(d, 2),
        "offset_mm": round(d * unit_factor, 2),
        "endpoint": {"x": round(endpoint[0], 3), "y": round(endpoint[1], 3)},
        "nearest_point": {"x": round(proj[0], 3), "y": round(proj[1], 3)},
    }


def _find_connection_overshoots(
    a: dict,
    b: dict,
    a_eps: tuple[tuple[float, float] | None, tuple[float, float] | None],
    b_eps: tuple[tuple[float, float] | None, tuple[float, float] | None],
    *,
    physical_tol: float,
    overshoot_max: float,
    main_min: float,
    unit_factor: float,
) -> list[dict]:
    """Detect short tails that pass beyond an intended L/T connection."""
    if not _same_pipe_style(a, b):
        return []

    a_pts = [p for p in a_eps if p is not None]
    b_pts = [p for p in b_eps if p is not None]
    if len(a_pts) < 2 or len(b_pts) < 2:
        return []

    axis_a = _line_axis(a_eps)
    axis_b = _line_axis(b_eps)
    if not axis_a or not axis_b:
        return []
    if abs(_dot(axis_a, axis_b)) >= _ANGLE_COS_TOL:
        return []

    candidates: list[dict] = []

    def collect(
        endpoint: tuple[float, float],
        connection_handle: str,
        overshoot_handle: str,
        seg_a: tuple[float, float],
        seg_b: tuple[float, float],
    ) -> None:
        d, touch, t = _point_segment_projection(endpoint, seg_a, seg_b)
        if d > physical_tol or not (0.0 < t < 1.0):
            return

        len_a = _dist(touch, seg_a)
        len_b = _dist(touch, seg_b)
        overshoot_len = min(len_a, len_b)
        main_len = max(len_a, len_b)
        if not (physical_tol < overshoot_len <= overshoot_max):
            return
        if main_len < main_min or main_len < overshoot_len * 2.0:
            return

        overshoot_end = seg_a if len_a <= len_b else seg_b
        candidates.append({
            "connection_handle": connection_handle,
            "overshoot_handle": overshoot_handle,
            "overshoot_kind": "endpoint_to_segment_tail",
            "overshoot": round(overshoot_len, 2),
            "overshoot_mm": round(overshoot_len * unit_factor, 2),
            "touch_point": {"x": round(touch[0], 3), "y": round(touch[1], 3)},
            "overshoot_end": {"x": round(overshoot_end[0], 3), "y": round(overshoot_end[1], 3)},
        })

    ah = str(a.get("handle") or "")
    bh = str(b.get("handle") or "")
    for endpoint in a_pts:
        collect(endpoint, ah, bh, b_pts[0], b_pts[1])
    for endpoint in b_pts:
        collect(endpoint, bh, ah, a_pts[0], a_pts[1])

    return candidates


# ── 공간 그리드 인덱스 (O(n²) → O(n·k) 최적화) ─────────────────────────────

def _build_grid(eps: dict[str, tuple], tolerance: float = _CONN_TOL) -> dict[tuple[int, int], list[str]]:
    cell = max(tolerance * 2, 1e-9)
    grid: dict[tuple[int, int], list[str]] = defaultdict(list)
    for h, (s, e) in eps.items():
        for pt in (s, e):
            if pt:
                grid[(int(pt[0] // cell), int(pt[1] // cell))].append(h)
    return dict(grid)


def _grid_candidates(h: str, eps: dict, grid: dict, tolerance: float = _CONN_TOL) -> set[str]:
    cell = max(tolerance * 2, 1e-9)
    result: set[str] = set()
    for pt in eps[h]:
        if pt:
            cx, cy = int(pt[0] // cell), int(pt[1] // cell)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    result.update(grid.get((cx + dx, cy + dy), []))
    result.discard(h)
    return result


# ── Union-Find ────────────────────────────────────────────────────────────────

class _UF:
    def __init__(self, items):
        self.p = {x: x for x in items}

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, x, y):
        self.p[self.find(x)] = self.find(y)

    def groups(self) -> dict[str, list[str]]:
        g: dict[str, list[str]] = defaultdict(list)
        for x in self.p:
            g[self.find(x)].append(x)
        return dict(g)


# ── 메인 클래스 ───────────────────────────────────────────────────────────────

class PipeTopologyBuilder:
    """
    LINE 끝점 근접(tolerance mm)으로 배관 경로(pipe_run)를 구성합니다.
    전기 에이전트 TopologyBuilder의 배관 버전.
    """

    def __init__(self, tolerance: float = _CONN_TOL):
        self.tol = tolerance

    def build(self, elements: list[dict], unit_factor: float = 1.0) -> dict[str, Any]:
        """
        Args:
            unit_factor: drawing_unit -> mm 변환 계수 (workflow_handler에서 주입)
                         total_length_mm = total_length * unit_factor
        """
        uf = max(unit_factor, 1e-9)
        tol = self.tol / uf
        raw_lines = [
            e for e in elements
            if str(e.get("raw_type") or "").upper() in _LINE_RAW
            and e.get("handle")
            and not e.get("exclude_from_pipe_topology")
            and str(e.get("connectivity_role") or "").lower() != "symbol"
        ]
        blocks = [e for e in elements if str(e.get("raw_type") or "").upper() in _BLOCK_RAW and e.get("handle")]
        annotations = [
            e for e in elements
            if str(e.get("raw_type") or "").upper() in _TEXT_RAW
            and _is_pipe_annotation(_annotation_text(e))
            and _annotation_pos(e) is not None
        ]
        title_extents = [
            ext
            for e in elements
            if str(e.get("raw_type") or "").upper() in _TEXT_RAW and _is_title_text(e)
            for ext in [_bbox_extents(e)]
            if ext is not None
        ]
        raw_eps: dict[str, tuple] = {}
        duplicate_handles: list[str] = []
        for e in raw_lines:
            h = e.get("handle")
            if not h:
                continue
            if h in raw_eps:
                duplicate_handles.append(str(h))
                continue
            raw_eps[h] = _endpoints(e)
        if duplicate_handles:
            _log.warning(
                "[PipeTopology] LINE handle 중복 감지 — 첫 등장만 사용 (sample=%s, total=%d)",
                duplicate_handles[:5],
                len(duplicate_handles),
            )
        layer_line_counts: dict[str, int] = defaultdict(int)
        for line in raw_lines:
            layer_line_counts[str(line.get("layer") or "")] += 1
        layer_color_hints = _build_layer_pipe_color_hints(
            raw_lines,
            raw_eps,
            annotations,
            unit_factor,
            dict(layer_line_counts),
        )
        filter_reasons: dict[str, int] = defaultdict(int)
        accepted_reasons: dict[str, int] = defaultdict(int)
        lines: list[dict] = []
        for e in raw_lines:
            ok, reason = _is_pipe_run_line(
                e,
                raw_eps[e["handle"]],
                unit_factor=unit_factor,
                annotations=annotations,
                title_extents=title_extents,
                layer_color_hints=layer_color_hints,
                layer_line_counts=dict(layer_line_counts),
            )
            if ok:
                lines.append(e)
                accepted_reasons[reason] += 1
            else:
                filter_reasons[reason] += 1

        if not lines:
            return {
                "pipe_runs": [],
                "equipment_graph": {},
                "virtual_connections": [],
                "cross_run_virtual_connections": [],
                "broken_gaps": [],
                "connection_mismatches": [],
                "connection_overshoots": [],
                "summary": {
                    "run_count": 0,
                    "total_lines": 0,
                    "raw_lines": len(raw_lines),
                    "filtered_lines": len(raw_lines),
                    "line_accept_reasons": dict(accepted_reasons),
                    "line_filter_reasons": dict(filter_reasons),
                    "unconnected_lines": 0,
                    "block_count": len(blocks),
                },
            }

        if len(lines) > _MAX_LINES:
            _log.warning("[PipeTopology] 대형 도면 %d lines > %d 상한 → 첫 %d개만 처리", len(lines), _MAX_LINES, _MAX_LINES)
            lines = lines[:_MAX_LINES]

        hs   = [e["handle"] for e in lines]
        eps  = {e["handle"]: _endpoints(e) for e in lines}
        line_map = {e["handle"]: e for e in lines}
        eps_bboxes = {h: _bbox_from_eps(eps[h]) for h in hs}
        grid = _build_grid(eps, tol)
        adj: dict[str, set[str]] = {h: set() for h in hs}

        # 끝점 근접 LINE-LINE 연결 (그리드 최적화)
        for ha in hs:
            sa, ea = eps[ha]
            pts_a = [p for p in (sa, ea) if p]
            if not pts_a:
                continue
            for hb in _grid_candidates(ha, eps, grid, tol):
                if hb <= ha:          # 중복 방지
                    continue
                if _has_physical_pipe_touch(line_map[ha], line_map[hb], eps[ha], eps[hb], tol):
                    adj[ha].add(hb)
                    adj[hb].add(ha)

        # Endpoint-to-segment T-junctions are missed by endpoint-grid lookup
        # when the touched pipe segment is long and its endpoints are far away.
        for idx, ha in enumerate(hs):
            for hb in hs[idx + 1:]:
                if hb in adj[ha]:
                    continue
                if not _bbox_near(eps_bboxes[ha], eps_bboxes[hb], tol):
                    continue
                if _has_physical_pipe_touch(line_map[ha], line_map[hb], eps[ha], eps[hb], tol):
                    adj[ha].add(hb)
                    adj[hb].add(ha)

        # TEXT/MTEXT labels such as "G" or "20A" often sit inside a plotted
        # pipe gap. Record that context without joining the graph; drawing QA
        # suppresses the same pair because the interruption is intentional
        # drafting notation, not a missing physical connection.
        virtual_connections: list[dict] = []
        if annotations:
            gap_max = max(tol * 3.0, _ANNOTATION_GAP_MAX_MM / uf)
            near_tol = max(tol * 1.5, _ANNOTATION_NEAR_TOL_MM / uf)
            for idx, ha in enumerate(hs):
                for hb in hs[idx + 1:]:
                    if hb in adj[ha]:
                        continue
                    bridge = _find_annotation_bridge(
                        line_map[ha],
                        line_map[hb],
                        eps[ha],
                        eps[hb],
                        annotations,
                        physical_tol=tol,
                        gap_max=gap_max,
                        near_tol=near_tol,
                        unit_factor=unit_factor,
                    )
                    if bridge:
                        virtual_connections.append(bridge)
        virtual_pair_keys = {
            frozenset((str(vc.get("from_handle") or ""), str(vc.get("to_handle") or "")))
            for vc in virtual_connections
            if vc.get("from_handle") and vc.get("to_handle")
        }

        # If no pipe annotation bridges the gap, keep the same-style collinear
        # gap as a deterministic continuity candidate.
        broken_gaps: list[dict] = []
        broken_gap_max = max(tol * 3.0, _BROKEN_GAP_MAX_MM / uf)
        for idx, ha in enumerate(hs):
            for hb in hs[idx + 1:]:
                if hb in adj[ha]:
                    continue
                if frozenset((ha, hb)) in virtual_pair_keys:
                    continue
                gap = _find_broken_pipe_gap(
                    line_map[ha],
                    line_map[hb],
                    eps[ha],
                    eps[hb],
                    physical_tol=tol,
                    gap_max=broken_gap_max,
                    unit_factor=unit_factor,
                )
                if gap:
                    broken_gaps.append(gap)

        connection_mismatches: list[dict] = []
        mismatch_max = max(tol * 2.5, 200.0 / uf)
        for idx, ha in enumerate(hs):
            for hb in hs[idx + 1:]:
                if hb in adj[ha]:
                    continue
                if not _bbox_near(eps_bboxes[ha], eps_bboxes[hb], mismatch_max):
                    continue
                mismatch = _find_connection_mismatch(
                    line_map[ha],
                    line_map[hb],
                    eps[ha],
                    eps[hb],
                    physical_tol=tol,
                    mismatch_max=mismatch_max,
                    unit_factor=unit_factor,
                )
                if mismatch:
                    connection_mismatches.append(mismatch)

        connection_overshoots: list[dict] = []
        seen_overshoots: set[tuple[str, str, float, float]] = set()
        overshoot_max = max(tol * 3.0, _CONNECTION_OVERSHOOT_MAX_MM / uf)
        overshoot_main_min = _CONNECTION_OVERSHOOT_MAIN_MIN_MM / uf
        for idx, ha in enumerate(hs):
            for hb in hs[idx + 1:]:
                if not _bbox_near(eps_bboxes[ha], eps_bboxes[hb], overshoot_max):
                    continue
                for overshoot in _find_connection_overshoots(
                    line_map[ha],
                    line_map[hb],
                    eps[ha],
                    eps[hb],
                    physical_tol=tol,
                    overshoot_max=overshoot_max,
                    main_min=overshoot_main_min,
                    unit_factor=unit_factor,
                ):
                    end = overshoot.get("overshoot_end") or {}
                    key = (
                        str(overshoot.get("connection_handle") or ""),
                        str(overshoot.get("overshoot_handle") or ""),
                        float(end.get("x") or 0.0),
                        float(end.get("y") or 0.0),
                    )
                    if key in seen_overshoots:
                        continue
                    seen_overshoots.add(key)
                    connection_overshoots.append(overshoot)

        # 연결 성분 → pipe_run
        uf = _UF(hs)
        for h, nbs in adj.items():
            for nb in nbs:
                uf.union(h, nb)

        el_map = {e["handle"]: e for e in elements if e.get("handle")}
        pipe_runs: list[dict] = []
        line_to_run: dict[str, int] = {}
        groups_list = list(uf.groups().values())
        for run_id, members in enumerate(groups_list):
            for h in members:
                line_to_run[h] = run_id

        # virtual_connections 분류:
        #   - intra_run_virtuals : 양쪽 handle 이 같은 run 안에 있어 run.virtual_connections 로 들어감
        #   - cross_run_virtuals : 서로 다른 run 의 gap 을 잇는 주석 컨텍스트 (run 외부에 별도 보관)
        cross_run_virtuals: list[dict] = []
        intra_run_groups: dict[frozenset[str], list[dict]] = defaultdict(list)
        for vc in virtual_connections:
            fh = vc.get("from_handle")
            th = vc.get("to_handle")
            if not fh or not th:
                continue
            r_from = line_to_run.get(fh)
            r_to = line_to_run.get(th)
            if r_from is None or r_to is None:
                continue
            if r_from == r_to:
                intra_run_groups[frozenset((fh, th))].append(vc)
            else:
                vc_with_runs = dict(vc)
                vc_with_runs["from_run_id"] = r_from
                vc_with_runs["to_run_id"] = r_to
                cross_run_virtuals.append(vc_with_runs)

        for run_id, members in enumerate(groups_list):
            member_set = set(members)
            run_virtuals = [
                vc for vc in virtual_connections
                if vc.get("from_handle") in member_set and vc.get("to_handle") in member_set
            ]
            run_labels = [
                label
                for vc in run_virtuals
                for label in (vc.get("labels") or [])
            ]
            total_len = sum(
                _dist(*eps[h]) if all(eps[h]) else 0.0
                for h in members
            ) + sum(float(vc.get("gap") or 0.0) for vc in run_virtuals)
            mat = next(
                (el_map[h].get("material") for h in members
                 if el_map.get(h, {}).get("material") not in (None, "UNKNOWN")),
                _annotation_material(run_labels) or "UNKNOWN",
            )
            dia = next(
                (el_map[h].get("diameter_mm", 0) for h in members
                 if el_map.get(h, {}).get("diameter_mm", 0) > 0),
                _annotation_diameter(run_labels) or 0.0,
            )
            pipe_runs.append({
                "run_id":           run_id,
                "handles":          members,
                "total_length":     round(total_len, 2),
                "total_length_mm":  round(total_len * unit_factor, 2),  # mm 정규화
                "connected_blocks": [],
                "material":         mat,
                "diameter_mm":      dia,
                "virtual_connections": run_virtuals,
            })

        # 블록-run 연결: 블록 insert_point와 LINE 끝점 근접
        blk_tol = _BLOCK_CONN_TOL / max(unit_factor, 1e-9)
        for b in blocks:
            bh = b["handle"]
            bp = _pt(b, "position", "insert_point")
            if not bp:
                continue
            for lh in hs:
                sa, ea = eps[lh]
                if any(p and _dist(bp, p) <= blk_tol for p in (sa, ea)):
                    rid = line_to_run.get(lh)
                    if rid is not None and bh not in pipe_runs[rid]["connected_blocks"]:
                        pipe_runs[rid]["connected_blocks"].append(bh)

        # 블록 간 연결 그래프 (같은 run에 연결된 블록끼리)
        eq_graph: dict[str, list[str]] = {b["handle"]: [] for b in blocks}
        for run in pipe_runs:
            cb = run.get("connected_blocks") or []
            for i, bh in enumerate(cb):
                for obh in cb[i + 1:]:
                    if obh not in eq_graph.get(bh, []):
                        eq_graph.setdefault(bh, []).append(obh)
                    if bh not in eq_graph.get(obh, []):
                        eq_graph.setdefault(obh, []).append(bh)

        unconn = sum(1 for h in hs if not adj[h])
        _log.info(
            "[PipeTopology] runs=%d lines=%d(raw=%d) unconnected=%d blocks=%d virtual=%d broken_gaps=%d mismatches=%d overshoots=%d accepted=%s filtered=%s",
            len(pipe_runs), len(lines), len(raw_lines), unconn, len(blocks), len(virtual_connections), len(broken_gaps),
            len(connection_mismatches), len(connection_overshoots), dict(accepted_reasons), dict(filter_reasons),
        )
        return {
            "pipe_runs":       pipe_runs,
            "equipment_graph": eq_graph,
            "virtual_connections": virtual_connections,
            "cross_run_virtual_connections": cross_run_virtuals,
            "broken_gaps": broken_gaps,
            "connection_mismatches": connection_mismatches,
            "connection_overshoots": connection_overshoots,
            "summary": {
                "run_count":         len(pipe_runs),
                "total_lines":       len(lines),
                "raw_lines":         len(raw_lines),
                "filtered_lines":    len(raw_lines) - len(lines),
                "line_accept_reasons": dict(accepted_reasons),
                "line_filter_reasons": dict(filter_reasons),
                "pipe_color_hints":  {k: sorted(v) for k, v in layer_color_hints.items()},
                "unconnected_lines": unconn,
                "block_count":       len(blocks),
                "unit_factor":       unit_factor,
                "virtual_connections": len(virtual_connections),
                "broken_gaps":       len(broken_gaps),
                "connection_mismatches": len(connection_mismatches),
                "connection_overshoots": len(connection_overshoots),
            },
        }
