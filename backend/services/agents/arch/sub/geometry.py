"""
File    : backend/services/agents/arch/sub/geometry.py
Author  : 김다빈
Create  : 2026-04-24
Description : 건축 도면 엔티티의 공간 전처리를 수행합니다.
              배관 GeometryPreprocessor 패턴을 건축 도메인에 적용.

  1. wall_clearances  — 벽체 간 이격 계산
  2. opening_analysis — 문·창문 치수 및 유효 폭
  3. corridor_widths  — 복도 구간별 폭
  4. ceiling_heights  — TEXT 엔티티에서 층고 추정

출력:
  wall_clearances  : [{wall_a, wall_b, separation_drawing, overlapping}, ...]
  opening_analysis : [{handle, type, width_mm, height_mm, effective_width_mm}, ...]
  corridor_widths  : [{corridor_id, width_mm, handles}, ...]
  ceiling_heights  : [{handle, estimated_height_mm, source_text}, ...]
  summary          : {walls, openings, corridors}
"""
from __future__ import annotations

import logging
import math
import re
from typing import Any

_log = logging.getLogger(__name__)

_MAX_PAIRS   = 50
_MIN_OPENING = 300.0   # 최소 문 폭 (mm)
_MAX_OPENING = 3000.0  # 최대 문 폭 (mm)

_WALL_TYPES    = frozenset({"LINE", "POLYLINE", "LWPOLYLINE"})
_BLOCK_TYPES   = frozenset({"INSERT", "BLOCK"})
_TEXT_TYPES    = frozenset({"TEXT", "MTEXT"})
_DOOR_PREFIXES = ("DOOR", "D", "문", "도어")
_WIN_PREFIXES  = ("WIN", "W", "창문", "창호")
_COR_PREFIXES  = ("COR", "HALL", "복도")

_HEIGHT_RE = re.compile(r"(\d{2,4}(?:\.\d+)?)\s*(?:mm|M|m|층고)?", re.IGNORECASE)


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


def _bbox_dims(bb: tuple) -> tuple[float, float]:
    w = abs(bb[2] - bb[0])
    h = abs(bb[3] - bb[1])
    return w, h


class ArchGeometryPreprocessor:
    """건축 도면 공간 전처리 — 벽체 이격, 개구부 분석, 복도 폭."""

    def process(self, elements: list[dict]) -> dict:
        walls    = [e for e in elements if str(e.get("raw_type") or "").upper() in _WALL_TYPES]
        blocks   = [e for e in elements if str(e.get("raw_type") or "").upper() in _BLOCK_TYPES]
        texts    = [e for e in elements if str(e.get("raw_type") or "").upper() in _TEXT_TYPES]

        wall_clearances  = self._calc_wall_clearances(walls)
        opening_analysis = self._analyze_openings(blocks)
        corridor_widths  = self._calc_corridor_widths(blocks, walls)
        ceiling_heights  = self._extract_ceiling_heights(texts)

        _log.info(
            "[ArchGeometry] walls=%d openings=%d corridors=%d heights=%d",
            len(walls), len(opening_analysis), len(corridor_widths), len(ceiling_heights),
        )
        return {
            "wall_clearances":  wall_clearances,
            "opening_analysis": opening_analysis,
            "corridor_widths":  corridor_widths,
            "ceiling_heights":  ceiling_heights,
            "summary": {
                "walls":    len(walls),
                "openings": len(opening_analysis),
                "corridors": len(corridor_widths),
            },
        }

    def _calc_wall_clearances(self, walls: list[dict]) -> list[dict]:
        pairs = []
        for i in range(len(walls)):
            if len(pairs) >= _MAX_PAIRS:
                break
            ba = _bbox(walls[i])
            if not ba:
                continue
            for j in range(i + 1, len(walls)):
                if len(pairs) >= _MAX_PAIRS:
                    break
                bb = _bbox(walls[j])
                if not bb:
                    continue
                sep = _sep(ba, bb)
                pairs.append({
                    "wall_a": str(walls[i].get("handle") or ""),
                    "wall_b": str(walls[j].get("handle") or ""),
                    "separation_drawing": round(sep, 2),
                    "overlapping": sep == 0.0,
                })
        return pairs

    def _analyze_openings(self, blocks: list[dict]) -> list[dict]:
        result = []
        for blk in blocks:
            bn = str(blk.get("block_name") or blk.get("standard_name") or "").upper()
            if any(bn.startswith(p.upper()) for p in _DOOR_PREFIXES):
                opening_type = "DOOR"
            elif any(bn.startswith(p.upper()) for p in _WIN_PREFIXES):
                opening_type = "WINDOW"
            else:
                continue
            bb = _bbox(blk)
            if not bb:
                continue
            w, h = _bbox_dims(bb)
            width_mm  = max(w, h) if max(w, h) > 0 else 0.0
            height_mm = min(w, h) if min(w, h) > 0 else 0.0
            # 유효 폭: 문틀 두께 20mm 제외 (단순 추정)
            effective_width = max(0.0, width_mm - 20.0)
            if _MIN_OPENING <= width_mm <= _MAX_OPENING:
                result.append({
                    "handle":          str(blk.get("handle") or ""),
                    "type":            opening_type,
                    "width_mm":        round(width_mm, 1),
                    "height_mm":       round(height_mm, 1),
                    "effective_width_mm": round(effective_width, 1),
                })
        return result

    def _calc_corridor_widths(self, blocks: list[dict], walls: list[dict]) -> list[dict]:
        corridors = [b for b in blocks
                     if any(str(b.get("block_name") or b.get("standard_name") or "").upper().startswith(p.upper())
                            for p in _COR_PREFIXES)]
        result = []
        for cid, cor in enumerate(corridors):
            cb = _bbox(cor)
            if not cb:
                continue
            cw, ch = _bbox_dims(cb)
            width_mm = min(cw, ch) if min(cw, ch) > 0 else max(cw, ch)
            nearby = []
            for wall in walls:
                wb = _bbox(wall)
                if wb and _sep(cb, wb) < 500.0:
                    nearby.append(str(wall.get("handle") or ""))
            result.append({
                "corridor_id": cid,
                "width_mm":    round(width_mm, 1),
                "handles":     nearby,
            })
        return result

    def _extract_ceiling_heights(self, texts: list[dict]) -> list[dict]:
        result = []
        for txt in texts:
            content = str(txt.get("text") or txt.get("content") or "")
            m = _HEIGHT_RE.search(content)
            if not m:
                continue
            val = float(m.group(1))
            if 2000.0 <= val <= 5000.0:
                result.append({
                    "handle":             str(txt.get("handle") or ""),
                    "estimated_height_mm": val,
                    "source_text":        content.strip()[:50],
                })
        return result
