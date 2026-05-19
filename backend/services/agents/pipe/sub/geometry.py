"""
File    : backend/services/agents/piping/sub/geometry.py
Author  : 송주엽
Create  : 2026-04-24
Description : arch 레이어 없이 MEP 엔티티만으로 공간 분석을 수행합니다.

  1. 실제 arch_elements가 있으면 긴 축방향 LINE을 벽체 후보로 사용합니다.
     없으면 MEP 선분에서 proxy_wall 후보를 추정해 대용합니다.
  2. MEP-MEP 이격 — 블록 간 bbox 최소 이격을 계산합니다.
  3. pipe-to-wall 이격 — 각 MEP 블록과 가장 가까운 arch/proxy wall 간 이격을 계산합니다.

출력:
  proxy_walls    : [{handle, layer, bbox, _wall_length}, ...]
  mep_clearances : [{handle_a, handle_b, layer_a, layer_b,
                     separation_drawing, separation_mm, overlapping}, ...]
  wall_clearances: [{mep_handle, wall_handle, wall_source, separation_drawing, separation_mm,
                     wall_length, note}, ...]
  summary        : {proxy_walls, arch_wall_candidates, mep_blocks, mep_lines, unit_factor}

Modification History :
    - 2026-04-29 (송주엽) : unit_factor 지원 추가 (separation_mm 필드 출력)
"""
from __future__ import annotations

import logging
import math
import re
from typing import Any

_log = logging.getLogger(__name__)

_MAX_PAIRS    = 50     # 이격 쌍 상한 (토큰 절감)
_WALL_MIN_LEN = 800.0  # 벽체 후보 최소 길이 (도면 단위, mm 가정)
_WALL_ANG_TOL = 8.0    # 축방향 허용 각도 오차 (degrees)

_BLOCK_RAW = frozenset({"INSERT", "BLOCK"})
_LINE_RAW  = frozenset({"LINE"})
_REFERENCE_BLOCK_MAX_DIM_MM = 5000.0
_GENERIC_LAYER_RE = re.compile(r"^(?:0|L\d+|LAYER\d*|\d+)$", re.IGNORECASE)
_PIPE_BLOCK_TEXT_RE = re.compile(
    r"GAS|PIPE|PIPING|VALVE|METER|PUMP|TANK|DRAIN|SANIT|WATER|"
    r"가스|배관|밸브|계량기|펌프|탱크|배수|위생|급수|급탕",
    re.IGNORECASE,
)


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


def _bbox_max_dim_mm(bb: tuple[float, float, float, float], unit_factor: float) -> float:
    return max(abs(bb[2] - bb[0]), abs(bb[3] - bb[1])) * unit_factor


def _has_explicit_block_pipe_evidence(e: dict) -> bool:
    attrs = e.get("attributes") or e.get("properties") or {}
    text = " ".join(
        str(v or "")
        for v in (
            e.get("name"),
            e.get("block_name"),
            e.get("effective_name"),
            e.get("layer"),
            e.get("material"),
            attrs.get("TAG_NAME"),
            attrs.get("SIZE"),
            attrs.get("DIAMETER"),
            attrs.get("MATERIAL"),
            attrs.get("PRESSURE"),
            attrs.get("SLOPE"),
        )
    )
    material = str(e.get("material") or "").upper()
    return bool(
        e.get("diameter_mm")
        or e.get("pressure_mpa")
        or e.get("slope_pct")
        or attrs.get("SIZE")
        or attrs.get("DIAMETER")
        or attrs.get("MATERIAL")
        or attrs.get("TAG_NAME")
        or attrs.get("PRESSURE")
        or attrs.get("SLOPE")
        or material not in {"", "UNKNOWN", "NONE"}
        or _PIPE_BLOCK_TEXT_RE.search(text)
    )


def _has_block_pipe_evidence(e: dict) -> bool:
    return bool(
        str(e.get("layer_role") or "").lower() == "mep"
        or e.get("flag_for_piping_agent")
        or _has_explicit_block_pipe_evidence(e)
    )


def _is_reference_or_background_block(e: dict, bb: tuple[float, float, float, float], unit_factor: float) -> bool:
    """Large generic blocks are usually xref/background containers, not MEP equipment."""
    max_dim_mm = _bbox_max_dim_mm(bb, unit_factor)
    if max_dim_mm <= _REFERENCE_BLOCK_MAX_DIM_MM:
        return False
    # A large block promoted only by "near pipe annotation" is often a whole
    # architectural/background container. Keep large blocks only when the block
    # itself carries pipe evidence such as name, size, material, or attributes.
    if _has_explicit_block_pipe_evidence(e):
        return False
    layer = str(e.get("layer") or "").strip()
    block_name = str(e.get("block_name") or e.get("effective_name") or "").strip()
    return bool(_GENERIC_LAYER_RE.match(layer) or block_name)


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


def _has_positive_overlap(a: tuple, b: tuple, tol: float = 1e-6) -> bool:
    """True only when two bboxes overlap by area, not merely touch edges."""
    ax1, ay1 = min(a[0], a[2]), min(a[1], a[3])
    ax2, ay2 = max(a[0], a[2]), max(a[1], a[3])
    bx1, by1 = min(b[0], b[2]), min(b[1], b[3])
    bx2, by2 = max(b[0], b[2]), max(b[1], b[3])
    return (min(ax2, bx2) - max(ax1, bx1)) > tol and (min(ay2, by2) - max(ay1, by1)) > tol


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

def _wall_candidates_from_entities(
    candidates: list[dict] | None,
    unit_factor: float,
    *,
    source: str,
) -> list[dict]:
    walls: list[dict] = []
    for e in candidates or []:
        if str(e.get("raw_type") or "").upper() not in _LINE_RAW:
            continue
        if not e.get("handle"):
            continue
        length, angle = _line_len_angle(e)
        if length * unit_factor < _WALL_MIN_LEN or not _is_axis_aligned(angle):
            continue
        wall = {
            "handle":       e["handle"],
            "layer":        e.get("layer", ""),
            "bbox":         e.get("bbox"),
            "_wall_length": round(length * unit_factor, 1),
            "_wall_angle":  round(angle, 1),
            "wall_source":  source,
        }
        if isinstance(e.get("start"), dict):
            wall["start"] = e["start"]
        if isinstance(e.get("end"), dict):
            wall["end"] = e["end"]
        walls.append(wall)
    return walls


class GeometryPreprocessor:
    """
    arch 레이어 없이 MEP 엔티티만으로 공간 분석을 수행합니다.
    실제 arch_elements가 있으면 벽체 후보로 사용하고, 없으면 proxy_wall을 대용합니다.

    Args:
        unit_factor: 도면 좌표 단위 → mm 변환 계수 (mm=1.0, inch=25.4, m=1000.0)
                     workflow_handler에서 drawing_data["unit_to_mm_factor"] 를 주입하세요.
    """

    def __init__(self, max_pairs: int = _MAX_PAIRS, unit_factor: float = 1.0):
        self.max_pairs   = max_pairs
        self.unit_factor = unit_factor

    def process(
        self,
        elements: list[dict],
        unit_factor: float | None = None,
        *,
        arch_elements: list[dict] | None = None,
    ) -> dict[str, Any]:
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
        proxy_walls = _wall_candidates_from_entities(lines, uf, source="proxy_wall")
        arch_walls = _wall_candidates_from_entities(arch_elements, uf, source="arch_reference")

        # ── 2. MEP 블록 간 bbox 이격 ─────────────────────────────────
        mep_clearances: list[dict] = []
        bboxes_all = [(b, _bbox(b)) for b in blocks]
        bboxes_all = [(b, bb) for b, bb in bboxes_all if bb is not None]
        filtered_reference_blocks = [
            b for b, bb in bboxes_all
            if _is_reference_or_background_block(b, bb, uf)
        ]
        bboxes = [
            (b, bb) for b, bb in bboxes_all
            if not _is_reference_or_background_block(b, bb, uf)
        ]
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
                    "overlapping":        _has_positive_overlap(bba, bbb),
                })

        # ── 3. arch/proxy wall ↔ MEP 블록 이격 ───────────────────────
        wall_clearances: list[dict] = []
        clearance_walls = arch_walls or proxy_walls
        wall_bboxes = [(w, _bbox_from_wall(w)) for w in clearance_walls]
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
                wall_source = str(closest_wall.get("wall_source") or "proxy_wall")
                wall_clearances.append({
                    "mep_handle":         b["handle"],
                    "wall_handle":        closest_wall["handle"],
                    "wall_source":        wall_source,
                    "separation_drawing": round(closest_sep, 2),
                    "separation_mm":      round(closest_sep * uf, 2),  # mm 정규화
                    "wall_length":        closest_wall.get("_wall_length", 0),
                    "note": (
                        "arch_reference wall candidate"
                        if wall_source == "arch_reference"
                        else "proxy_wall (arch not split; long axis-aligned line candidate)"
                    ),
                })

        _log.info(
            "[GeoPreprocess] proxy_walls=%d arch_walls=%d mep_blocks=%d mep_clearances=%d wall_clearances=%d unit_factor=%.4f",
            len(proxy_walls), len(arch_walls), len(blocks), len(mep_clearances), len(wall_clearances), uf,
        )
        return {
            "proxy_walls":     proxy_walls,
            "mep_clearances":  mep_clearances,
            "wall_clearances": wall_clearances,
            "summary": {
                "proxy_walls": len(proxy_walls),
                "arch_wall_candidates": len(arch_walls),
                "wall_clearance_source": "arch_reference" if arch_walls else "proxy_wall",
                "mep_blocks":  len(bboxes),
                "raw_mep_blocks": len(blocks),
                "filtered_reference_blocks": len(filtered_reference_blocks),
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
