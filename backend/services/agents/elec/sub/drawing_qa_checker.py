"""
File    : backend/services/agents/elec/sub/drawing_qa_checker.py
Author  : 김지우
Create  : 2026-05-04
Description :
  LLM 없이 CAD 도면의 기초 품질(Drawing QA)을 검사하는 모듈.
  pipe/sub/drawing_qa_checker.py의 전기 도메인 버전.

  ★ 규칙:
    - AI(LLM)가 판단하기 전, 도면 자체가 분석 가능한 상태인지 확인
    - "없음"이 아니라 "있고 이상함"만 보고

  검사 항목:
    1. 도면 단위 이상 (unknown / 비표준)
    2. 레이어 집중도 (전 객체가 1~2개 레이어에만 몰린 경우)
    3. 전기 블록 없음 경고 (전기 도면인데 INSERT/BLOCK 0개)
    4. 전선(LINE) 없음 경고
    5. 분전반(Panel) 블록 없음 경고
    6. 도면 범위 이상 (bbox가 극단적으로 큰 경우 — 오삽입 의심)
    7. 동일 위치 블록 중복 삽입
"""
from __future__ import annotations

import logging
import math
from collections import Counter
from typing import Any

_log = logging.getLogger(__name__)

# ── 상수 ──────────────────────────────────────────────────────────────────────
_LAYER_CONCENTRATION_THRESHOLD = 0.90  # 90% 이상이 1개 레이어에 몰리면 경고
_DUPLICATE_BLOCK_TOL_MM = 10.0        # 이 거리 이내 동일 블록명 = 중복 삽입 의심
_BBOX_EXTREME_RATIO = 100_000         # 도면 범위 비율 이 값 이상이면 비정상
_PANEL_PREFIXES = ("PNL", "MCC", "CB", "MCB", "MCCB", "PANEL", "분전", "배전")


def run_drawing_qa(
    drawing_data: dict[str, Any],
    unit_factor: float = 1.0,
) -> list[dict]:
    """
    전기 도면 품질 검사를 실행한다.

    Args:
        drawing_data: CadDrawingData JSON
        unit_factor : drawing_unit → mm 변환 계수

    Returns:
        QA 이슈 리스트 [{level, check_id, message, detail}, ...]
        level: "error" | "warning" | "info"
    """
    issues: list[dict] = []
    entities: list[dict] = [
        e for e in (drawing_data.get("entities") or [])
        if isinstance(e, dict)
    ]

    def _etype(e): return str(e.get("raw_type") or e.get("type") or "").upper()

    lines  = [e for e in entities if _etype(e) in ("LINE", "POLYLINE", "LWPOLYLINE", "ARC", "SPLINE")]
    blocks = [e for e in entities if _etype(e) in ("INSERT", "BLOCK")]

    # ── 1. 도면 단위 이상 ─────────────────────────────────────────────────
    drawing_unit = str(drawing_data.get("drawing_unit") or "unknown").lower()
    if drawing_unit == "unknown":
        issues.append({
            "level":    "warning",
            "check_id": "unknown_drawing_unit",
            "message":  "도면 단위를 확인할 수 없음",
            "detail":   "drawing_unit이 'unknown'으로 추출됨. 치수 기반 법규 검토 정확도 저하 가능.",
        })
    elif drawing_unit not in ("mm", "cm", "m", "inch", "feet"):
        issues.append({
            "level":    "warning",
            "check_id": "nonstandard_drawing_unit",
            "message":  f"비표준 도면 단위: '{drawing_unit}'",
            "detail":   "mm, cm, m, inch, feet 외 단위는 이격 거리 계산 오류 유발 가능.",
        })

    # ── 2. 레이어 집중도 ──────────────────────────────────────────────────
    if entities:
        layer_counts = Counter(_elayer(e) for e in entities)
        top_layer, top_count = layer_counts.most_common(1)[0]
        concentration = top_count / len(entities)
        if concentration >= _LAYER_CONCENTRATION_THRESHOLD:
            issues.append({
                "level":    "warning",
                "check_id": "layer_concentration",
                "message":  f"전체 객체의 {concentration*100:.0f}%가 레이어 '{top_layer}'에 집중",
                "detail":   (
                    "레이어 분리가 되어 있지 않으면 도메인 분류 및 법규 적용 정확도가 낮아집니다. "
                    "전기/건축/치수 레이어를 분리하여 작성하세요."
                ),
            })

    # ── 3. 전기 블록 없음 ─────────────────────────────────────────────────
    if not blocks:
        issues.append({
            "level":    "warning",
            "check_id": "no_electric_blocks",
            "message":  "전기 블록(INSERT/BLOCK)이 0개",
            "detail":   (
                "전등, 콘센트, 분전반 등의 전기 설비가 블록으로 삽입되지 않았거나 "
                "explode(분해)되어 LINE/ARC로만 이루어져 있을 수 있음. "
                "블록 구조가 없으면 의미 분석 정확도가 크게 떨어집니다."
            ),
        })

    # ── 4. 전선 없음 ─────────────────────────────────────────────────────
    elec_line_layers = [
        e for e in lines
        if any(
            kw in str(e.get("layer") or "").upper()
            for kw in ("CABLE", "WIRE", "ELEC", "EL-", "E-", "PWR", "전선", "배선")
        )
    ]
    if blocks and not elec_line_layers:
        issues.append({
            "level":    "info",
            "check_id": "no_identified_wire_layers",
            "message":  "전기 배선 레이어 식별 불가",
            "detail":   (
                "블록은 있으나 CABLE/WIRE/ELEC 등 키워드가 포함된 레이어의 선이 없습니다. "
                "전선 레이어명에 전기 키워드(E-, EL-, CABLE 등)를 포함시키면 분석 정확도가 향상됩니다."
            ),
        })

    # ── 5. 분전반 없음 ───────────────────────────────────────────────────
    panel_blocks = [
        e for e in blocks
        if any(
            str(e.get("effective_name") or e.get("block_name") or "").upper().startswith(p)
            for p in _PANEL_PREFIXES
        )
    ]
    if blocks and not panel_blocks:
        issues.append({
            "level":    "info",
            "check_id": "no_panel_identified",
            "message":  "분전반(Panel) 블록이 식별되지 않음",
            "detail":   (
                "PNL, MCC, CB, MCB 등의 접두사를 가진 블록이 없어 회로 계통 분석이 불완전할 수 있습니다. "
                "effective_name 또는 block_name에 패널 접두사를 포함시키세요."
            ),
        })

    # ── 6. 도면 범위 이상 ────────────────────────────────────────────────
    all_bboxes = [_bbox_extents(e) for e in entities if _bbox_extents(e)]
    if all_bboxes:
        min_x = min(b[0] for b in all_bboxes)
        min_y = min(b[1] for b in all_bboxes)
        max_x = max(b[2] for b in all_bboxes)
        max_y = max(b[3] for b in all_bboxes)
        width  = abs(max_x - min_x)
        height = abs(max_y - min_y)
        if width > 1e-6 and height > 1e-6:
            ratio = max(width, height) / min(width, height)
            if ratio > _BBOX_EXTREME_RATIO:
                issues.append({
                    "level":    "warning",
                    "check_id": "extreme_drawing_extent",
                    "message":  f"도면 가로/세로 비율이 비정상적으로 큼 ({ratio:.0f}:1)",
                    "detail":   (
                        f"도면 범위: X[{min_x:.0f}~{max_x:.0f}], Y[{min_y:.0f}~{max_y:.0f}]. "
                        "좌표가 0에서 극단적으로 벗어난 객체가 있을 수 있습니다 (오삽입 의심)."
                    ),
                })

    # ── 7. 동일 위치 블록 중복 삽입 ──────────────────────────────────────
    dup_tol_raw = _DUPLICATE_BLOCK_TOL_MM / max(unit_factor, 1e-9)
    seen: list[tuple[str, float, float]] = []  # (block_name, x, y)
    dup_pairs: list[dict] = []
    reported_dups: set[frozenset] = set()

    for blk in blocks:
        bname = str(blk.get("effective_name") or blk.get("block_name") or "").upper()
        if not bname:
            continue
        pt = _ipt(blk)
        if not pt:
            continue
        bh = str(blk.get("handle") or "")
        for (prev_name, px, py) in seen:
            if prev_name != bname:
                continue
            dist = math.hypot(pt[0] - px, pt[1] - py)
            if dist <= dup_tol_raw:
                key = frozenset({bh, prev_name + f"_{px:.0f}_{py:.0f}"})
                if key not in reported_dups:
                    reported_dups.add(key)
                    dup_pairs.append({
                        "handle": bh,
                        "block_name": bname,
                        "dist_mm": round(dist * unit_factor, 2),
                    })
        seen.append((bname, pt[0], pt[1]))

    for dup in dup_pairs[:10]:  # 최대 10건만 보고
        issues.append({
            "level":    "warning",
            "check_id": "duplicate_block_insertion",
            "message":  f"블록 '{dup['block_name']}' 거의 같은 위치에 중복 삽입 ({dup['dist_mm']}mm)",
            "detail":   f"Handle: {dup['handle']}. 같은 심볼이 {dup['dist_mm']}mm 이내에 중복 배치되어 있습니다.",
        })

    _log.info(
        "[DrawingQA] 검사 완료 — 이슈 %d건 (error=%d warning=%d info=%d)",
        len(issues),
        sum(1 for i in issues if i["level"] == "error"),
        sum(1 for i in issues if i["level"] == "warning"),
        sum(1 for i in issues if i["level"] == "info"),
    )
    return issues


# ── 헬퍼 ─────────────────────────────────────────────────────────────────────

def _elayer(e: dict) -> str:
    return str(e.get("layer") or "")


def _ipt(e: dict) -> tuple[float, float] | None:
    for k in ("insert_point", "position", "center"):
        p = e.get(k)
        if isinstance(p, dict) and "x" in p:
            try:
                return float(p["x"]), float(p["y"])
            except (TypeError, ValueError):
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
        except (TypeError, ValueError, KeyError):
            pass
    return None
