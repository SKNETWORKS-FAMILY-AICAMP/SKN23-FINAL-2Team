"""
File    : backend/services/agents/elec/sub/geometry.py
Author  : 김지우
Create  : 2026-04-24
Description : arch 레이어 없이 전기 엔티티만으로 공간 분석을 수행합니다.
              배관 GeometryPreprocessor 패턴을 전기 도메인에 적용.

  1. proxy_wall 추출 — 긴 축방향 LINE을 벽체 후보로 식별
  2. conduit_clearances — 전선관 간 bbox 최소 이격 계산
  3. panel_clearances  — 분전반·기기와 proxy_wall 간 이격 계산

출력:
  proxy_walls        : [{handle, layer, bbox, _wall_length}, ...]
  conduit_clearances : [{handle_a, handle_b, layer_a, layer_b, separation_drawing, overlapping}, ...]
  panel_clearances   : [{mep_handle, wall_handle, separation_drawing, wall_length, note}, ...]
  summary            : {proxy_walls, elec_blocks, elec_lines}
"""
from __future__ import annotations

import logging
import math
import re
from typing import Any

_log = logging.getLogger(__name__)

_MAX_PAIRS    = 50
_WALL_MIN_LEN = 800.0
_WALL_ANG_TOL = 8.0

_BLOCK_RAW = frozenset({"INSERT", "BLOCK"})
_LINE_RAW  = frozenset({"LINE"})
_REFERENCE_BLOCK_MAX_DIM_MM = 5000.0
_GENERIC_LAYER_RE = re.compile(r"^(?:0|L\d+|LAYER\d*|\d+|XREF|REF)$", re.IGNORECASE)
_ELEC_BLOCK_TEXT_RE = re.compile(
    r"ELEC|EL-|E-|PWR|POWER|LIGHT|SWITCH|SOCKET|OUTLET|PANEL|PNL|MCC|MCCB|"
    r"MCB|ELB|ACB|CB|TRAY|DUCT|CONDUIT|CABLE|WIRE|GND|GROUND|EARTH|"
    r"전기|전등|조명|스위치|콘센트|분전|배전|차단기|전선|접지|트레이|전선관",
    re.IGNORECASE,
)


def _bbox(e: dict):
    b = e.get("bbox")
    if not isinstance(b, dict):
        return None
    try:
        if "x1" in b:
            return float(b["x1"]), float(b["y1"]), float(b["x2"]), float(b["y2"])
        if "min_x" in b:
            return float(b["min_x"]), float(b["min_y"]), float(b["max_x"]), float(b["max_y"])
    except (TypeError, ValueError):
        pass
    return None


def _sep(a: tuple, b: tuple) -> float:
    ax1, ay1 = min(a[0], a[2]), min(a[1], a[3])
    ax2, ay2 = max(a[0], a[2]), max(a[1], a[3])
    bx1, by1 = min(b[0], b[2]), min(b[1], b[3])
    bx2, by2 = max(b[0], b[2]), max(b[1], b[3])
    dx = max(0.0, max(ax1, bx1) - min(ax2, bx2))
    dy = max(0.0, max(ay1, by1) - min(ay2, by2))
    return math.hypot(dx, dy)


def _is_overlap(a: tuple, b: tuple) -> bool:
    return _sep(a, b) == 0.0


def _bbox_max_dim_mm(bb: tuple[float, float, float, float], unit_factor: float) -> float:
    return max(abs(bb[2] - bb[0]), abs(bb[3] - bb[1])) * unit_factor


def _has_explicit_elec_evidence(e: dict) -> bool:
    attrs = e.get("attributes") or e.get("properties") or {}
    text = " ".join(
        str(v or "")
        for v in (
            e.get("name"),
            e.get("block_name"),
            e.get("effective_name"),
            e.get("layer"),
            e.get("category"),
            e.get("elec_category"),
            attrs.get("TAG_NAME") if isinstance(attrs, dict) else "",
            attrs.get("CIRCUIT_NO") if isinstance(attrs, dict) else "",
            attrs.get("BREAKER_AMPS") if isinstance(attrs, dict) else "",
            attrs.get("VOLTAGE") if isinstance(attrs, dict) else "",
        )
    )
    return bool(
        e.get("cable_sqmm")
        or e.get("voltage")
        or e.get("circuit_breaker_a")
        or e.get("electric_review_scope") == "review"
        or _ELEC_BLOCK_TEXT_RE.search(text)
    )


def _is_reference_or_background_block(
    e: dict,
    bb: tuple[float, float, float, float],
    unit_factor: float,
) -> bool:
    """크고 일반적인 블록은 건축/xref 배경일 가능성이 높으므로 전기 충돌 검사에서 제외."""
    if _bbox_max_dim_mm(bb, unit_factor) <= _REFERENCE_BLOCK_MAX_DIM_MM:
        return False
    if _has_explicit_elec_evidence(e):
        return False
    layer = str(e.get("layer") or "").strip()
    block_name = str(e.get("block_name") or e.get("effective_name") or "").strip()
    return bool(_GENERIC_LAYER_RE.match(layer) or block_name)


def calc_clearance_move_vector(
    target_bbox: tuple,
    ref_bbox: tuple,
    current_sep_mm: float,
    required_sep_mm: float,
) -> tuple[float, float]:
    """
    이격거리 위반 시 target 객체를 ref 객체에서 밀어낼 이동 벡터를 계산한다.

    Args:
        target_bbox    : (x1, y1, x2, y2) — 이동할 객체 bbox
        ref_bbox       : (x1, y1, x2, y2) — 기준 객체 bbox
        current_sep_mm : 현재 이격 거리 (mm)
        required_sep_mm: 목표 이격 거리 (mm)

    Returns:
        (delta_x, delta_y) — target 객체에 적용할 이동 벡터 (mm)
    """
    # 두 객체 중심 계산
    tx = (target_bbox[0] + target_bbox[2]) / 2.0
    ty = (target_bbox[1] + target_bbox[3]) / 2.0
    rx = (ref_bbox[0] + ref_bbox[2]) / 2.0
    ry = (ref_bbox[1] + ref_bbox[3]) / 2.0

    # target → ref 방향 벡터 (target이 ref에서 멀어져야 하므로 반대 방향)
    ddx, ddy = tx - rx, ty - ry
    dist = math.hypot(ddx, ddy)

    # 이미 충분히 이격된 경우 이동 불필요
    if current_sep_mm >= required_sep_mm:
        return (0.0, 0.0)

    move_amount = required_sep_mm - current_sep_mm

    if dist < 1e-6:
        # 중심이 완전히 겹칠 때는 오른쪽으로 밀어냄
        return (round(move_amount, 2), 0.0)

    # 단위 방향 벡터 (ref → target 방향)
    ux, uy = ddx / dist, ddy / dist
    return (round(ux * move_amount, 2), round(uy * move_amount, 2))


def _pt(d: dict, *keys):
    for k in keys:
        p = d.get(k)
        if isinstance(p, dict) and "x" in p:
            try:
                return float(p["x"]), float(p["y"])
            except (TypeError, ValueError):
                pass
    return None


def _line_length_angle(e: dict):
    s = _pt(e, "start_point", "start")
    t = _pt(e, "end_point", "end")
    if not s or not t:
        return 0.0, None
    dx, dy = t[0] - s[0], t[1] - s[1]
    length = math.hypot(dx, dy)
    angle = math.degrees(math.atan2(dy, dx)) % 180.0
    return length, angle


def _wall_candidates_from_entities(
    candidates: list[dict] | None,
    unit_factor: float,
    *,
    source: str,
) -> list[dict]:
    walls: list[dict] = []
    for e in candidates or []:
        if str(e.get("raw_type") or e.get("type") or "").upper() not in _LINE_RAW:
            continue
        length, angle = _line_length_angle(e)
        if length * unit_factor < _WALL_MIN_LEN:
            continue
        if angle is None:
            continue
        if not (angle < _WALL_ANG_TOL or angle > 180 - _WALL_ANG_TOL
                or abs(angle - 90) < _WALL_ANG_TOL):
            continue
        bb = _bbox(e)
        if not bb:
            s = _pt(e, "start_point", "start")
            t = _pt(e, "end_point", "end")
            if s and t:
                bb = (min(s[0], t[0]), min(s[1], t[1]),
                      max(s[0], t[0]), max(s[1], t[1]))
        if bb:
            wall = {
                "handle": str(e.get("handle") or ""),
                "layer":  str(e.get("layer") or ""),
                "bbox":   {"x1": bb[0], "y1": bb[1], "x2": bb[2], "y2": bb[3]},
                "_wall_length": round(length * unit_factor, 2),
                "wall_source": source,
            }
            if isinstance(e.get("start"), dict):
                wall["start"] = e["start"]
            if isinstance(e.get("end"), dict):
                wall["end"] = e["end"]
            walls.append(wall)
    return walls


class ElecGeometryPreprocessor:
    """전기 도면 공간 전처리 — 전선관 이격 및 분전반 벽체 이격 계산."""

    def __init__(self, max_pairs: int = _MAX_PAIRS, unit_factor: float = 1.0):
        self.max_pairs = max_pairs
        self.unit_factor = unit_factor

    def process(
        self,
        elements: list[dict],
        unit_factor: float | None = None,
        *,
        arch_elements: list[dict] | None = None,
    ) -> dict:
        if unit_factor is not None:
            self.unit_factor = unit_factor
        uf = self.unit_factor

        blocks_all = [
            e for e in elements
            if str(e.get("raw_type") or e.get("type") or "").upper() in _BLOCK_RAW and e.get("handle")
        ]
        lines = [
            e for e in elements
            if str(e.get("raw_type") or e.get("type") or "").upper() in _LINE_RAW and e.get("handle")
        ]

        block_bboxes = [(b, _bbox(b)) for b in blocks_all]
        block_bboxes = [(b, bb) for b, bb in block_bboxes if bb is not None]
        filtered_reference_blocks = [
            b for b, bb in block_bboxes
            if _is_reference_or_background_block(b, bb, uf)
        ]
        blocks = [
            b for b, bb in block_bboxes
            if not _is_reference_or_background_block(b, bb, uf)
        ]

        proxy_walls = _wall_candidates_from_entities(lines, uf, source="proxy_wall")
        arch_walls = _wall_candidates_from_entities(arch_elements, uf, source="arch_reference")
        walls_for_clearance = arch_walls or proxy_walls
        conduit_clearances = self._calc_mep_clearances(blocks, uf)
        panel_clearances = self._calc_wall_clearances(blocks, walls_for_clearance, uf)

        _log.info(
            "[ElecGeometry] proxy_walls=%d arch_walls=%d elec_blocks=%d conduit_pairs=%d panel_pairs=%d unit_factor=%.4f",
            len(proxy_walls), len(arch_walls), len(blocks), len(conduit_clearances), len(panel_clearances), uf,
        )
        return {
            "proxy_walls":        proxy_walls,
            "arch_walls":         arch_walls,
            "conduit_clearances": conduit_clearances,
            "panel_clearances":   panel_clearances,
            "summary": {
                "proxy_walls":  len(proxy_walls),
                "arch_walls":   len(arch_walls),
                "elec_blocks":  len(blocks),
                "elec_lines":   len(lines),
                "filtered_reference_blocks": len(filtered_reference_blocks),
                "wall_clearance_source": "arch_reference" if arch_walls else "proxy_wall",
                "unit_factor": uf,
            },
        }

    def _extract_proxy_walls(self, lines: list[dict]) -> list[dict]:
        walls = []
        for e in lines:
            length, angle = _line_length_angle(e)
            if length < _WALL_MIN_LEN:
                continue
            if angle is None:
                continue
            if not (angle < _WALL_ANG_TOL or angle > 180 - _WALL_ANG_TOL
                    or abs(angle - 90) < _WALL_ANG_TOL):
                continue
            bb = _bbox(e)
            if not bb:
                s = _pt(e, "start_point", "start")
                t = _pt(e, "end_point", "end")
                if s and t:
                    bb = (min(s[0], t[0]), min(s[1], t[1]),
                          max(s[0], t[0]), max(s[1], t[1]))
            if bb:
                walls.append({
                    "handle": str(e.get("handle") or ""),
                    "layer":  str(e.get("layer") or ""),
                    "bbox":   {"x1": bb[0], "y1": bb[1], "x2": bb[2], "y2": bb[3]},
                    "_wall_length": round(length, 2),
                })
        return walls

    def _calc_mep_clearances(self, blocks: list[dict], unit_factor: float) -> list[dict]:
        pairs = []
        for i in range(len(blocks)):
            if len(pairs) >= self.max_pairs:
                break
            ba = _bbox(blocks[i])
            if not ba:
                continue
            for j in range(i + 1, len(blocks)):
                if len(pairs) >= self.max_pairs:
                    break
                bb = _bbox(blocks[j])
                if not bb:
                    continue
                sep = _sep(ba, bb)
                pairs.append({
                    "handle_a": str(blocks[i].get("handle") or ""),
                    "handle_b": str(blocks[j].get("handle") or ""),
                    "layer_a":  str(blocks[i].get("layer") or ""),
                    "layer_b":  str(blocks[j].get("layer") or ""),
                    "separation_drawing": round(sep, 2),
                    "separation_mm": round(sep * unit_factor, 2),
                    "overlapping": _is_overlap(ba, bb),
                    "target_bbox": {"x1": ba[0], "y1": ba[1], "x2": ba[2], "y2": ba[3]},
                    "ref_bbox": {"x1": bb[0], "y1": bb[1], "x2": bb[2], "y2": bb[3]},
                })
        return pairs

    def _calc_wall_clearances(self, blocks: list[dict], proxy_walls: list[dict], unit_factor: float) -> list[dict]:
        pairs = []
        for blk in blocks:
            if len(pairs) >= self.max_pairs:
                break
            bb = _bbox(blk)
            if not bb:
                continue
            nearest_sep = None
            nearest_wall = None
            for wall in proxy_walls:
                wb = (wall["bbox"]["x1"], wall["bbox"]["y1"],
                      wall["bbox"]["x2"], wall["bbox"]["y2"])
                s = _sep(bb, wb)
                if nearest_sep is None or s < nearest_sep:
                    nearest_sep = s
                    nearest_wall = wall
            if nearest_sep is not None and nearest_wall:
                pairs.append({
                    "mep_handle": str(blk.get("handle") or ""),
                    "wall_handle": nearest_wall["handle"],
                    "wall_source": nearest_wall.get("wall_source") or "proxy_wall",
                    "separation_drawing": round(nearest_sep, 2),
                    "separation_mm": round(nearest_sep * unit_factor, 2),
                    "wall_length": nearest_wall["_wall_length"],
                    "target_bbox": {"x1": bb[0], "y1": bb[1], "x2": bb[2], "y2": bb[3]},
                    "ref_bbox": nearest_wall["bbox"],
                    "note": "전기기기→벽체 최근접 이격",
                })
        return pairs
