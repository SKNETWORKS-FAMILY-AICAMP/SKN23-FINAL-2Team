"""
File    : backend/services/agents/elec/sub/topology.py
Author  : 김지우
Create  : 2026-04-24
Modified: 2026-05-04
  - [고도화] 공간 그리드 인덱스 도입
  - [고도화] T-접합(T-junction) 감지
  - [고도화] Annotation Bridging
  - [고도화] unit_factor 파라미터 추가
  - [신규] DEVICE(조명/스위치/콘센트) -> circuit_run 연결 로직 추가
Description : CAD LINE/WIRE 끝점 근접으로 전기 회로 경로(circuit_run)를 구성하고
              연결된 분전반·기기를 식별합니다.

출력 스키마:
  circuit_runs: [
    {
      run_id          : int,
      handles         : [str, ...],
      total_length_mm : float,              # mm 환산 총 길이
      voltage         : float,
      cable_sqmm       : float,
      connected_panels : [str, ...]
      connected_devices: [{handle, category, subtype}, ...]   # 조명/스위치/콘센트 등
    }, ...
  ]
  panel_graph        : {panel_handle: [handle, ...]}
  broken_segments    : [{handle_a, handle_b, gap_mm, midpoint}, ...]
  virtual_connections: [{from_handle, to_handle, labels, gap_mm}, ...]
  summary            : {run_count, total_lines, unconnected_wires, ...}
"""
from __future__ import annotations

import logging
import math
import re
from collections import defaultdict
from typing import Any

_log = logging.getLogger(__name__)

# 상수
_CONN_TOL             = 50.0    # 끝점 연결 허용 오차 (도면 단위, mm 기준)
_BREAK_DETECT_MAX_MM  = 200.0   # 이 거리 이내 미연결 쌍은 단선 후보 (mm)
_ISOLATED_DEVICE_MAX_GAP_MM = 1500.0
_BLOCK_CONN_TOL_MUL   = 1.5
_MAX_LINES            = 5000
_MIN_WIRE_LENGTH_MM   = 300.0
_GROUNDING_MIN_WIRE_LENGTH_MM = 30.0
_NOISE_LINE_MAX_MM    = 100.0   # 이 미만 고립 선은 노이즈로 제거
_ANNOTATION_GAP_MAX_MM= 500.0   # 주석 교량 최대 선간 거리 (mm)
_ANNOTATION_NEAR_MM   = 100.0   # 주석이 선과 이 거리 이내면 교량 후보
_GROUNDING_ANNOTATION_NEAR_MM = 250.0
_T_PARAM_TOL          = 0.05    # T-접합: 선분의 5%~95% 위치만 인정
_LINE_RAW   = frozenset({"LINE", "ARC", "POLYLINE", "LWPOLYLINE", "SPLINE"})
_BLOCK_RAW  = frozenset({"INSERT", "BLOCK"})
_TEXT_RAW   = frozenset({"TEXT", "MTEXT"})
_CIRCLE_RAW = frozenset({"CIRCLE"})
_PANEL_PREFIXES = ("PNL", "MCC", "CB", "MCB", "MCCB", "TR")

_EXCLUDED_WIRE_LAYER_RE = re.compile(
    r"(?:^|[-_ ])(?:DIM|DIMS|DIMENSION|CENTER|CEN|HATCH|DETAIL|MECH|FORMAT|TITLE|TEXT|ANNO|DEFPOINTS|BORDER|FRAME|TABLE|GRID|LID)(?:$|[-_ ])",
    re.IGNORECASE,
)
# 다형(점선/파선) 선종: 이 선종끼리 gap은 의도적 패턴이며 단선이 아님
_DASHED_LINETYPE_RE = re.compile(
    r"^(?:G[0-9]+|F[0-9]+|HID[0-9]*|DASHED|HIDDEN|DOTTED|PHANTOM|DIVIDE|ZIGZAG|CENTER2?|DOT|지중|매설|숨은|은선)",
    re.IGNORECASE,
)
_WIRE_LAYER_RE = re.compile(
    r"(?:^|[-_ ])(?:E\d+|W\d+|E|W|EL|ELEC|WIRE|CABLE|LIGHT|LIGHTING|POWER|SOCKET|OUTLET|CONDUIT|CIRCUIT|GROUND|GND|GRD|TRAY|DUCT)(?:$|[-_ ])"
    r"|전등|조명|전기|배선|콘센트|회로|전원",
    re.IGNORECASE,
)
_WIRE_TEXT_RE = re.compile(
    r"\b(?:FGV|CV|HFIX|HIV|IV|SQ|MM2|AWG|PE|N|L\d+|E[12]|[34]P|MCCB|MCB|ELB|CB|CABLE|TRAY|GROUND|GND|GRD|22\.?9KV|A)\b|\uc811\uc9c0|㎟|mm²",
    re.IGNORECASE,
)
_WIRE_SIZE_TEXT_RE = re.compile(
    r"(?:FGV|CV|HFIX|HIV|IV)?\s*(\d+(?:\.\d+)?)\s*(?:SQ|㎟|mm2|mm²|mm\s*2)",
    re.IGNORECASE,
)
_GROUNDING_INTENT_RE = re.compile(
    r"GROUND|GND|GRD|EARTH|접지|접지봉|접지선|접지도체|접지저항|외함\s*접지|피뢰|L\.?\s*A|E1|E2|1종|2종|3종|특별\s*3종",
    re.IGNORECASE,
)

# 디바이스로 인정하는 카테고리 집합
# elec_attr_extractor 또는 mapping_agent가 block에 부여하는 category 값
_DEVICE_CATEGORIES: frozenset[str] = frozenset({
    "LIGHT",        # 조명 기구
    "EXIT_LIGHT",   # 비상유도등
    "EMERGENCY",    # 비상등
    "SWITCH",       # 스위치
    "SOCKET",       # 콘센트
    "OUTLET",       # 아울렛 (SOCKET 동의어)
    "FAN",          # 환풍기
    "MOTOR",        # 소형 모터
    "SENSOR",       # 감지기(연기·열)
    "HEATER",       # 전기 히터
})

# 블록명/레이어명으로 디바이스를 판별하는 패턴 (category 필드가 없는 경우 폴백)
# 한국어 블록명("콘센트", "조명", "스위치" 등) 포함
_DEVICE_NAME_RE = re.compile(
    r"(LIGHT|LT|LAMP|EXIT|EM|SW|SWITCH|SOCKET|OUTLET|PLUG|FAN|MOTOR|SENSOR|HEATER"
    r"|콘센트"          # 콘센트
    r"|조명"                # 조명
    r"|전등"                # 전등
    r"|비상등"          # 비상등
    r"|스위치"          # 스위치
    r"|환풍기"          # 환풍기
    r"|방향지시기" # 방향지시기(비상유도등)
    r"|감지기"          # 감지기
    r"|E-OUTLET|E-SOCKET|E-LIGHT|E-SWITCH)",
    re.IGNORECASE,
)

# 전기 회로 주석 패턴: L1, L2, N, PE, CB-1 등
_ELEC_ANN_RE = re.compile(
    r"^(?:L\d+|N|PE|GND|GR|E|CB[-_]?\d+|MCB[-_]?\d+|MCCB[-_]?\d+|[A-Z]{1,3}\d+)$",
    re.IGNORECASE,
)


# 기하 헬퍼
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


def _sub(a, b):
    return a[0] - b[0], a[1] - b[1]


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1]


def _endpoints(e: dict) -> tuple[tuple | None, tuple | None]:
    rt = str(e.get("raw_type") or "").upper()
    if rt in ("POLYLINE", "LWPOLYLINE"):
        vts = e.get("vertices") or e.get("points") or []
        if len(vts) >= 2:
            try:
                return (float(vts[0]["x"]), float(vts[0]["y"])), \
                       (float(vts[-1]["x"]), float(vts[-1]["y"]))
            except (KeyError, TypeError, ValueError):
                pass
    s = _pt(e, "start_point", "start", "position", "center")
    t = _pt(e, "end_point", "end")
    return s, t


def _point_segment(p, a, b):
    """점 p에서 선분 ab까지의 최단거리와 매개변수 t를 반환한다."""
    ab = _sub(b, a)
    ab2 = _dot(ab, ab)
    if ab2 <= 1e-9:
        return _dist(p, a), 0.0
    t = max(0.0, min(1.0, _dot(_sub(p, a), ab) / ab2))
    proj = (a[0] + ab[0] * t, a[1] + ab[1] * t)
    return _dist(p, proj), t


# 디바이스 판별 헬퍼
def _route_signature(e: dict) -> tuple[str, str]:
    return (
        str(e.get("layer") or "").strip().upper(),
        str(e.get("linetype") or "").strip().upper(),
    )


def _segments_nearly_collinear(a: dict, b: dict, unit_factor: float) -> bool:
    sa, ta = _endpoints(a)
    sb, tb = _endpoints(b)
    if not (sa and ta and sb and tb):
        return False
    av = (ta[0] - sa[0], ta[1] - sa[1])
    bv = (tb[0] - sb[0], tb[1] - sb[1])
    alen = math.hypot(av[0], av[1])
    blen = math.hypot(bv[0], bv[1])
    if alen <= 1e-9 or blen <= 1e-9:
        return False
    direction_cross = abs(av[0] * bv[1] - av[1] * bv[0]) / (alen * blen)
    if direction_cross > 0.08:
        return False
    line_tol = max(5.0 / max(unit_factor, 1e-9), min(alen, blen) * 0.08, 1.0)
    distances = [
        _point_line_distance(sb, sa, ta),
        _point_line_distance(tb, sa, ta),
        _point_line_distance(sa, sb, tb),
        _point_line_distance(ta, sb, tb),
    ]
    return min(distances) <= line_tol


def _point_line_distance(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    v = (b[0] - a[0], b[1] - a[1])
    length = math.hypot(v[0], v[1])
    if length <= 1e-9:
        return _dist(p, a)
    return abs(v[0] * (a[1] - p[1]) - (a[0] - p[0]) * v[1]) / length


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


def _arc_can_bridge_route(arc: dict, line_a: dict, line_b: dict) -> bool:
    arc_layer, arc_linetype = _route_signature(arc)
    a_layer, a_linetype = _route_signature(line_a)
    b_layer, b_linetype = _route_signature(line_b)
    default_types = {"", "CONTINUOUS", "BYLAYER", "BYBLOCK"}
    if arc_linetype not in default_types and arc_linetype in {a_linetype, b_linetype}:
        return True
    return bool(arc_layer and arc_layer in {a_layer, b_layer})


def _arc_bridges_wire_endpoints(
    raw_lines: list[dict],
    line_a: dict | None,
    line_b: dict | None,
    point_a: tuple[float, float],
    point_b: tuple[float, float],
    unit_factor: float,
) -> bool:
    if not line_a or not line_b:
        return False
    gap = _dist(point_a, point_b)
    margin = max(10.0 / max(unit_factor, 1e-9), gap * 0.1)
    for arc in raw_lines:
        if _etype(arc) != "ARC":
            continue
        if not _arc_can_bridge_route(arc, line_a, line_b):
            continue
        bbox = arc.get("bbox")
        if not isinstance(bbox, dict):
            continue
        if _point_in_unordered_bbox(point_a, bbox, margin) and _point_in_unordered_bbox(point_b, bbox, margin):
            return True
    return False


def _arc_bridge_candidates(
    arc: dict,
    lines: list[dict],
    eps_list: list[tuple],
    unit_factor: float,
    tol: float,
) -> list[tuple[int, str, tuple[float, float]]]:
    bbox = arc.get("bbox")
    if not isinstance(bbox, dict):
        return []
    margin = max(tol, 10.0 / max(unit_factor, 1e-9))
    candidates: list[tuple[int, str, tuple[float, float]]] = []
    for idx, (start, end) in enumerate(eps_list):
        for side, point in (("start", start), ("end", end)):
            if point and _point_in_unordered_bbox(point, bbox, margin):
                candidates.append((idx, side, point))
    return candidates


def _is_short_route_gap_fragment(
    handle: str,
    line: dict,
    lines: list[dict],
    eps_list: list[tuple[tuple | None, tuple | None]],
    handle_to_idx: dict[str, int],
    *,
    conn_tol: float,
    break_max_raw: float,
    unit_factor: float,
) -> bool:
    idx = handle_to_idx.get(handle)
    if idx is None:
        return False
    endpoints = [p for p in eps_list[idx] if p]
    if not endpoints:
        return False
    signature = _route_signature(line)
    for other_idx, other in enumerate(lines):
        if other_idx == idx:
            continue
        if _route_signature(other) != signature:
            continue
        if not _segments_nearly_collinear(line, other, unit_factor):
            continue
        other_endpoints = [p for p in eps_list[other_idx] if p]
        for p in endpoints:
            for q in other_endpoints:
                gap = _dist(p, q)
                if conn_tol < gap <= break_max_raw:
                    return True
    return False


def _is_device(block: dict) -> bool:
    """
    블록이 전기 기구(조명, 스위치, 콘센트 등)인지 판별한다.

    판별 우선순위:
      1. block.category 필드: elec_attr_extractor 또는 mapping_agent가 부여한 확정 카테고리
      2. effective_name 또는 block_name 패턴 (한/영 혼용)
      3. layer 이름 패턴: E-OUTLET, E-SOCKET, E-LIGHT, E-SWITCH 등
    """
    # 1순위: category 필드 직접 확인
    category = str(block.get("category") or "").upper().strip()
    if category and category in _DEVICE_CATEGORIES:
        return True

    # 2순위: 블록 이름 패턴 매칭 (한국어·영문 혼용)
    name = str(
        block.get("effective_name") or
        block.get("block_name") or
        block.get("standard_name") or ""
    )
    if name and bool(_DEVICE_NAME_RE.search(name)):
        return True

    # 3순위: 레이어명 패턴 매칭 (E-OUTLET, E-SOCKET, E-LIGHT, E-SWITCH)
    layer = str(block.get("layer") or "")
    if layer and bool(_DEVICE_NAME_RE.search(layer)):
        return True

    return False


def _ann_text(e: dict) -> str:
    return str(e.get("text") or e.get("content") or "").strip()


def _ann_pos(e: dict) -> tuple | None:
    return _pt(e, "position", "insert_point", "center")


def _is_elec_ann(text: str) -> bool:
    return bool(_ELEC_ANN_RE.match(text.strip()))


def _etype(e: dict) -> str:
    return str(e.get("raw_type") or e.get("type") or "").upper()


def evaluate_drawing_intent(elements: list[dict]) -> dict:
    counts: dict[str, int] = defaultdict(int)
    for e in elements:
        counts[_etype(e)] += 1
    text_blob = " ".join(
        str(e.get("text") or e.get("content") or e.get("layer") or "")
        for e in elements
    ).upper()
    annotation_count = counts["TEXT"] + counts["MTEXT"] + counts["DIMENSION"]
    circle_count = counts["CIRCLE"]
    line_count = counts["LINE"] + counts["POLYLINE"] + counts["LWPOLYLINE"] + counts["ARC"]
    insert_count = counts["INSERT"] + counts["BLOCK"]
    wire_layer_count = sum(1 for e in elements if _WIRE_LAYER_RE.search(str(e.get("layer") or "")))
    features: list[str] = []
    scores = {"DETAIL_DRAWING": 0.0, "WIRING_PLAN": 0.0, "EQUIPMENT_PLAN": 0.0, "GROUNDING_PLAN": 0.0, "SLD": 0.0}

    def add(intent: str, score: float, feature: str) -> None:
        scores[intent] += score
        features.append(feature)

    if re.search(r"\b(?:SLD|SINGLE\s*LINE|MCCB|MCB|ELB|ACB|LOAD|PANEL)\b", text_blob):
        add("SLD", 2.0, "single-line/panel keywords")
    if re.search(r"22\.?9\s*KV|22\.?9KV|HIGH\s*VOLT|수변전|CUBICLE|TR\b", text_blob, re.IGNORECASE):
        add("EQUIPMENT_PLAN", 2.2, "22.9kV/substation equipment keywords")
    grounding_hits = _GROUNDING_INTENT_RE.findall(text_blob)
    if grounding_hits:
        add("GROUNDING_PLAN", 3.2, "grounding/test/E1/E2 keywords")
    if len(grounding_hits) >= 3:
        add("GROUNDING_PLAN", 1.4, "grounding keyword density")
    if re.search(r"FGV\s*\d+|접지\s*(선|봉|저항|도체)|외함\s*접지|L\.?\s*A", text_blob, re.IGNORECASE):
        add("GROUNDING_PLAN", 1.2, "grounding conductor/equipment annotations")
    if re.search(r"CABLE|TRAY|DUCT|CONDUIT|WIRE|ROUTE|CV|HFIX", text_blob, re.IGNORECASE):
        add("WIRING_PLAN", 1.8, "cable/tray/routing keywords")
    if wire_layer_count >= max(3, line_count * 0.12):
        add("WIRING_PLAN", 1.6, "wire-like layer density")
    if line_count >= 20 and annotation_count >= 5:
        add("EQUIPMENT_PLAN", 0.8, "plan-like mixed geometry")
    if re.search(r"(DETAIL|\uc0c1\uc138)", text_blob, re.IGNORECASE):
        add("DETAIL_DRAWING", 1.7, "detail keyword")
    if annotation_count >= 5 and circle_count >= 4 and insert_count <= 2 and scores["WIRING_PLAN"] < 1.0 and scores["GROUNDING_PLAN"] < 1.0:
        add("DETAIL_DRAWING", 1.2, "detail-like circle/dimension mix")

    intent = max(scores, key=scores.get)
    confidence = min(1.0, scores[intent] / max(sum(v for v in scores.values() if v > 0), 1.0) + 0.25)
    if scores[intent] <= 0:
        intent = "UNKNOWN"
        confidence = 0.0
    return {
        "drawing_intent": intent,
        "intent_confidence": round(confidence, 4),
        "intent_features": features,
        "intent_scores": {k: round(v, 4) for k, v in scores.items()},
    }


def classify_drawing_intent(elements: list[dict]) -> str:
    return str(evaluate_drawing_intent(elements).get("drawing_intent") or "UNKNOWN")


def _length(e: dict) -> float:
    try:
        if e.get("length") is not None:
            return float(e.get("length"))
    except (TypeError, ValueError):
        pass
    s, t = _endpoints(e)
    if s and t:
        return _dist(s, t)
    pts = e.get("vertices") or e.get("points") or []
    total = 0.0
    for i in range(len(pts) - 1):
        try:
            a = float(pts[i]["x"]), float(pts[i]["y"])
            b = float(pts[i + 1]["x"]), float(pts[i + 1]["y"])
        except (KeyError, TypeError, ValueError):
            continue
        total += _dist(a, b)
    return total


def _entity_inside_any_bbox(e: dict, boxes: list[dict[str, float]], margin: float = 0.0) -> bool:
    pts = [p for p in _endpoints(e) if p]
    center = _pt(e, "position", "center", "insert_point")
    if center:
        pts.append(center)
    if not pts:
        return False
    return any(all(_point_in_expanded_bbox(p, box, margin) for p in pts) for box in boxes)


def _near_wire_annotation(e: dict, texts: list[dict], max_distance: float) -> bool:
    s, t = _endpoints(e)
    if not s and not t:
        return False
    for ann in texts:
        label = _ann_text(ann)
        pos = _ann_pos(ann)
        if not pos or not _WIRE_TEXT_RE.search(label):
            continue
        if s and t:
            d, _ = _point_segment(pos, s, t)
            if d <= max_distance:
                return True
        elif s and _dist(pos, s) <= max_distance:
            return True
    return False


def _wire_size_from_text(text: str) -> float:
    match = _WIRE_SIZE_TEXT_RE.search(text)
    if not match:
        return 0.0
    try:
        return float(match.group(1))
    except (TypeError, ValueError):
        return 0.0


def _nearest_wire_annotation_attrs(e: dict, texts: list[dict], max_distance: float) -> dict:
    s, t = _endpoints(e)
    if not s and not t:
        return {}
    best: tuple[float, dict] | None = None
    for ann in texts:
        label = _ann_text(ann)
        pos = _ann_pos(ann)
        if not pos or not _WIRE_TEXT_RE.search(label):
            continue
        if s and t:
            distance, _ = _point_segment(pos, s, t)
        elif s:
            distance = _dist(pos, s)
        else:
            continue
        if distance > max_distance:
            continue
        cable_sqmm = _wire_size_from_text(label)
        attrs = {
            "wire_annotation_handle": str(ann.get("handle") or ""),
            "wire_annotation_text": label,
            "wire_annotation_distance": round(distance, 3),
        }
        if cable_sqmm:
            attrs["cable_sqmm"] = cable_sqmm
        if best is None or distance < best[0]:
            best = (distance, attrs)
    return best[1] if best else {}


def _annotation_near_distance(drawing_intent: str, unit_factor: float) -> float:
    near_mm = _GROUNDING_ANNOTATION_NEAR_MM if drawing_intent == "GROUNDING_PLAN" else _ANNOTATION_NEAR_MM
    return near_mm / max(unit_factor, 1e-9)


def _is_wire_candidate(
    e: dict,
    *,
    drawing_intent: str,
    terminal_bboxes: list[dict[str, float]],
    texts: list[dict],
    unit_factor: float,
) -> bool:
    raw = _etype(e)
    if raw not in _LINE_RAW:
        return False
    if raw == "ARC":
        return False
    layer = str(e.get("layer") or "")
    if raw in {"POLYLINE", "LWPOLYLINE"} and (
        e.get("is_closed") is True
        or str(e.get("closed") or "").lower() == "true"
    ):
        return False
    has_wire_layer = bool(_WIRE_LAYER_RE.search(layer))
    has_wire_attr = bool(e.get("cable_sqmm") or e.get("voltage"))
    has_wire_text = _near_wire_annotation(e, texts, _annotation_near_distance(drawing_intent, unit_factor))
    is_grounding = drawing_intent == "GROUNDING_PLAN"
    if _EXCLUDED_WIRE_LAYER_RE.search(layer) and not (is_grounding and (has_wire_attr or has_wire_text)):
        return False
    if _entity_inside_any_bbox(e, terminal_bboxes, margin=20.0 / max(unit_factor, 1e-9)):
        if not (is_grounding and (has_wire_attr or has_wire_text)):
            return False
    min_len_mm = _GROUNDING_MIN_WIRE_LENGTH_MM if is_grounding else _MIN_WIRE_LENGTH_MM
    min_len = min_len_mm / max(unit_factor, 1e-9)
    if _length(e) < min_len:
        return False
    if drawing_intent == "DETAIL_DRAWING":
        return has_wire_layer and (has_wire_attr or has_wire_text)
    return has_wire_layer or has_wire_attr or has_wire_text


def _wire_suppression_reason(
    e: dict,
    *,
    drawing_intent: str,
    terminal_bboxes: list[dict[str, float]],
    texts: list[dict],
    unit_factor: float,
) -> str:
    raw = _etype(e)
    layer = str(e.get("layer") or "")
    if raw not in _LINE_RAW:
        return "not_line_like"
    if raw == "ARC":
        return "arc_shape"
    if raw in {"POLYLINE", "LWPOLYLINE"} and (e.get("is_closed") is True or str(e.get("closed") or "").lower() == "true"):
        return "closed_shape"
    has_wire_layer = bool(_WIRE_LAYER_RE.search(layer))
    has_wire_attr = bool(e.get("cable_sqmm") or e.get("voltage"))
    has_wire_text = _near_wire_annotation(e, texts, _annotation_near_distance(drawing_intent, unit_factor))
    is_grounding = drawing_intent == "GROUNDING_PLAN"
    if _EXCLUDED_WIRE_LAYER_RE.search(layer) and not (is_grounding and (has_wire_attr or has_wire_text)):
        return "excluded_layer"
    if _entity_inside_any_bbox(e, terminal_bboxes, margin=20.0 / max(unit_factor, 1e-9)):
        if not (is_grounding and (has_wire_attr or has_wire_text)):
            return "inside_terminal_symbol"
    min_len_mm = _GROUNDING_MIN_WIRE_LENGTH_MM if is_grounding else _MIN_WIRE_LENGTH_MM
    if _length(e) < min_len_mm / max(unit_factor, 1e-9):
        return "too_short_for_route"
    if drawing_intent == "DETAIL_DRAWING" and not (has_wire_layer and (has_wire_attr or has_wire_text)):
        return "detail_scope_requires_wire_layer_and_annotation"
    if not (has_wire_layer or has_wire_attr or has_wire_text):
        return "no_wire_semantic"
    return ""


def _circle_data(e: dict) -> tuple[tuple[float, float], float] | None:
    center = _pt(e, "center", "position")
    if not center:
        return None
    try:
        radius = float(e.get("radius") or 0)
    except (TypeError, ValueError):
        radius = 0.0
    if radius <= 0:
        return None
    return center, radius


def _bbox_from_circles(circles: list[dict]) -> dict[str, float]:
    xs: list[float] = []
    ys: list[float] = []
    for c in circles:
        data = _circle_data(c)
        if not data:
            continue
        (x, y), r = data
        xs.extend([x - r, x + r])
        ys.extend([y - r, y + r])
    return {
        "x1": round(min(xs), 4),
        "y1": round(min(ys), 4),
        "x2": round(max(xs), 4),
        "y2": round(max(ys), 4),
    }


def _collapse_concentric_circles(circles: list[dict]) -> list[dict]:
    """Treat nested terminal rings at the same center as one physical terminal."""
    groups: list[list[dict]] = []
    parsed = [(c, *_circle_data(c)) for c in circles if _circle_data(c)]
    if not parsed:
        return []

    radii = sorted(radius for _, _, radius in parsed)
    median_radius = radii[len(radii) // 2] if radii else 0.0
    center_tol = max(median_radius * 0.2, 0.25)

    for circle, center, _radius in parsed:
        placed = False
        for group in groups:
            ref = _circle_data(group[0])
            if ref and _dist(center, ref[0]) <= center_tol:
                group.append(circle)
                placed = True
                break
        if not placed:
            groups.append([circle])

    representatives: list[dict] = []
    for group in groups:
        with_data = [(c, *_circle_data(c)) for c in group if _circle_data(c)]
        if not with_data:
            continue
        cx = sum(center[0] for _, center, _ in with_data) / len(with_data)
        cy = sum(center[1] for _, center, _ in with_data) / len(with_data)
        rep, _, radius = max(with_data, key=lambda item: item[2])
        handles = [str(c.get("handle") or "") for c, _, _ in with_data if c.get("handle")]
        collapsed = dict(rep)
        collapsed["center"] = {"x": round(cx, 4), "y": round(cy, 4)}
        collapsed["radius"] = radius
        collapsed["member_handles"] = handles
        representatives.append(collapsed)

    return representatives


def _point_in_expanded_bbox(p: tuple[float, float], bbox: dict[str, float], margin: float) -> bool:
    return (
        bbox["x1"] - margin <= p[0] <= bbox["x2"] + margin
        and bbox["y1"] - margin <= p[1] <= bbox["y2"] + margin
    )


def _symmetry_score(points: list[tuple[float, float]]) -> float:
    if len(points) < 4:
        return 0.0
    cx = sum(p[0] for p in points) / len(points)
    cy = sum(p[1] for p in points) / len(points)
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    scale = max(max(xs) - min(xs), max(ys) - min(ys), 1.0)
    tolerance = scale * 0.15
    matched = 0
    for x, y in points:
        mirror = (2 * cx - x, 2 * cy - y)
        if any(_dist(mirror, other) <= tolerance for other in points):
            matched += 1
    return round(matched / len(points), 4)


def _cluster_circles(circles: list[dict]) -> list[list[dict]]:
    if not circles:
        return []
    parsed = [(c, *_circle_data(c)) for c in circles if _circle_data(c)]
    if not parsed:
        return []

    nearest: list[float] = []
    for _, center, _ in parsed:
        dists = [_dist(center, other_center) for _, other_center, _ in parsed if other_center != center]
        if dists:
            nearest.append(min(dists))
    radii = [radius for _, _, radius in parsed]
    nearest.sort()
    radii.sort()
    median_nearest = nearest[len(nearest) // 2] if nearest else 0.0
    median_radius = radii[len(radii) // 2] if radii else 0.0
    threshold = max(median_nearest * 1.8, median_radius * 8.0, 1.0)

    uf = _UF(len(parsed))
    for i, (_, center_i, radius_i) in enumerate(parsed):
        for j in range(i + 1, len(parsed)):
            _, center_j, radius_j = parsed[j]
            if _dist(center_i, center_j) <= threshold + radius_i + radius_j:
                uf.union(i, j)

    clusters: list[list[dict]] = []
    for idxs in uf.groups().values():
        clusters.append([parsed[i][0] for i in idxs])
    return clusters


def detect_terminal_candidates(elements: list[dict], unit_factor: float = 1.0) -> list[dict]:
    def _etype(e): return str(e.get("raw_type") or e.get("type") or "").upper()

    circles = [e for e in elements if _etype(e) in _CIRCLE_RAW and _circle_data(e)]
    texts = [
        e for e in elements
        if _etype(e) in _TEXT_RAW
        and _ann_pos(e) is not None
        and _ann_text(e)
    ]
    # Short non-circle entities that can form terminal housing geometry
    body_candidates = [
        e for e in elements
        if _etype(e) in {"LINE", "POLYLINE", "LWPOLYLINE"} and e.get("handle")
    ]
    candidates: list[dict] = []

    for raw_cluster in _cluster_circles(circles):
        cluster = _collapse_concentric_circles(raw_cluster)
        if len(cluster) < 4:
            continue
        points = [_circle_data(c)[0] for c in cluster if _circle_data(c)]
        bbox = _bbox_from_circles(cluster)
        width = bbox["x2"] - bbox["x1"]
        height = bbox["y2"] - bbox["y1"]
        if width <= 0 or height <= 0:
            continue

        # Median nearest-neighbor spacing between terminal units
        all_dists = sorted(
            _dist(points[i], points[j])
            for i in range(len(points))
            for j in range(i + 1, len(points))
        )
        median_spacing = all_dists[len(all_dists) // 2] if all_dists else 0.0

        symmetry = _symmetry_score(points)
        # 텍스트 탐색 마진: 심볼 아래에 배치된 라벨(TEST/접지 등)을 잡기 위해
        # 바운딩박스 크기의 1.5배까지 확장 (기존 0.75는 심볼 바로 옆만 탐지)
        margin = max(width, height) * 1.5
        nearby_texts = []
        for text in texts:
            pos = _ann_pos(text)
            if pos and _point_in_expanded_bbox(pos, bbox, margin):
                nearby_texts.append({
                    "handle": str(text.get("handle") or ""),
                    "text": _ann_text(text),
                })

        # A deformed 2x2 terminal is still a terminal candidate; QA should flag it
        # instead of losing the cluster before validation.
        if len(cluster) > 4 and symmetry < 0.5 and not nearby_texts:
            continue

        # Build per-circle body map: lines/polylines forming each sub-terminal housing
        circle_body_map: dict[str, list[str]] = {}
        max_body_len = max(median_spacing * 2.5, 1.0) if median_spacing > 0 else 0.0
        for circle in cluster:
            cd = _circle_data(circle)
            if not cd:
                continue
            (cx, cy), cr = cd
            expand = max(cr * 3.0, median_spacing * 0.45) if median_spacing > 0 else cr * 3.0
            sub_box = {"x1": cx - expand, "y1": cy - expand, "x2": cx + expand, "y2": cy + expand}
            body_hs = [
                str(e["handle"])
                for e in body_candidates
                if _entity_inside_any_bbox(e, [sub_box])
                and (max_body_len <= 0 or _length(e) < max_body_len)
            ]
            circle_body_map[str(circle.get("handle") or "")] = body_hs

        candidate_type = "breaker_terminal" if any(
            re.search(r"\b(?:MCCB|MCB|ELB|ACB|CB|[34]P)\b", t["text"], re.IGNORECASE)
            for t in nearby_texts
        ) else "terminal_block"

        # Derive expected layout from circle positions
        xs = sorted(set(round(p[0], 1) for p in points))
        ys = sorted(set(round(p[1], 1) for p in points))
        expected_layout = {
            "cols": len(xs),
            "rows": len(ys),
            "spacing_x": round((xs[-1] - xs[0]) / max(len(xs) - 1, 1) * unit_factor, 4) if len(xs) > 1 else 0.0,
            "spacing_y": round((ys[-1] - ys[0]) / max(len(ys) - 1, 1) * unit_factor, 4) if len(ys) > 1 else 0.0,
        }

        candidates.append({
            "type": "terminal_candidate",
            "candidate_type": candidate_type,
            "circle_handles": [str(c.get("handle") or "") for c in cluster],
            "raw_circle_handles": [str(c.get("handle") or "") for c in raw_cluster],
            "circles": [
                {
                    "handle": str(c.get("handle") or ""),
                    "member_handles": list(c.get("member_handles") or []),
                    "center": {"x": _circle_data(c)[0][0], "y": _circle_data(c)[0][1]},
                    "radius": _circle_data(c)[1],
                }
                for c in cluster
                if _circle_data(c)
            ],
            "circle_count": len(cluster),
            "raw_circle_count": len(raw_cluster),
            "bbox": bbox,
            "bbox_mm": {
                "width": round(width * unit_factor, 4),
                "height": round(height * unit_factor, 4),
            },
            "symmetry_score": symmetry,
            "expected_pattern": "2x2_grid" if len(cluster) == 4 else "circle_grid",
            "expected_layout": expected_layout,
            "circle_body_map": circle_body_map,
            "nearby_texts": nearby_texts,
            "confidence": round(min(1.0, 0.55 + symmetry * 0.35 + min(len(nearby_texts), 3) * 0.05), 4),
        })

    return candidates


# 공간 그리드 인덱스
def _build_grid(
    eps_list: list[tuple[tuple | None, tuple | None]],
    cell: float,
) -> dict[tuple[int, int], list[int]]:
    grid: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i, (s, t) in enumerate(eps_list):
        for p in (s, t):
            if p:
                grid[(int(p[0] // cell), int(p[1] // cell))].append(i)
    return dict(grid)


def _neighbors(i: int, eps_list, grid, cell) -> set[int]:
    result: set[int] = set()
    for p in eps_list[i]:
        if p:
            cx, cy = int(p[0] // cell), int(p[1] // cell)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    result.update(grid.get((cx + dx, cy + dy), []))
    result.discard(i)
    return result


# Union-Find
# Union-Find
class _UF:
    def __init__(self, n: int):
        self.p = list(range(n))

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, x: int, y: int) -> None:
        px, py = self.find(x), self.find(y)
        if px != py:
            self.p[px] = py

    def groups(self) -> dict[int, list[int]]:
        g: dict[int, list[int]] = defaultdict(list)
        for i in range(len(self.p)):
            g[self.find(i)].append(i)
        return dict(g)


# 메인 클래스
class ElecTopologyBuilder:
    """전기 도면 회로 경로 및 분전반 연결 그래프를 구성한다.

    개선 사항:
      - 공간 그리드 인덱스로 대형 도면 성능 확보
      - T-접합 감지: 끝점이 다른 선 중간에 닿는 분기 회로 인식
      - Annotation Bridging: L1/L2 텍스트로 끊긴 선도 하나의 회로로 인식
      - unit_factor: inch 도면도 mm 기준으로 처리
    """
    def __init__(self, tolerance: float = _CONN_TOL):
        self.tol = tolerance

    def build(self, elements: list[dict], unit_factor: float = 1.0) -> dict:
        """
        Args:
            elements   : 도면 엔티티 리스트
            unit_factor: drawing_unit을 mm로 변환하는 계수 (1.0=mm, 25.4=inch)
        """
        intent_meta = evaluate_drawing_intent(elements)
        drawing_intent = str(intent_meta.get("drawing_intent") or "UNKNOWN")
        terminal_candidates = detect_terminal_candidates(elements, unit_factor)
        terminal_bboxes = [
            c["bbox"] for c in terminal_candidates
            if isinstance(c.get("bbox"), dict)
        ]
        all_texts = [
            e for e in elements
            if _etype(e) in _TEXT_RAW
            and _ann_pos(e) is not None
            and _ann_text(e)
        ]
        raw_lines = [e for e in elements if _etype(e) in _LINE_RAW and e.get("handle")]
        lines = []
        suppressed_wire_candidates: list[dict] = []
        for e in raw_lines:
            annotation_attrs = _nearest_wire_annotation_attrs(
                e,
                all_texts,
                _annotation_near_distance(drawing_intent, unit_factor),
            )
            if annotation_attrs:
                if annotation_attrs.get("cable_sqmm") and not e.get("cable_sqmm"):
                    e["cable_sqmm"] = annotation_attrs["cable_sqmm"]
                e.setdefault("wire_annotation", {}).update(annotation_attrs)
            if _is_wire_candidate(
                e,
                drawing_intent=drawing_intent,
                terminal_bboxes=terminal_bboxes,
                texts=all_texts,
                unit_factor=unit_factor,
            ):
                lines.append(e)
            else:
                suppressed_wire_candidates.append({
                    "handle": str(e.get("handle") or ""),
                    "layer": str(e.get("layer") or ""),
                    "reason": _wire_suppression_reason(
                        e,
                        drawing_intent=drawing_intent,
                        terminal_bboxes=terminal_bboxes,
                        texts=all_texts,
                        unit_factor=unit_factor,
                    ),
                })
        blocks = [e for e in elements if _etype(e) in _BLOCK_RAW and e.get("handle")]
        texts  = [e for e in elements
                  if _etype(e) in _TEXT_RAW
                  and _is_elec_ann(_ann_text(e))
                  and _ann_pos(e) is not None]
        wire_filter_stats = {
            "raw_line_like_count": len(raw_lines),
            "wire_candidate_count": len(lines),
            "filtered_out_count": len(raw_lines) - len(lines),
            "wire_candidate_handles": [str(e.get("handle") or "") for e in lines],
            "suppressed_wire_candidates": suppressed_wire_candidates[:200],
        }

        if len(lines) > _MAX_LINES:
            _log.warning("[ElecTopology] LINE %d개 초과 - 앞 %d개만 처리", len(lines), _MAX_LINES)
            lines = lines[:_MAX_LINES]

        # 끝점 사전 구성
        eps_list: list[tuple] = [_endpoints(e) for e in lines]
        handle_to_idx = {
            str(e.get("handle") or ""): i
            for i, e in enumerate(lines)
            if e.get("handle")
        }
        cell = self.tol * 2
        grid = _build_grid(eps_list, cell)
        uf   = _UF(len(lines))

        # [1] 끝점-끝점 연결
        for i in range(len(lines)):
            si, ti = eps_list[i]
            pts_i = [p for p in (si, ti) if p]
            if not pts_i:
                continue
            for j in _neighbors(i, eps_list, grid, cell):
                if j <= i:
                    continue
                sj, tj = eps_list[j]
                pts_j = [p for p in (sj, tj) if p]
                if any(_dist(pi, pj) <= self.tol for pi in pts_i for pj in pts_j):
                    uf.union(i, j)

        # [2] T-접합 감지: 끝점이 다른 선의 중간에 닿는 경우
        wide_cell = self.tol * 6
        wide_grid = _build_grid(eps_list, wide_cell)
        for i in range(len(lines)):
            si, ti = eps_list[i]
            pts_i = [p for p in (si, ti) if p]
            if not pts_i:
                continue
            for j in _neighbors(i, eps_list, wide_grid, wide_cell):
                if j == i or uf.find(i) == uf.find(j):
                    continue
                sj, tj = eps_list[j]
                if not sj or not tj:
                    continue
                for pi in pts_i:
                    d, t_param = _point_segment(pi, sj, tj)
                    if d <= self.tol and _T_PARAM_TOL < t_param < (1 - _T_PARAM_TOL):
                        uf.union(i, j)
                        break

        # [2-b] ARC 연결 보강: ARC는 선 후보에서는 제외되지만, 코너/엘보 연결선으로
        # 두 배선 끝점을 잇는 경우가 많으므로 회로 그래프에서는 연결 요소로 반영한다.
        arc_bridge_connections: list[dict] = []
        arc_gap_max = _BREAK_DETECT_MAX_MM / max(unit_factor, 1e-9)
        for arc in raw_lines:
            if _etype(arc) != "ARC":
                continue
            candidates = _arc_bridge_candidates(arc, lines, eps_list, unit_factor, self.tol)
            if len(candidates) < 2:
                continue
            for pos_a in range(len(candidates)):
                idx_a, side_a, point_a = candidates[pos_a]
                for idx_b, side_b, point_b in candidates[pos_a + 1:]:
                    if idx_a == idx_b or uf.find(idx_a) == uf.find(idx_b):
                        continue
                    gap = _dist(point_a, point_b)
                    if gap > arc_gap_max:
                        continue
                    if not _arc_can_bridge_route(arc, lines[idx_a], lines[idx_b]):
                        continue
                    uf.union(idx_a, idx_b)
                    arc_bridge_connections.append({
                        "arc_handle": str(arc.get("handle") or ""),
                        "from_handle": str(lines[idx_a].get("handle") or ""),
                        "from_side": side_a,
                        "to_handle": str(lines[idx_b].get("handle") or ""),
                        "to_side": side_b,
                        "gap_mm": round(gap * unit_factor, 2),
                    })
                    _log.debug(
                        "[ElecTopology] ARC bridge connected: %s %s(%s) ↔ %s(%s) gap=%.1f",
                        arc.get("handle"),
                        lines[idx_a].get("handle"), side_a,
                        lines[idx_b].get("handle"), side_b,
                        gap * unit_factor,
                    )

        # [3] Annotation Bridging
        # [3] Annotation Bridging
        virtual_connections: list[dict] = []
        if texts:
            gap_max  = _ANNOTATION_GAP_MAX_MM / max(unit_factor, 1e-9)
            near_tol = _ANNOTATION_NEAR_MM    / max(unit_factor, 1e-9)
            for i in range(len(lines)):
                si, ti = eps_list[i]
                pts_i = [p for p in (si, ti) if p]
                if not pts_i:
                    continue
                for j in range(i + 1, len(lines)):
                    if uf.find(i) == uf.find(j):
                        continue
                    sj, tj = eps_list[j]
                    pts_j = [p for p in (sj, tj) if p]
                    if not pts_j:
                        continue
                    pairs = [(pa, pb, _dist(pa, pb)) for pa in pts_i for pb in pts_j]
                    pa, pb, gap = min(pairs, key=lambda x: x[2])
                    if gap <= self.tol or gap > gap_max:
                        continue
                    # 두 끝점 사이에 주석이 있는지 확인
                    matched_labels: list[str] = []
                    matched_handles: list[str] = []
                    for ann in texts:
                        pos = _ann_pos(ann)
                        if pos:
                            d, _ = _point_segment(pos, pa, pb)
                            if d <= near_tol:
                                matched_labels.append(_ann_text(ann))
                                if ann.get("handle"):
                                    matched_handles.append(str(ann["handle"]))
                    if matched_labels:
                        uf.union(i, j)
                        virtual_connections.append({
                            "from_handle": str(lines[i].get("handle") or ""),
                            "to_handle":   str(lines[j].get("handle") or ""),
                            "annotation_handles": matched_handles,
                            "labels":   matched_labels,
                            "gap":      round(gap, 2),
                            "gap_mm":   round(gap * unit_factor, 2),
                        })

        # circuit_runs 구성
        circuit_runs: list[dict] = []
        for root, idxs in uf.groups().items():
            group = [lines[i] for i in idxs]
            raw_len = sum(float(e.get("length") or 0) for e in group)
            voltages = [float(e["voltage"])    for e in group if e.get("voltage")]
            sqmms    = [float(e["cable_sqmm"]) for e in group if e.get("cable_sqmm")]
            circuit_runs.append({
                "run_id":          root,
                "handles":         [str(e.get("handle") or "") for e in group],
                "total_length":    round(raw_len, 2),
                "total_length_mm": round(raw_len * unit_factor, 2),
                "voltage":    voltages[0] if voltages else 0.0,
                "cable_sqmm": sqmms[0]    if sqmms    else 0.0,
                "connected_panels":  [],
                # 회로에 연결된 전기 기구 목록 (조명, 스위치, 콘센트 등)
                "connected_devices": [],
            })

        # 분전반-회로 연결
        panel_graph: dict[str, list[str]] = {}
        blk_tol = self.tol * _BLOCK_CONN_TOL_MUL

        for block in blocks:
            bh = str(block.get("handle") or "")
            bn = str(
                block.get("effective_name") or
                block.get("block_name") or
                block.get("standard_name") or ""
            ).upper()
            if not any(bn.startswith(p) for p in _PANEL_PREFIXES):
                continue
            bc = _pt(block, "center", "position", "insert_point")
            if not bc:
                continue
            panel_graph[bh] = []
            for run in circuit_runs:
                for hi in run["handles"]:
                    idx = handle_to_idx.get(hi)
                    if idx is None:
                        continue
                    si, ti = eps_list[idx]
                    for ep in filter(None, (si, ti)):
                        if _dist(bc, ep) <= blk_tol:
                            run["connected_panels"].append(bh)
                            panel_graph[bh].append(hi)
                            break

        # 디바이스(조명/스위치/콘센트 등)를 circuit_run에 연결
        # 분전반과 분리된 디바이스는 panel_graph가 아니라 connected_devices에만 기록한다.
        # 판별 기준:
        #   1순위: block.category 필드 (elec_attr_extractor 또는 mapping_agent가 부여)
        #   2순위: effective_name 또는 block_name 패턴 매칭 (_DEVICE_NAME_RE)
        device_graph: dict[str, str] = {}  # {device_handle: run_id}

        for block in blocks:
            bh = str(block.get("handle") or "")

            # 이미 분전반으로 처리한 블록은 건너뜀
            if bh in panel_graph:
                continue

            # 디바이스 여부 판별
            if not _is_device(block):
                continue
            bc = _pt(block, "insert_point", "center", "position")
            if not bc:
                continue

            category = str(block.get("category") or "").upper()
            subtype  = str(block.get("subtype")  or "").upper()

            # 회로 끝점과의 거리 기준으로 가장 가까운 circuit_run에 연결
            # 동일 블록이 여러 회로 끝점에 닿는 경우 최근접 1개에만 연결
            best_dist = blk_tol  # 허용 오차 이내에서만 연결
            best_run = None
            for run in circuit_runs:
                for hi in run["handles"]:
                    idx = handle_to_idx.get(hi)
                    if idx is None:
                        continue
                    si, ti = eps_list[idx]
                    for ep in filter(None, (si, ti)):
                        d = _dist(bc, ep)
                        if d <= best_dist:
                            best_dist = d
                            best_run  = run

            if best_run is not None:
                best_run["connected_devices"].append({
                    "handle":   bh,
                    "category": category or "UNKNOWN",
                    "subtype":  subtype,
                    "name":     str(
                        block.get("effective_name") or
                        block.get("block_name") or ""
                    ),
                })
                device_graph[bh] = best_run["run_id"]

        _log.debug(
            "[ElecTopology] 디바이스 연결 완료: %d개(조명/스위치/콘센트 등)",
            len(device_graph),
        )

        # 노이즈 회로 제거
        break_max_raw = _BREAK_DETECT_MAX_MM / max(unit_factor, 1e-9)
        noise_handles: set[str] = set()
        for run in circuit_runs:
            if len(run["handles"]) == 1 and run["total_length_mm"] < _NOISE_LINE_MAX_MM:
                h = str(run["handles"][0])
                idx = handle_to_idx.get(h)
                if idx is not None and _is_short_route_gap_fragment(
                    h,
                    lines[idx],
                    lines,
                    eps_list,
                    handle_to_idx,
                    conn_tol=self.tol,
                    break_max_raw=break_max_raw,
                    unit_factor=unit_factor,
                ):
                    continue
                noise_handles.add(h)

        clean_runs = [
            r for r in circuit_runs
            if not (
                len(r["handles"]) == 1
                and r["total_length_mm"] < _NOISE_LINE_MAX_MM
                and str(r["handles"][0]) in noise_handles
            )
        ]
        noise_removed = len(circuit_runs) - len(clean_runs)

        # dangling / 단선 감지
        block_pts = [
            p for p in (_pt(b, "center", "position", "insert_point") for b in blocks)
            if p
        ]

        all_eps: list[dict] = []
        for i, line in enumerate(lines):
            h = str(line.get("handle") or "")
            if h in noise_handles:
                continue
            si, ti = eps_list[i]
            if si:
                all_eps.append({"handle": h, "side": "start", "x": si[0], "y": si[1]})
            if ti:
                all_eps.append({"handle": h, "side": "end",   "x": ti[0], "y": ti[1]})

        dangling: list[dict] = []
        for ep in all_eps:
            # 블록 근처 끝점은 연결된 것으로 간주
            if any(math.hypot(ep["x"] - bp[0], ep["y"] - bp[1]) <= blk_tol for bp in block_pts):
                continue
            connected = any(
                ep["handle"] != other["handle"]
                and math.hypot(ep["x"] - other["x"], ep["y"] - other["y"]) <= self.tol
                for other in all_eps
            )
            if not connected:
                dangling.append(ep)

        # 고립된 디바이스(회로에 연결되지 않은 device block) 감지
        # device_graph에 없는 device block = LINE 끝점이 blk_tol 이내에 없음
        isolated_devices: list[dict] = []
        isolated_device_max_gap_raw = _ISOLATED_DEVICE_MAX_GAP_MM / max(unit_factor, 1e-9)
        if drawing_intent != "DETAIL_DRAWING":
            for block in blocks:
                bh = str(block.get("handle") or "")
                if bh in panel_graph or bh in device_graph:
                    continue
                if not _is_device(block):
                    continue
                bc = _pt(block, "insert_point", "center", "position")
                if not bc:
                    continue
                # 가장 가까운 LINE 끝점까지 거리 계산
                nearest_gap_raw: float | None = None
                nearest_line_handle: str = ""
                for ep in all_eps:
                    d = math.hypot(bc[0] - ep["x"], bc[1] - ep["y"])
                    if nearest_gap_raw is None or d < nearest_gap_raw:
                        nearest_gap_raw = d
                        nearest_line_handle = ep["handle"]
                if nearest_gap_raw is None or nearest_gap_raw > isolated_device_max_gap_raw:
                    continue
                category = str(block.get("category") or "").upper()
                name = str(
                    block.get("effective_name") or
                    block.get("block_name") or
                    block.get("standard_name") or ""
                )
                entry: dict = {
                    "handle":   bh,
                    "category": category or "DEVICE",
                    "name":     name,
                    "position": {"x": round(bc[0], 4), "y": round(bc[1], 4)},
                }
                if nearest_gap_raw is not None:
                    entry["gap_mm"] = round(nearest_gap_raw * unit_factor, 2)
                    entry["nearest_line_handle"] = nearest_line_handle
                    # 가까운 dangling 끝점이 있으면 broken_segment 후보로도 기록
                    entry["is_near_dangling"] = nearest_gap_raw <= break_max_raw
                isolated_devices.append(entry)

        handle_to_linetype = {
            str(e.get("handle") or ""): str(e.get("linetype") or "").strip().upper()
            for e in lines
        }
        handle_to_line = {
            str(e.get("handle") or ""): e
            for e in lines
        }

        broken_segments: list[dict] = []
        reported: set[frozenset] = set()
        if drawing_intent != "DETAIL_DRAWING":
            for i, a in enumerate(dangling):
                for b in dangling[i + 1:]:
                    if a["handle"] == b["handle"]:
                        continue
                    gap = math.hypot(a["x"] - b["x"], a["y"] - b["y"])
                    if gap <= break_max_raw:
                        key = frozenset({a["handle"], b["handle"]})
                        if key in reported:
                            continue
                        # 양쪽 모두 다형(점선/파선) 선종이면 의도적 패턴 gap → 단선 아님
                        lt_a = handle_to_linetype.get(a["handle"], "")
                        lt_b = handle_to_linetype.get(b["handle"], "")
                        if (lt_a and lt_b
                                and lt_a not in ("CONTINUOUS", "BYLAYER", "BYBLOCK")
                                and lt_b not in ("CONTINUOUS", "BYLAYER", "BYBLOCK")
                                and bool(_DASHED_LINETYPE_RE.match(lt_a))
                                and bool(_DASHED_LINETYPE_RE.match(lt_b))):
                            _log.debug(
                                "[ElecTopology] broken_segment 억제 (다형 선종): %s(%s) ↔ %s(%s) gap=%.1f",
                                a["handle"], lt_a, b["handle"], lt_b, gap * unit_factor,
                            )
                            continue
                        if _arc_bridges_wire_endpoints(
                            raw_lines,
                            handle_to_line.get(a["handle"]),
                            handle_to_line.get(b["handle"]),
                            (a["x"], a["y"]),
                            (b["x"], b["y"]),
                            unit_factor,
                        ):
                            _log.debug(
                                "[ElecTopology] broken_segment suppressed by bridge ARC: %s <-> %s gap=%.1f",
                                a["handle"], b["handle"], gap * unit_factor,
                            )
                            continue
                        reported.add(key)
                        broken_segments.append({
                            "handle_a": a["handle"], "side_a": a["side"],
                            "handle_b": b["handle"], "side_b": b["side"],
                            "gap_mm":   round(gap * unit_factor, 2),
                            "midpoint": {
                                "x": round((a["x"] + b["x"]) / 2, 4),
                                "y": round((a["y"] + b["y"]) / 2, 4),
                            },
                        })

        if broken_segments:
            _log.warning("[ElecTopology] broken wire candidates detected: %d", len(broken_segments))

        unconnected = sum(1 for r in clean_runs if not r["connected_panels"])
        total_devices = sum(len(r["connected_devices"]) for r in clean_runs)
        _log.info(
            "[ElecTopology] runs=%d noise_removed=%d unconnected=%d "
            "panels=%d devices=%d broken=%d isolated_devices=%d virtual=%d",
            len(clean_runs), noise_removed, unconnected,
            len(panel_graph), total_devices, len(broken_segments),
            len(isolated_devices), len(virtual_connections),
        )

        return {
            "drawing_intent": drawing_intent,
            "intent_confidence": intent_meta.get("intent_confidence", 0.0),
            "intent_features": intent_meta.get("intent_features", []),
            "circuit_runs":        clean_runs,
            "panel_graph":         panel_graph,
            "terminal_candidates": terminal_candidates,
            "broken_segments":     broken_segments,
            "isolated_devices":    isolated_devices,
            "virtual_connections": virtual_connections,
            "arc_bridge_connections": arc_bridge_connections,
            "dangling_endpoints":  dangling,
            "device_graph": device_graph,
            "summary": {
                "run_count":          len(clean_runs),
                "total_lines":        len(lines),
                "drawing_intent":      drawing_intent,
                "intent_confidence":   intent_meta.get("intent_confidence", 0.0),
                "intent_features":      intent_meta.get("intent_features", []),
                "intent_scores":        intent_meta.get("intent_scores", {}),
                "wire_filter":         wire_filter_stats,
                "wire_candidate_count": len(lines),
                "topology_edge_count":  sum(max(len(r.get("handles", [])) - 1, 0) for r in clean_runs),
                "unconnected_wires":  unconnected,
                "panel_count":        len(panel_graph),
                "device_count":       len(device_graph),
                "isolated_device_count": len(isolated_devices),
                "terminal_candidate_count": len(terminal_candidates),
                "broken_count":       len(broken_segments),
                "virtual_conn_count": len(virtual_connections),
                "arc_bridge_count":   len(arc_bridge_connections),
                "noise_removed":      noise_removed,
                "unit_factor":        unit_factor,
            },
        }
