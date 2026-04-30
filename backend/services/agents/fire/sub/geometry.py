"""
File    : backend/services/agents/fire/sub/geometry.py
Author  : 김민정
Create  : 2026-04-24
Description : arch 레이어 없이 소방 엔티티만으로 공간 분석을 수행합니다.
              배관 GeometryPreprocessor 패턴을 소방 도메인에 적용.

  1. proxy_wall 추출 — 긴 축방향 LINE을 벽체 후보로 식별
  2. head_clearances  — 스프링클러 헤드/감지기 간 bbox 이격 계산
  3. head_wall_clearances — 헤드/감지기와 proxy_wall 간 이격 계산
  4. coverage_analysis   — 헤드별 추정 커버 반경 및 인접 헤드 중복 여부

출력:
  proxy_walls         : [{handle, layer, bbox, _wall_length}, ...]
  head_clearances     : [{handle_a, handle_b, separation_drawing, overlapping}, ...]
  head_wall_clearances: [{head_handle, wall_handle, separation_drawing, wall_length}, ...]
  coverage_analysis   : [{head_handle, estimated_radius_mm, overlapping_heads: [str, ...]}, ...]
  summary             : {proxy_walls, fire_blocks, fire_lines}
"""
from __future__ import annotations

import logging
import math
from typing import Any

_log = logging.getLogger(__name__)

_MAX_PAIRS       = 50
_WALL_MIN_LEN    = 800.0
_WALL_ANG_TOL    = 8.0
_SPK_RADIUS_MM   = 2300.0   # NFSC 103 기준 헤드 표준 커버 반경

_BLOCK_RAW = frozenset({"INSERT", "BLOCK"})
_LINE_RAW  = frozenset({"LINE"})
_HEAD_PREFIXES = ("SPK", "HYD", "FDH", "SMK", "HTD")


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


def _pt(d: dict, *keys):
    for k in keys:
        p = d.get(k)
        if isinstance(p, dict) and "x" in p:
            try:
                return float(p["x"]), float(p["y"])
            except (TypeError, ValueError):
                pass
    return None


def _center(e: dict):
    bb = _bbox(e)
    if bb:
        return (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
    return _pt(e, "center", "position", "insert_point")


def _line_length_angle(e: dict):
    s = _pt(e, "start_point", "start")
    t = _pt(e, "end_point", "end")
    if not s or not t:
        return 0.0, None
    dx, dy = t[0] - s[0], t[1] - s[1]
    length = math.hypot(dx, dy)
    angle = math.degrees(math.atan2(dy, dx)) % 180.0
    return length, angle


def _is_head(e: dict) -> bool:
    bn = str(e.get("block_name") or e.get("standard_name") or "").upper()
    return any(bn.startswith(p) for p in _HEAD_PREFIXES)


class FireGeometryPreprocessor:
    """소방 도면 공간 전처리 — 헤드 이격 및 커버리지 분석."""

    def process(self, elements: list[dict]) -> dict:
        blocks = [e for e in elements if str(e.get("raw_type") or "").upper() in _BLOCK_RAW]
        lines  = [e for e in elements if str(e.get("raw_type") or "").upper() in _LINE_RAW]
        heads  = [b for b in blocks if _is_head(b)]

        proxy_walls = self._extract_proxy_walls(lines)
        head_clearances = self._calc_head_clearances(heads)
        head_wall_clearances = self._calc_wall_clearances(heads, proxy_walls)
        coverage_analysis = self._calc_coverage(heads)

        _log.info(
            "[FireGeometry] proxy_walls=%d heads=%d head_pairs=%d",
            len(proxy_walls), len(heads), len(head_clearances),
        )
        return {
            "proxy_walls":          proxy_walls,
            "head_clearances":      head_clearances,
            "head_wall_clearances": head_wall_clearances,
            "coverage_analysis":    coverage_analysis,
            "summary": {
                "proxy_walls": len(proxy_walls),
                "fire_blocks": len(blocks),
                "fire_lines":  len(lines),
            },
        }

    def _extract_proxy_walls(self, lines: list[dict]) -> list[dict]:
        walls = []
        for e in lines:
            length, angle = _line_length_angle(e)
            if length < _WALL_MIN_LEN or angle is None:
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

    def _calc_head_clearances(self, heads: list[dict]) -> list[dict]:
        pairs = []
        for i in range(len(heads)):
            if len(pairs) >= _MAX_PAIRS:
                break
            ba = _bbox(heads[i])
            if not ba:
                continue
            for j in range(i + 1, len(heads)):
                if len(pairs) >= _MAX_PAIRS:
                    break
                bb = _bbox(heads[j])
                if not bb:
                    continue
                sep = _sep(ba, bb)
                pairs.append({
                    "handle_a": str(heads[i].get("handle") or ""),
                    "handle_b": str(heads[j].get("handle") or ""),
                    "separation_drawing": round(sep, 2),
                    "overlapping": sep == 0.0,
                })
        return pairs

    def _calc_wall_clearances(self, heads: list[dict], walls: list[dict]) -> list[dict]:
        pairs = []
        for head in heads:
            if len(pairs) >= _MAX_PAIRS:
                break
            hb = _bbox(head)
            if not hb:
                continue
            nearest_sep = None
            nearest_wall = None
            for wall in walls:
                wb = (wall["bbox"]["x1"], wall["bbox"]["y1"],
                      wall["bbox"]["x2"], wall["bbox"]["y2"])
                s = _sep(hb, wb)
                if nearest_sep is None or s < nearest_sep:
                    nearest_sep = s
                    nearest_wall = wall
            if nearest_sep is not None and nearest_wall:
                pairs.append({
                    "head_handle": str(head.get("handle") or ""),
                    "wall_handle": nearest_wall["handle"],
                    "separation_drawing": round(nearest_sep, 2),
                    "wall_length": nearest_wall["_wall_length"],
                })
        return pairs

    def _calc_coverage(self, heads: list[dict]) -> list[dict]:
        result = []
        for head in heads:
            hc = _center(head)
            if not hc:
                continue
            overlapping = []
            for other in heads:
                if other is head:
                    continue
                oc = _center(other)
                if not oc:
                    continue
                dist = math.hypot(hc[0] - oc[0], hc[1] - oc[1])
                if dist < _SPK_RADIUS_MM * 2:
                    overlapping.append(str(other.get("handle") or ""))
            result.append({
                "head_handle": str(head.get("handle") or ""),
                "estimated_radius_mm": _SPK_RADIUS_MM,
                "overlapping_heads": overlapping,
            })
        return result
