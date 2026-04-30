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

_CONN_TOL = 50.0          # 끝점 허용 오차 (mm 단위 도면 좌표)
_BLOCK_CONN_TOL_MUL = 3   # 블록-끝점 허용 오차 배율 (파이프보다 여유 있게)
_MAX_LINES = 2000         # 초과 시 경고 후 첫 N개만 처리

_LINE_RAW  = frozenset({"LINE", "ARC", "POLYLINE", "LWPOLYLINE", "SPLINE"})
_BLOCK_RAW = frozenset({"INSERT", "BLOCK"})
_TEXT_RAW  = frozenset({"TEXT", "MTEXT", "MLEADER"})

_ANNOTATION_GAP_MAX_MM = 800.0
_ANNOTATION_NEAR_TOL_MM = 120.0
_ANGLE_COS_TOL = math.cos(math.radians(8.0))
_GAP_AXIS_COS_TOL = math.cos(math.radians(12.0))
_PIPE_ANNOTATION_RE = re.compile(
    r"^(?:G|GAS|LPG|LNG|DN\s*\d+(?:\.\d+)?|\d+(?:\.\d+)?\s*A?)$",
    re.IGNORECASE,
)
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
_TITLE_GRAPHIC_NEAR_TOL_MM = 120.0
_SYMBOL_LINE_MAX_MM = 80.0
_WEAK_UNKNOWN_LINE_MIN_MM = 300.0
_ANNOTATION_LINE_NEAR_MM = 160.0


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


def _unit(v: tuple[float, float]) -> tuple[float, float] | None:
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
    return _pt(e, "position", "insert_point", "center") or _bbox_center(e)


def _annotation_text(e: dict) -> str:
    return str(e.get("text") or e.get("content") or "").strip()


def _is_title_text(e: dict) -> bool:
    return bool(_TITLE_TEXT_RE.search(_annotation_text(e)))


def _normal_text(text: str) -> str:
    return re.sub(r"\s+", "", text).upper()


def _is_pipe_annotation(text: str) -> bool:
    if not text:
        return False
    return bool(_PIPE_ANNOTATION_RE.match(_normal_text(text)))


def _annotation_material(texts: list[str]) -> str | None:
    for text in texts:
        if _normal_text(text).startswith(("G", "LPG", "LNG")):
            return "GAS"
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
) -> tuple[bool, str]:
    layer = str(e.get("layer") or "")
    role = str(e.get("layer_role") or "").lower()
    length_mm = _line_length(eps) * unit_factor
    explicit = _has_explicit_pipe_attrs(e)

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

    promoted = bool(e.get("flag_for_piping_agent"))
    pipe_layer = bool(_PIPE_LAYER_RE.search(layer)) or role == "mep" or promoted
    color = _color_key(e.get("color"))
    hinted_colors = layer_color_hints.get(layer) or set()
    near_annotation = _line_near_pipe_annotation(
        e,
        eps,
        annotations,
        _ANNOTATION_LINE_NEAR_MM / max(unit_factor, 1e-9),
    )

    color_mismatch = bool(hinted_colors and color and color not in hinted_colors)
    has_pipe_evidence = pipe_layer or explicit or near_annotation

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

    return True, "pipe_candidate_promoted" if promoted else "pipe_candidate"


def _same_pipe_style(a: dict, b: dict) -> bool:
    a_layer = str(a.get("layer") or "")
    b_layer = str(b.get("layer") or "")
    if a_layer and b_layer and a_layer != b_layer:
        return False

    # Color is only a weak hint. CAD standards vary by project and plotting
    # style, so do not split a pipe run solely because ACI/RGB color differs.
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
    return (
        any(_point_segment_distance(pa, b_pts[0], b_pts[1]) <= tol for pa in a_pts)
        or any(_point_segment_distance(pb, a_pts[0], a_pts[1]) <= tol for pb in b_pts)
    )


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

    candidates: list[tuple[float, str, str, tuple[float, float], tuple[float, float]]] = []
    for endpoint in a_pts:
        d, proj, t = _point_segment_projection(endpoint, b_pts[0], b_pts[1])
        if physical_tol < d <= mismatch_max and 0.05 < t < 0.95:
            candidates.append((d, str(a.get("handle") or ""), str(b.get("handle") or ""), endpoint, proj))
    for endpoint in b_pts:
        d, proj, t = _point_segment_projection(endpoint, a_pts[0], a_pts[1])
        if physical_tol < d <= mismatch_max and 0.05 < t < 0.95:
            candidates.append((d, str(b.get("handle") or ""), str(a.get("handle") or ""), endpoint, proj))

    if not candidates:
        return None

    d, endpoint_handle, segment_handle, endpoint, proj = min(candidates, key=lambda x: x[0])
    return {
        "endpoint_handle": endpoint_handle,
        "segment_handle": segment_handle,
        "offset": round(d, 2),
        "offset_mm": round(d * unit_factor, 2),
        "endpoint": {"x": round(endpoint[0], 3), "y": round(endpoint[1], 3)},
        "nearest_point": {"x": round(proj[0], 3), "y": round(proj[1], 3)},
    }


# ── 공간 그리드 인덱스 (O(n²) → O(n·k) 최적화) ─────────────────────────────

def _build_grid(eps: dict[str, tuple]) -> dict[tuple[int, int], list[str]]:
    cell = _CONN_TOL * 2
    grid: dict[tuple[int, int], list[str]] = defaultdict(list)
    for h, (s, e) in eps.items():
        for pt in (s, e):
            if pt:
                grid[(int(pt[0] // cell), int(pt[1] // cell))].append(h)
    return dict(grid)


def _grid_candidates(h: str, eps: dict, grid: dict) -> set[str]:
    cell = _CONN_TOL * 2
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
        raw_lines = [e for e in elements if str(e.get("raw_type") or "").upper() in _LINE_RAW  and e.get("handle")]
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
        raw_eps = {e["handle"]: _endpoints(e) for e in raw_lines}
        layer_color_hints = _build_layer_pipe_color_hints(
            raw_lines, raw_eps, annotations, unit_factor
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
                "broken_gaps": [],
                "connection_mismatches": [],
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
        grid = _build_grid(eps)
        adj: dict[str, set[str]] = {h: set() for h in hs}

        # 끝점 근접 LINE-LINE 연결 (그리드 최적화)
        for ha in hs:
            sa, ea = eps[ha]
            pts_a = [p for p in (sa, ea) if p]
            if not pts_a:
                continue
            for hb in _grid_candidates(ha, eps, grid):
                if hb <= ha:          # 중복 방지
                    continue
                if _has_physical_pipe_touch(line_map[ha], line_map[hb], eps[ha], eps[hb], self.tol):
                    adj[ha].add(hb)
                    adj[hb].add(ha)

        # Endpoint-to-segment T-junctions are missed by endpoint-grid lookup
        # when the touched pipe segment is long and its endpoints are far away.
        for idx, ha in enumerate(hs):
            for hb in hs[idx + 1:]:
                if hb in adj[ha]:
                    continue
                if not _bbox_near(eps_bboxes[ha], eps_bboxes[hb], self.tol):
                    continue
                if _has_physical_pipe_touch(line_map[ha], line_map[hb], eps[ha], eps[hb], self.tol):
                    adj[ha].add(hb)
                    adj[hb].add(ha)

        # TEXT/MTEXT labels such as "G" or "20A" often intentionally break a
        # plotted pipe line. Treat a small collinear gap with a pipe annotation
        # in the gap as a virtual connection, instead of reporting a broken run.
        virtual_connections: list[dict] = []
        if annotations:
            gap_max = max(self.tol * 3.0, _ANNOTATION_GAP_MAX_MM / max(unit_factor, 1e-9))
            near_tol = max(self.tol * 1.5, _ANNOTATION_NEAR_TOL_MM / max(unit_factor, 1e-9))
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
                        physical_tol=self.tol,
                        gap_max=gap_max,
                        near_tol=near_tol,
                        unit_factor=unit_factor,
                    )
                    if bridge:
                        adj[ha].add(hb)
                        adj[hb].add(ha)
                        virtual_connections.append(bridge)

        # If no pipe annotation bridges the gap, keep the same-style collinear
        # gap as a deterministic continuity candidate.
        broken_gaps: list[dict] = []
        broken_gap_max = max(self.tol * 3.0, _ANNOTATION_GAP_MAX_MM / max(unit_factor, 1e-9))
        for idx, ha in enumerate(hs):
            for hb in hs[idx + 1:]:
                if hb in adj[ha]:
                    continue
                gap = _find_broken_pipe_gap(
                    line_map[ha],
                    line_map[hb],
                    eps[ha],
                    eps[hb],
                    physical_tol=self.tol,
                    gap_max=broken_gap_max,
                    unit_factor=unit_factor,
                )
                if gap:
                    broken_gaps.append(gap)

        connection_mismatches: list[dict] = []
        mismatch_max = max(self.tol * 2.5, 200.0 / max(unit_factor, 1e-9))
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
                    physical_tol=self.tol,
                    mismatch_max=mismatch_max,
                    unit_factor=unit_factor,
                )
                if mismatch:
                    connection_mismatches.append(mismatch)

        # 연결 성분 → pipe_run
        uf = _UF(hs)
        for h, nbs in adj.items():
            for nb in nbs:
                uf.union(h, nb)

        el_map = {e["handle"]: e for e in elements if e.get("handle")}
        pipe_runs: list[dict] = []
        line_to_run: dict[str, int] = {}

        for run_id, members in enumerate(uf.groups().values()):
            for h in members:
                line_to_run[h] = run_id
            member_set = set(members)
            run_virtuals = [
                vc for vc in virtual_connections
                if vc["from_handle"] in member_set and vc["to_handle"] in member_set
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
        blk_tol = self.tol * _BLOCK_CONN_TOL_MUL
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
            cb = run["connected_blocks"]
            for i, bh in enumerate(cb):
                for obh in cb[i + 1:]:
                    if obh not in eq_graph.get(bh, []):
                        eq_graph.setdefault(bh, []).append(obh)
                    if bh not in eq_graph.get(obh, []):
                        eq_graph.setdefault(obh, []).append(bh)

        unconn = sum(1 for h in hs if not adj[h])
        _log.info(
            "[PipeTopology] runs=%d lines=%d(raw=%d) unconnected=%d blocks=%d virtual=%d broken_gaps=%d mismatches=%d accepted=%s filtered=%s",
            len(pipe_runs), len(lines), len(raw_lines), unconn, len(blocks), len(virtual_connections), len(broken_gaps),
            len(connection_mismatches), dict(accepted_reasons), dict(filter_reasons),
        )
        return {
            "pipe_runs":       pipe_runs,
            "equipment_graph": eq_graph,
            "virtual_connections": virtual_connections,
            "broken_gaps": broken_gaps,
            "connection_mismatches": connection_mismatches,
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
            },
        }
