"""
File    : backend/services/agents/piping/sub/geometry.py
Author  : 송주엽
Create  : 2026-04-24
Description : arch 레이어 없이 MEP 엔티티만으로 공간 분석을 수행합니다.

  1. proxy_wall 추출 — 긴 축방향 LINE(길이 ≥ WALL_MIN_LEN, 각도 ≈ 0°/90°)을
     건축 벽체 후보로 식별하고, arch_elements 대용으로 활용합니다.
  2. MEP-MEP 이격 — 블록 간 bbox 최소 이격을 계산합니다.
  3. pipe-to-wall 이격 — 각 MEP 블록과 가장 가까운 proxy_wall 간 이격을 계산합니다.

출력:
  proxy_walls    : [{handle, layer, bbox, _wall_length}, ...]
  mep_clearances : [{handle_a, handle_b, layer_a, layer_b,
                     separation_drawing, separation_mm, overlapping}, ...]
  wall_clearances: [{mep_handle, wall_handle, separation_drawing, separation_mm,
                     wall_length, note}, ...]
  summary        : {proxy_walls, mep_blocks, mep_lines, unit_factor}

Modification History :
    - 2026-04-29 (송주엽) : unit_factor 지원 추가 (separation_mm 필드 출력)
"""
from __future__ import annotations

import logging
import math
from typing import Any

_log = logging.getLogger(__name__)

_MAX_PAIRS    = 50     # 이격 쌍 상한 (토큰 절감)
_WALL_MIN_LEN = 800.0  # 벽체 후보 최소 길이 (도면 단위, mm 가정)
_WALL_ANG_TOL = 8.0    # 축방향 허용 각도 오차 (degrees)

_BLOCK_RAW = frozenset({"INSERT", "BLOCK"})
_LINE_RAW  = frozenset({"LINE"})


# ── 기하 헬퍼 ────────────────────────────────────────────────────────────────

def _bbox(e: dict) -> tuple[float, float, float, float] | None:
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
    """축맞춤 bbox 최소 이격. 겹침이면 0."""
    ax1, ay1 = min(a[0], a[2]), min(a[1], a[3])
    ax2, ay2 = max(a[0], a[2]), max(a[1], a[3])
    bx1, by1 = min(b[0], b[2]), min(b[1], b[3])
    bx2, by2 = max(b[0], b[2]), max(b[1], b[3])
    if ax2 < bx1:
        return bx1 - ax2
    if bx2 < ax1:
        return ax1 - bx2
    if ay2 < by1:
        return by1 - ay2
    if by2 < ay1:
        return ay1 - by2
    return 0.0


def _line_len_angle(e: dict) -> tuple[float, float]:
    """LINE 엔티티의 (길이, 각도°). 좌표 없으면 (0, 0)."""
    s, en = e.get("start"), e.get("end")
    if isinstance(s, dict) and isinstance(en, dict):
        try:
            dx = float(en["x"]) - float(s["x"])
            dy = float(en["y"]) - float(s["y"])
            ang = math.degrees(math.atan2(dy, dx)) % 180.0
            return math.hypot(dx, dy), ang
        except (TypeError, ValueError, KeyError):
            pass
    return 0.0, 0.0


def _is_axis_aligned(angle_deg: float) -> bool:
    """수평(~0°) 또는 수직(~90°) 판정."""
    return (
        angle_deg < _WALL_ANG_TOL
        or angle_deg > 180.0 - _WALL_ANG_TOL
        or abs(angle_deg - 90.0) < _WALL_ANG_TOL
    )


# ── 메인 클래스 ───────────────────────────────────────────────────────────────

class GeometryPreprocessor:
    """
    arch 레이어 없이 MEP 엔티티만으로 공간 분석을 수행합니다.
    proxy_wall 결과를 compliance의 arch_elements 대용으로 주입하세요.

    Args:
        unit_factor: 도면 좌표 단위 → mm 변환 계수 (mm=1.0, inch=25.4, m=1000.0)
                     workflow_handler에서 drawing_data["unit_to_mm_factor"] 를 주입하세요.
    """

    def __init__(self, max_pairs: int = _MAX_PAIRS, unit_factor: float = 1.0):
        self.max_pairs   = max_pairs
        self.unit_factor = unit_factor

    def process(self, elements: list[dict], unit_factor: float | None = None) -> dict[str, Any]:
        """
        Args:
            unit_factor: process() 호출 시 덮어쓸 단위 계수 (None이면 __init__ 값 사용)
        """
        if unit_factor is not None:
            self.unit_factor = unit_factor
        uf = self.unit_factor

        blocks = [
            e for e in elements
            if str(e.get("raw_type") or "").upper() in _BLOCK_RAW and e.get("handle")
        ]
        lines = [
            e for e in elements
            if str(e.get("raw_type") or "").upper() in _LINE_RAW and e.get("handle")
        ]

        # ── 1. proxy_wall 추출 ───────────────────────────────────────
        proxy_walls: list[dict] = []
        for e in lines:
            length, angle = _line_len_angle(e)
            if length >= _WALL_MIN_LEN and _is_axis_aligned(angle):
                proxy_walls.append({
                    "handle":       e["handle"],
                    "layer":        e.get("layer", ""),
                    "bbox":         e.get("bbox"),
                    "_wall_length": round(length, 1),
                    "_wall_angle":  round(angle, 1),
                })

        # ── 2. MEP 블록 간 bbox 이격 ─────────────────────────────────
        mep_clearances: list[dict] = []
        bboxes = [(b, _bbox(b)) for b in blocks]
        bboxes = [(b, bb) for b, bb in bboxes if bb is not None]
        _total_pairs = len(bboxes) * (len(bboxes) - 1) // 2 if len(bboxes) > 1 else 0
        if _total_pairs > self.max_pairs:
            _log.warning(
                "[GeoPreprocess] MEP 블록 이격 쌍 %d개 > 상한 %d → 첫 %d쌍만 계산 (대형 도면)",
                _total_pairs, self.max_pairs, self.max_pairs,
            )

        for i, (ba, bba) in enumerate(bboxes):
            if len(mep_clearances) >= self.max_pairs:
                break
            for bb2, bbb in bboxes[i + 1:]:
                if len(mep_clearances) >= self.max_pairs:
                    break
                s = _sep(bba, bbb)
                mep_clearances.append({
                    "handle_a":           ba["handle"],
                    "handle_b":           bb2["handle"],
                    "layer_a":            ba.get("layer", ""),
                    "layer_b":            bb2.get("layer", ""),
                    "separation_drawing": round(s, 2),
                    "separation_mm":      round(s * uf, 2),  # mm 정규화
                    "overlapping":        s == 0.0,
                })

        # ── 3. proxy_wall ↔ MEP 블록 이격 ────────────────────────────
        wall_clearances: list[dict] = []
        wall_bboxes = [(w, _bbox_from_wall(w)) for w in proxy_walls]
        wall_bboxes = [(w, wb) for w, wb in wall_bboxes if wb is not None]

        for b, bba in bboxes:
            if len(wall_clearances) >= self.max_pairs:
                break
            if not wall_bboxes:
                break
            closest_wall, closest_sep = None, math.inf
            for w, wbb in wall_bboxes:
                s = _sep(bba, wbb)
                if s < closest_sep:
                    closest_sep, closest_wall = s, w
            if closest_wall is not None:
                wall_clearances.append({
                    "mep_handle":         b["handle"],
                    "wall_handle":        closest_wall["handle"],
                    "separation_drawing": round(closest_sep, 2),
                    "separation_mm":      round(closest_sep * uf, 2),  # mm 정규화
                    "wall_length":        closest_wall.get("_wall_length", 0),
                    "note":               "proxy_wall (arch 미분리 도면 — 긴 축방향 선분 추정)",
                })

        _log.info(
            "[GeoPreprocess] proxy_walls=%d mep_blocks=%d mep_clearances=%d wall_clearances=%d unit_factor=%.4f",
            len(proxy_walls), len(blocks), len(mep_clearances), len(wall_clearances), uf,
        )
        return {
            "proxy_walls":     proxy_walls,
            "mep_clearances":  mep_clearances,
            "wall_clearances": wall_clearances,
            "summary": {
                "proxy_walls": len(proxy_walls),
                "mep_blocks":  len(blocks),
                "mep_lines":   len(lines),
                "unit_factor": uf,
            },
        }


def _bbox_from_wall(wall: dict) -> tuple[float, float, float, float] | None:
    """proxy_wall의 bbox — 저장된 bbox 우선, 없으면 start/end로 구성."""
    stored = _bbox(wall)
    if stored:
        return stored
    s = wall.get("start")
    e = wall.get("end")
    if isinstance(s, dict) and isinstance(e, dict):
        try:
            x1, y1 = float(s["x"]), float(s["y"])
            x2, y2 = float(e["x"]), float(e["y"])
            pad = 10.0
            return min(x1, x2) - pad, min(y1, y2) - pad, max(x1, x2) + pad, max(y1, y2) + pad
        except (TypeError, ValueError, KeyError):
            pass
    return None
