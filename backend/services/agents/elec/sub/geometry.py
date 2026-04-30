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
from typing import Any

_log = logging.getLogger(__name__)

_MAX_PAIRS    = 50
_WALL_MIN_LEN = 800.0
_WALL_ANG_TOL = 8.0

_BLOCK_RAW = frozenset({"INSERT", "BLOCK"})
_LINE_RAW  = frozenset({"LINE"})


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


class ElecGeometryPreprocessor:
    """전기 도면 공간 전처리 — 전선관 이격 및 분전반 벽체 이격 계산."""

    def process(self, elements: list[dict]) -> dict:
        blocks = [e for e in elements if str(e.get("raw_type") or "").upper() in _BLOCK_RAW]
        lines  = [e for e in elements if str(e.get("raw_type") or "").upper() in _LINE_RAW]

        proxy_walls = self._extract_proxy_walls(lines)
        conduit_clearances = self._calc_mep_clearances(blocks)
        panel_clearances = self._calc_wall_clearances(blocks, proxy_walls)

        _log.info(
            "[ElecGeometry] proxy_walls=%d conduit_pairs=%d panel_pairs=%d",
            len(proxy_walls), len(conduit_clearances), len(panel_clearances),
        )
        return {
            "proxy_walls":        proxy_walls,
            "conduit_clearances": conduit_clearances,
            "panel_clearances":   panel_clearances,
            "summary": {
                "proxy_walls":  len(proxy_walls),
                "elec_blocks":  len(blocks),
                "elec_lines":   len(lines),
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

    def _calc_mep_clearances(self, blocks: list[dict]) -> list[dict]:
        pairs = []
        for i in range(len(blocks)):
            if len(pairs) >= _MAX_PAIRS:
                break
            ba = _bbox(blocks[i])
            if not ba:
                continue
            for j in range(i + 1, len(blocks)):
                if len(pairs) >= _MAX_PAIRS:
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
                    "overlapping": _is_overlap(ba, bb),
                })
        return pairs

    def _calc_wall_clearances(self, blocks: list[dict], proxy_walls: list[dict]) -> list[dict]:
        pairs = []
        for blk in blocks:
            if len(pairs) >= _MAX_PAIRS:
                break
            bb = _bbox(blk)
            if not bb:
                continue
            nearest_sep = None
            nearest_wall_h = None
            nearest_wall_len = None
            for wall in proxy_walls:
                wb = (wall["bbox"]["x1"], wall["bbox"]["y1"],
                      wall["bbox"]["x2"], wall["bbox"]["y2"])
                s = _sep(bb, wb)
                if nearest_sep is None or s < nearest_sep:
                    nearest_sep = s
                    nearest_wall_h = wall["handle"]
                    nearest_wall_len = wall["_wall_length"]
            if nearest_sep is not None:
                pairs.append({
                    "mep_handle": str(blk.get("handle") or ""),
                    "wall_handle": nearest_wall_h,
                    "separation_drawing": round(nearest_sep, 2),
                    "wall_length": nearest_wall_len,
                    "note": "전기기기→벽체 최근접 이격",
                })
        return pairs
