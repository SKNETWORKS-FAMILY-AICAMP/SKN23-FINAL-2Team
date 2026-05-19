"""
File    : backend/services/agents/fire/sub/review/parser.py
Author  : 김민정
Create  : 2026-04-15
Modified: 2026-04-24 (Phase 8 — parse() 표준 인터페이스 추가, 소방 도메인 속성 추출 강화)
Description : 도면 데이터에서 소방 설비 객체들의 좌표 및 메타데이터를 추출합니다.

Modification History:
    - 2026-04-15 (김민정) : 도면 엔티티 기하 정보 및 속성 데이터 파싱 로직 구현
    - 2026-04-24 (Phase 8) : parse() 표준 인터페이스 추가 (mapping_table 지원, elements[] 반환),
                             fire_category 자동 추론, 소방 도메인 속성(coverage_area, height 등) 추출
"""

import json
import logging
import math
import re
from typing import Any

from backend.services.agents.common.object_mapping_utils import map_texts_to_blocks as _map_texts_to_blocks_util

_HEAD_DEDUP_TOL_MM = 10.0

# ── 소화기 커버리지 grid 상수 ────────────────────────────────────────────────
ARCH_ENTITY_TYPES  = frozenset({"LINE", "LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE"})
BBOX_PADDING       = 5_000.0   # mm (fire_equipment bbox fallback padding)
GRID_STEP_DEFAULT  = 5_000.0   # mm
GRID_MAX_SAMPLES   = 400
COVERAGE_RADIUS    = 20_000.0  # mm (NFSC 보행거리 20m)
COVERAGE_GAP_TOP_N = 5         # worst gap 전달 상한

# ── 소방 카테고리 추론 패턴 ──────────────────────────────────────────────────
_FIRE_CAT_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"SPK|SPRIN|스프링|헤드|HEAD",           re.IGNORECASE), "sprinkler"),
    (re.compile(r"FDH|DET|감지|DETECT",                  re.IGNORECASE), "detector"),
    (re.compile(r"HYD|소화전|HYDRANT",                   re.IGNORECASE), "hydrant"),
    (re.compile(r"EXTING|소화기|EXTINGUISH|\bEX[-_][A-Z0-9]+", re.IGNORECASE), "extinguisher"),
    (re.compile(r"PUMP|펌프|FP-",                        re.IGNORECASE), "pump"),
    (re.compile(r"ALARM|경보|BELL|SIREN",                re.IGNORECASE), "alarm"),
    (re.compile(r"PANEL|패널|제어반|CTRL|CONTROL",       re.IGNORECASE), "panel"),
    (re.compile(r"PIPE|배관|MAIN|BRANCH|RISER",          re.IGNORECASE), "pipe"),
]
_ARCH_LAYER_RE = re.compile(
    r"건축|ARCH|A[-_]|WALL|DOOR|WINDOW|ROOM|GRID|치수|벽|문|창",
    re.IGNORECASE,
)
_FIRE_SIGNAL_RE = re.compile(
    r"소방|FIRE|SP[-_]?|SPK|SPRIN|스프링|헤드|HEAD|"
    r"FDH|DET|감지|HYD|소화전|PUMP|펌프|FP[-_]|ALARM|경보|"
    r"PANEL|제어반|PIPE|배관|MAIN|BRANCH|RISER|"
    r"EXTING|소화기|EXTINGUISH",
    re.IGNORECASE,
)

# ── Role scoring 상수 ───────────────────────────────────────────────────────
_AUX_BBOX_PADDING  = 800.0   # aux text bbox 확장량 (drawing unit)
_AUX_POINT_RADIUS  = 500.0   # aux text bbox 없는 경우 근접 반경
_MIN_BLOCK_REPEAT  = 3       # 도면 내 블록 반복 횟수 임계값 (실제 설비 신호)

_AUX_TEXT_RE = re.compile(
    r"범례|기호|비고|내용|수량|일람|상세|설명|표제|도면목록|"
    r"NOTE|SCHEDULE|TABLE|LEGEND|TITLE|SYMBOL|REMARK",
    re.IGNORECASE,
)

# coverage_gap 샘플 제외 전용 — 범례/기호표/도면 제목/스케줄 텍스트 패턴.
# _AUX_TEXT_RE는 fire_object_role 판정용이므로 여기서 건드리지 않는다.
_COVERAGE_EXCLUDE_RE = re.compile(
    r"범례|기호|기호표|비고|내용|수량|명칭|일람|일람표|상세|설명|표제|도면목록|"
    r"NOTE|SCHEDULE|D\.W\.G|DWG|TABLE|LEGEND|TITLE|SYMBOL|REMARK|"
    r"DRAWN|CHECKED|APPROVED|SCALE|축척|도면명|도면번호|평면도|DATE",
    re.IGNORECASE,
)
_COVERAGE_AUX_EXCLUDE_PADDING  = 8_000.0  # mm — 범례/표제 키워드 매칭 시 패딩
_COVERAGE_TEXT_EXCLUDE_PADDING = 4_000.0  # mm — broad TEXT/MTEXT 폴백 패딩


def _resolve_term(term_map: dict[str, str], *names: str) -> str:
    """
    MappingAgent 결과를 parser에서 보수적으로 재해석한다.
    term_map에 "EX" 같은 접두어만 있어도 "EX-a3" 블록을 소화기로 풀기 위함.
    """
    for raw_name in names:
        name = str(raw_name or "").strip()
        if not name:
            continue
        exact = term_map.get(name)
        if exact:
            return str(exact)

        match = re.match(r"^([A-Za-z]+)(?:[-_].*|\d.*)?$", name)
        if match:
            prefix = match.group(1).upper()
            mapped = term_map.get(prefix)
            if mapped:
                return str(mapped)
    return ""


def _get_xy(el: dict) -> tuple[float, float] | None:
    """element에서 (x, y) 추출. 직접 필드와 position dict 모두 지원."""
    x, y = el.get("x"), el.get("y")
    if x is not None and y is not None:
        try:
            return float(x), float(y)
        except (TypeError, ValueError):
            pass
    pos = el.get("position")
    if isinstance(pos, dict):
        px, py = pos.get("x"), pos.get("y")
        if px is not None and py is not None:
            try:
                return float(px), float(py)
            except (TypeError, ValueError):
                pass
    return None


def _percentile(values: list[float], pct: float) -> float:
    """순수 Python linear-interpolation percentile (numpy 미사용)."""
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    if n == 0:
        raise ValueError("_percentile requires at least one value")
    if n == 1:
        return sorted_vals[0]
    idx = (pct / 100.0) * (n - 1)
    lo, hi = int(idx), min(int(idx) + 1, n - 1)
    return sorted_vals[lo] * (1.0 - (idx - lo)) + sorted_vals[hi] * (idx - lo)


def _bbox_from_coords_list(coords: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    return min(xs), min(ys), max(xs), max(ys)


def _trimmed_bbox_from_coords(
    coords: list[tuple[float, float]],
    trim_pct: float = 5.0,
) -> tuple[float, float, float, float] | None:
    if len(coords) < 2:
        return None
    if len(coords) < 5:
        return _bbox_from_coords_list(coords)
    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    x_lo, x_hi = _percentile(xs, trim_pct), _percentile(xs, 100.0 - trim_pct)
    y_lo, y_hi = _percentile(ys, trim_pct), _percentile(ys, 100.0 - trim_pct)
    trimmed = [(x, y) for x, y in coords if x_lo <= x <= x_hi and y_lo <= y <= y_hi]
    if len(trimmed) >= 2:
        return _bbox_from_coords_list(trimmed)
    return _bbox_from_coords_list(coords)


def _pad_bbox(
    bbox: tuple[float, float, float, float],
    padding: float,
) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = bbox
    return x0 - padding, y0 - padding, x1 + padding, y1 + padding


def _estimate_floor_bbox(
    elements: list[dict],
    active_object_ids: set[str] | None = None,  # TODO: parse() 시그니처 확장 후 활성화
) -> tuple[float, float, float, float] | None:
    """
    검토 bbox를 4단계 우선순위로 추정한다.
    1순위: active_object_ids 선택 엔티티 (현재 parse()에 전달 경로 없음 — MVP TODO)
    2순위: fire_equipment {detector/sprinkler/hydrant/alarm/panel} — trimmed bbox + BBOX_PADDING
    3순위: 모든 fire_equipment (소화기 포함) — trimmed bbox + BBOX_PADDING
    4순위: 건축 평면 요소 (LINE/LWPOLYLINE/POLYLINE/ARC/CIRCLE) — 1%/99% trimming
    마지막: fire_equipment 1개만 있을 때 degenerate bbox + BBOX_PADDING fallback
    """
    # 1순위: 사용자 선택 영역
    if active_object_ids:
        coords = [
            xy for el in elements
            if el.get("id") in active_object_ids
            for xy in [_get_xy(el)] if xy is not None
        ]
        if len(coords) >= 2:
            return _bbox_from_coords_list(coords)

    # 2순위: 소방 anchor 설비(스프링클러/감지기/소화전/경보/패널) — 범례/표제 쪽이 bbox를 키우지 않도록 우선
    anchor_coords = [
        xy for el in elements
        if el.get("fire_object_role") == "fire_equipment"
        and el.get("fire_category") in {"detector", "sprinkler", "hydrant", "alarm", "panel"}
        for xy in [_get_xy(el)] if xy is not None
    ]
    anchor_bbox = _trimmed_bbox_from_coords(anchor_coords)
    if anchor_bbox is not None:
        return _pad_bbox(anchor_bbox, BBOX_PADDING)

    # 3순위: 모든 fire_equipment (소화기 포함)
    eq_coords = [
        xy for el in elements
        if el.get("fire_object_role") == "fire_equipment"
        for xy in [_get_xy(el)] if xy is not None
    ]
    eq_bbox = _trimmed_bbox_from_coords(eq_coords)
    if eq_bbox is not None:
        return _pad_bbox(eq_bbox, BBOX_PADDING)

    # 4순위: 건축 평면 요소 (LINE/LWPOLYLINE/…)
    arch_coords = [
        xy for el in elements
        if (el.get("raw_type") or el.get("type") or "").upper() in ARCH_ENTITY_TYPES
        for xy in [_get_xy(el)] if xy is not None
    ]
    if len(arch_coords) >= 2:
        xs = [c[0] for c in arch_coords]
        ys = [c[1] for c in arch_coords]
        x_lo, x_hi = _percentile(xs, 1), _percentile(xs, 99)
        y_lo, y_hi = _percentile(ys, 1), _percentile(ys, 99)
        trimmed = [(x, y) for x, y in arch_coords if x_lo <= x <= x_hi and y_lo <= y <= y_hi]
        if len(trimmed) >= 2:
            return _bbox_from_coords_list(trimmed)

    # 마지막: fire_equipment가 1개뿐일 때 — eq_coords는 위에서 이미 계산됨
    # _trimmed_bbox_from_coords는 len<2 시 None 반환, _bbox_from_coords_list는 단일 좌표도 허용
    if eq_coords:
        x0, y0, x1, y1 = _bbox_from_coords_list(eq_coords)
        return x0 - BBOX_PADDING, y0 - BBOX_PADDING, x1 + BBOX_PADDING, y1 + BBOX_PADDING

    return None


def _compute_grid_step(x0: float, y0: float, x1: float, y1: float) -> float:
    area = (x1 - x0) * (y1 - y0)
    if area <= 0:
        return GRID_STEP_DEFAULT
    min_step = (area / GRID_MAX_SAMPLES) ** 0.5
    return max(GRID_STEP_DEFAULT, min_step)


def _generate_sample_points(
    x0: float, y0: float, x1: float, y1: float, step: float
) -> list[tuple[float, float]]:
    inset = min(step / 2.0, 2_500.0)
    ix0, ix1 = x0 + inset, x1 - inset
    iy0, iy1 = y0 + inset, y1 - inset
    if ix0 > ix1 or iy0 > iy1:
        ix0, ix1, iy0, iy1 = x0, x1, y0, y1

    points: set[tuple[float, float]] = set()
    x = ix0
    while x <= ix1 + 1e-6:
        y = iy0
        while y <= iy1 + 1e-6:
            points.add((round(x, 1), round(y, 1)))
            y += step
        x += step

    # bbox 중심점 항상 포함
    points.add((round((x0 + x1) / 2.0, 1), round((y0 + y1) / 2.0, 1)))
    return sorted(points)


def _compute_extinguisher_coverage_gaps(
    elements: list[dict],
    active_object_ids: set[str] | None = None,
    aux_bboxes: list[tuple[float, float, float, float]] | None = None,
) -> list[dict]:
    """
    소화기 커버리지 공백 샘플 포인트 목록을 반환한다 (worst N개).
    aux_bboxes 영역 내부 샘플 포인트는 제외한다 (제목/표제/범례 영역 오검출 방지).
    소화기 0개이거나 bbox 추정 실패 시 [] 반환.
    """
    extinguishers: list[tuple[dict, float, float]] = []
    for el in elements:
        if (
            el.get("fire_category") == "extinguisher"
            and el.get("fire_object_role") == "fire_equipment"
        ):
            xy = _get_xy(el)
            if xy is not None:
                extinguishers.append((el, xy[0], xy[1]))

    logging.debug("[CoverageGap] 소화기 %d개 인식", len(extinguishers))
    if not extinguishers:
        logging.debug("[CoverageGap] 소화기 없음 — coverage_gap 검사 생략")
        return []

    bbox = _estimate_floor_bbox(elements, active_object_ids)
    if bbox is None:
        logging.warning("[CoverageGap] bbox 추정 불가 — coverage_gap 검사 생략")
        return []

    x0, y0, x1, y1 = bbox
    logging.debug("[CoverageGap] floor bbox (%.0f,%.0f)–(%.0f,%.0f)", x0, y0, x1, y1)

    step    = _compute_grid_step(x0, y0, x1, y1)
    samples = _generate_sample_points(x0, y0, x1, y1, step)
    logging.debug("[CoverageGap] grid step=%.0f, 샘플 %d개", step, len(samples))

    # 제목/표제/범례 영역 내부 샘플 포인트 제외
    excl = aux_bboxes or []
    logging.debug("[CoverageGap] 제외 bbox=%d개 적용", len(excl))
    filtered: list[tuple[float, float]] = []
    excluded_count = 0
    for sx, sy in samples:
        in_excl = False
        for bx in excl:
            bx1, by1 = min(bx[0], bx[2]), min(bx[1], bx[3])
            bx2, by2 = max(bx[0], bx[2]), max(bx[1], bx[3])
            if bx1 <= sx <= bx2 and by1 <= sy <= by2:
                in_excl = True
                break
        if in_excl:
            excluded_count += 1
        else:
            filtered.append((sx, sy))

    logging.debug(
        "[CoverageGap] 포인트 %d개 제외, 유효 %d개",
        excluded_count, len(filtered),
    )

    gaps: list[dict] = []
    for sx, sy in filtered:
        nearest_el, nx, ny = min(
            extinguishers,
            key=lambda t: (t[1] - sx) ** 2 + (t[2] - sy) ** 2,
        )
        dist = math.sqrt((nx - sx) ** 2 + (ny - sy) ** 2)
        if dist > COVERAGE_RADIUS:
            gaps.append({
                "sample_id":               "",   # 아래에서 재번호 매김
                "x":                       sx,
                "y":                       sy,
                "nearest_extinguisher_id": str(nearest_el.get("id") or nearest_el.get("handle") or ""),
                "nearest_distance_mm":     round(dist, 1),
            })

    gaps.sort(key=lambda g: g["nearest_distance_mm"], reverse=True)
    result = gaps[:COVERAGE_GAP_TOP_N]
    for j, g in enumerate(result):
        g["sample_id"] = f"cov_{j}"
    logging.debug(
        "[CoverageGap] gap 후보 %d개 → worst %d개 반환 (worst 거리: %.0fmm)",
        len(gaps), len(result),
        result[0]["nearest_distance_mm"] if result else 0.0,
    )
    return result


def _parse_bbox(entity: dict) -> tuple[float, float, float, float] | None:
    b = entity.get("bbox")
    if not isinstance(b, dict):
        return None
    try:
        if "x1" in b and "x2" in b:
            return float(b["x1"]), float(b["y1"]), float(b["x2"]), float(b["y2"])
        if "min_x" in b and "max_x" in b:
            return (
                float(b["min_x"]), float(b["min_y"]),
                float(b["max_x"]), float(b["max_y"]),
            )
    except (KeyError, TypeError, ValueError):
        return None
    return None


def _expand_bbox(
    bbox: tuple[float, float, float, float], padding: float
) -> tuple[float, float, float, float]:
    x1, y1, x2, y2 = bbox
    return x1 - padding, y1 - padding, x2 + padding, y2 + padding


def _bbox_contains_point(
    bbox: tuple[float, float, float, float], pos: dict
) -> bool:
    try:
        x = float(pos.get("x") or 0)
        y = float(pos.get("y") or 0)
    except (TypeError, ValueError):
        return False
    x1, y1 = min(bbox[0], bbox[2]), min(bbox[1], bbox[3])
    x2, y2 = max(bbox[0], bbox[2]), max(bbox[1], bbox[3])
    return x1 <= x <= x2 and y1 <= y <= y2


def _bbox_overlaps(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> bool:
    ax1, ay1 = min(a[0], a[2]), min(a[1], a[3])
    ax2, ay2 = max(a[0], a[2]), max(a[1], a[3])
    bx1, by1 = min(b[0], b[2]), min(b[1], b[3])
    bx2, by2 = max(b[0], b[2]), max(b[1], b[3])
    return not (ax2 < bx1 or bx2 < ax1 or ay2 < by1 or by2 < ay1)


def _collect_aux_regions(
    entities: list[dict],
) -> tuple[list[tuple[float, float, float, float]], list[tuple[float, float]]]:
    aux_bboxes: list[tuple[float, float, float, float]] = []
    aux_points: list[tuple[float, float]] = []
    for ent in entities:
        raw_type = str(ent.get("raw_type") or ent.get("type") or "").upper()
        if raw_type not in ("TEXT", "MTEXT", "MLEADER", "LEADER"):
            continue
        text = str(ent.get("text") or ent.get("content") or "")
        if not _AUX_TEXT_RE.search(text):
            continue
        bbox = _parse_bbox(ent)
        if bbox is not None:
            aux_bboxes.append(_expand_bbox(bbox, _AUX_BBOX_PADDING))
        else:
            pos = ent.get("center") or ent.get("insert_point") or ent.get("position")
            if isinstance(pos, dict):
                try:
                    aux_points.append((float(pos["x"]), float(pos["y"])))
                except (KeyError, TypeError, ValueError):
                    pass
    return aux_bboxes, aux_points


def _collect_coverage_exclude_bboxes(
    entities: list[dict],
) -> list[tuple[float, float, float, float]]:
    """
    coverage_gap 샘플 포인트 제외 영역을 수집한다.
    _AUX_TEXT_RE(fire_object_role 판정용) + _COVERAGE_EXCLUDE_RE(coverage 전용 확장 패턴) 적용.
    키워드 매칭 시 8000mm, broad TEXT/MTEXT 폴백 시 4000mm 패딩.
    """
    bboxes: list[tuple[float, float, float, float]] = []
    for ent in entities:
        raw_type = str(ent.get("raw_type") or ent.get("type") or "").upper()
        if raw_type in ("TEXT", "MTEXT", "MLEADER", "LEADER"):
            text = str(ent.get("text") or ent.get("content") or "")
            matched = _AUX_TEXT_RE.search(text) or _COVERAGE_EXCLUDE_RE.search(text)
            broad_text_exclude = raw_type in ("TEXT", "MTEXT") and len(text.strip()) >= 4
            if not (matched or broad_text_exclude):
                continue
            padding = _COVERAGE_AUX_EXCLUDE_PADDING if matched else _COVERAGE_TEXT_EXCLUDE_PADDING
        elif raw_type in ("INSERT", "BLOCK"):
            attrs = ent.get("attributes") or ent.get("metadata") or {}
            attr_parts: list[str] = []
            if isinstance(attrs, dict):
                for key, value in attrs.items():
                    attr_parts.append(str(key))
                    attr_parts.append(str(value))
            elif isinstance(attrs, list):
                for item in attrs:
                    if isinstance(item, dict):
                        attr_parts.extend(str(v) for v in item.values())
                    else:
                        attr_parts.append(str(item))
            text = " ".join(
                [
                    str(ent.get("block_name") or ent.get("name") or ""),
                    str(ent.get("layer") or ""),
                    " ".join(attr_parts),
                ]
            )
            matched = _AUX_TEXT_RE.search(text) or _COVERAGE_EXCLUDE_RE.search(text)
            if not matched:
                continue
            padding = _COVERAGE_AUX_EXCLUDE_PADDING
        else:
            continue
        bbox = _parse_bbox(ent)
        if bbox is not None:
            bboxes.append(_expand_bbox(bbox, padding))
        else:
            pos = ent.get("center") or ent.get("insert_point") or ent.get("position")
            if isinstance(pos, dict):
                try:
                    px, py = float(pos["x"]), float(pos["y"])
                    bboxes.append((px - padding, py - padding, px + padding, py + padding))
                except (KeyError, TypeError, ValueError):
                    pass
    return bboxes


def _count_block_names(entities: list[dict]) -> dict[str, int]:
    # 범례 심볼도 포함해 카운트한다. 범례에 동일 블록이 있으면 count가 1 증가하지만
    # _MIN_BLOCK_REPEAT(3) 판정과 단독등장(1~2) 판정에 미치는 영향이 미미하다.
    counts: dict[str, int] = {}
    for ent in entities:
        raw_type = str(ent.get("raw_type") or ent.get("type") or "").upper()
        if raw_type not in ("INSERT", "BLOCK"):
            continue
        name = str(ent.get("block_name") or ent.get("name") or "")
        if name:
            counts[name] = counts.get(name, 0) + 1
    return counts


def _score_fire_object_role(
    element: dict,
    aux_bboxes: list[tuple[float, float, float, float]],
    aux_points: list[tuple[float, float]],
    block_counts: dict[str, int],
) -> tuple[str, int, int]:
    fire_category = element.get("fire_category", "unknown")
    layer          = str(element.get("layer") or "")
    block_name     = str(element.get("block_name") or "")
    count          = block_counts.get(block_name, 0)
    pos            = element.get("position") or {}
    el_bbox        = _parse_bbox(element)

    # ── detector_score ──────────────────────────────────────────────────────
    detector_score = 0
    if fire_category != "unknown":
        detector_score += 2
    if _has_fire_signal(layer):
        detector_score += 1
    if count >= _MIN_BLOCK_REPEAT:
        detector_score += 1

    # ── aux_score ───────────────────────────────────────────────────────────
    aux_score = 0

    # [+3] bbox 겹침 OR 내부 포함 (padded aux_bboxes 사용)
    bbox_hit = False
    for aux_bbox in aux_bboxes:
        if el_bbox is not None and _bbox_overlaps(el_bbox, aux_bbox):
            bbox_hit = True
            break
        elif isinstance(pos, dict) and _bbox_contains_point(aux_bbox, pos):
            bbox_hit = True
            break
    if bbox_hit:
        aux_score += 3

    # [+2] bbox 없는 aux_point 근접 (bbox 히트가 없을 때만)
    if not bbox_hit and isinstance(pos, dict):
        try:
            ex = float(pos.get("x") or 0)
            ey = float(pos.get("y") or 0)
        except (TypeError, ValueError):
            ex, ey = 0.0, 0.0
        for px, py in aux_points:
            if math.hypot(ex - px, ey - py) <= _AUX_POINT_RADIUS:
                aux_score += 2
                break

    # [+1] layer에 aux 키워드
    if _AUX_TEXT_RE.search(layer):
        aux_score += 1

    # [+1] 도면 전체에 1~2회만 등장
    if 1 <= count <= 2:
        aux_score += 1

    # ── role 판정 ────────────────────────────────────────────────────────────
    if aux_score >= 5:
        role = "fire_aux"
    elif aux_score >= 3 and detector_score < 3:
        role = "fire_aux"
    elif aux_score >= 3 and detector_score >= 3:
        role = "unknown_fire_symbol"
    elif fire_category != "unknown" and detector_score >= 2 and aux_score < 3:
        role = "fire_equipment"
    else:
        role = "unknown_fire_symbol"

    return role, detector_score, aux_score


def _assign_fire_object_roles(
    elements: list[dict],
    aux_bboxes: list[tuple[float, float, float, float]],
    aux_points: list[tuple[float, float]],
    block_counts: dict[str, int],
) -> None:
    for element in elements:
        raw_type = str(element.get("raw_type") or "").upper()
        if raw_type not in ("INSERT", "BLOCK"):
            element["fire_object_role"] = None
            continue
        role, det_score, aux_score = _score_fire_object_role(
            element, aux_bboxes, aux_points, block_counts
        )
        element["fire_object_role"] = role
        element["_detector_score"]  = det_score
        element["_aux_score"]       = aux_score


def _include_in_topology(el: dict) -> bool:
    role = el.get("fire_object_role")
    return role is None or role == "fire_equipment"


def _infer_fire_category(layer: str, raw_type: str, block_name: str = "", resolved_type: str = "") -> str:
    text = f"{layer} {raw_type} {block_name} {resolved_type}"
    for pattern, category in _FIRE_CAT_MAP:
        if pattern.search(text):
            return category
    return "unknown"


def _has_fire_signal(*values: Any) -> bool:
    text = " ".join(str(v or "") for v in values)
    return bool(_FIRE_SIGNAL_RE.search(text))


def _is_arch_only_layer(layer: str, *values: Any) -> bool:
    return bool(_ARCH_LAYER_RE.search(layer or "")) and not _has_fire_signal(layer, *values)


def _dedupe_heads_by_position(heads: list[dict]) -> list[dict]:
    """
    같은 물리 헤드가 BLOCK/심볼/속성 등으로 중복 추출되는 경우가 있다.
    중복을 제거하지 않으면 최근접 거리가 0mm가 되어 실제 간격 위반을 놓치므로,
    좌표가 거의 같은 헤드는 하나의 헤드로 병합한다.
    """
    unique: list[dict] = []
    for head in heads:
        pos = head.get("position") or {}
        try:
            x = float(pos.get("x") or 0)
            y = float(pos.get("y") or 0)
        except (TypeError, ValueError):
            unique.append(head)
            continue

        merged = False
        for existing in unique:
            epos = existing.get("position") or {}
            try:
                ex = float(epos.get("x") or 0)
                ey = float(epos.get("y") or 0)
            except (TypeError, ValueError):
                continue
            if math.hypot(x - ex, y - ey) <= _HEAD_DEDUP_TOL_MM:
                aliases = existing.setdefault("duplicate_handles", [])
                h = str(head.get("handle") or head.get("id") or "")
                if h:
                    aliases.append(h)
                merged = True
                break
        if not merged:
            unique.append(dict(head))
    return unique


def _compute_nearest_distances(elements: list, fire_category: str, limit_mm: float) -> dict:
    """
    지정 카테고리 설비들의 최근접 거리를 계산한다.
    각 설비의 가장 가까운 동종 설비 거리만 사용하여 위반 후보를 판정한다.
    결과는 compliance 프롬프트의 fire_topology.<category> 필드로 전달된다.
    """
    if fire_category == "sprinkler":
        # Strict: only fire_equipment role counts as a real sprinkler head.
        # role=None elements (LINE, TEXT on sprinkler layers) are legend/pipe noise.
        # TODO: unify all spacing categories to fire_equipment-only in a future patch
        #       once detector/hydrant regression tests are confirmed safe.
        targets = [
            el for el in elements
            if isinstance(el, dict)
            and el.get("fire_category") == "sprinkler"
            and el.get("fire_object_role") == "fire_equipment"
            and isinstance(el.get("position"), dict)
        ]
    else:
        targets = [
            el for el in elements
            if isinstance(el, dict)
            and el.get("fire_category") == fire_category
            and _include_in_topology(el)
            and isinstance(el.get("position"), dict)
        ]
    raw_count = len(targets)
    targets = _dedupe_heads_by_position(targets)
    if len(targets) < 2:
        return {
            "nearest_distances": [],
            "violation_candidates": [],
            "unique_count": len(targets),
            "raw_count": raw_count,
            "limit_mm": limit_mm,
        }

    nearest_by_id: dict[str, dict] = {}
    for i in range(len(targets)):
        for j in range(i + 1, len(targets)):
            pa = targets[i]["position"]
            pb = targets[j]["position"]
            dx = float(pa.get("x") or 0) - float(pb.get("x") or 0)
            dy = float(pa.get("y") or 0) - float(pb.get("y") or 0)
            dist = round(math.sqrt(dx * dx + dy * dy), 1)
            id_a = str(targets[i].get("handle") or targets[i].get("id") or "")
            id_b = str(targets[j].get("handle") or targets[j].get("id") or "")
            for eid, nearest_eid in ((id_a, id_b), (id_b, id_a)):
                current = nearest_by_id.get(eid)
                if current is None or dist < current["distance_mm"]:
                    nearest_by_id[eid] = {"head": eid, "nearest_head": nearest_eid, "distance_mm": dist}

    nearest_distances = sorted(
        nearest_by_id.values(),
        key=lambda x: x["distance_mm"],
        reverse=True,
    )
    violation_candidates = [
        {**item, "limit_mm": limit_mm}
        for item in nearest_distances
        if item["distance_mm"] > limit_mm
    ]
    return {
        "nearest_distances": nearest_distances,
        "violation_candidates": violation_candidates,
        "unique_count": len(targets),
        "raw_count": raw_count,
        "limit_mm": limit_mm,
    }


class ParserAgent:
    """
    도면 JSON → 소방 검토용 구조체 변환기.

    parse()   : 표준 인터페이스 (mapping_table 지원, {elements:[]} 반환)
    execute() : 레거시 인터페이스 (backward-compat 유지)
    """

    # ── 표준 인터페이스 ───────────────────────────────────────────────────────

    def parse(self, raw_layout: dict | str, mapping_table: dict | None = None) -> dict:
        """
        Parameters
        ----------
        raw_layout    : C# 형식 도면 JSON (str 또는 dict)
                        {entities: [{handle, type, layer, bbox, ...}, ...]}
        mapping_table : MappingAgent 출력 (term_map, entity_type_map)

        Returns
        -------
        {
          "elements": [
            {
              "id":                    str,   # TAG_NAME or handle
              "handle":                str,
              "type":                  str,   # 매핑 후 전문 용어
              "raw_type":              str,   # 원본 CAD 타입
              "layer":                 str,
              "fire_category":         str,   # sprinkler|detector|hydrant|pump|alarm|panel|pipe|unknown
              "position":              {"x": float, "y": float},
              "bbox":                  dict | None,
              "coverage_area_m2":      float,
              "installation_height_mm": float,
              "standard_type":         str | None,
              "attributes":            dict,
            },
            ...
          ]
        }
        """
        if isinstance(raw_layout, str):
            try:
                raw_layout = json.loads(raw_layout)
            except json.JSONDecodeError:
                logging.error("[FireParserAgent] JSON 파싱 실패 — 빈 elements 반환")
                return {"elements": []}

        if not isinstance(raw_layout, dict):
            return {"elements": []}

        entities = (
            raw_layout.get("entities")
            or raw_layout.get("elements")
            or []
        )
        term_map: dict[str, str]        = (mapping_table or {}).get("term_map", {})
        entity_type_map: dict[str, str] = (mapping_table or {}).get("entity_type_map", {})

        elements: list[dict[str, Any]] = []
        skipped_arch = 0
        for ent in entities:
            handle     = str(ent.get("handle") or ent.get("object_id") or "")
            raw_type   = str(ent.get("raw_type") or ent.get("type") or "")
            layer      = str(ent.get("layer") or "")
            block_name = str(ent.get("block_name") or "")
            attrs      = ent.get("attributes") or ent.get("metadata") or {}

            resolved_type = (
                _resolve_term(term_map, block_name, layer)
                or entity_type_map.get(raw_type.upper())
                or raw_type
            )
            fire_category = _infer_fire_category(layer, raw_type, block_name, resolved_type)
            attr_text = " ".join(str(v or "") for v in attrs.values()) if isinstance(attrs, dict) else ""
            if fire_category == "unknown" and _is_arch_only_layer(
                layer,
                raw_type,
                block_name,
                resolved_type,
                ent.get("standard_type"),
                attr_text,
            ):
                skipped_arch += 1
                continue

            position = (
                ent.get("center")
                or ent.get("insert_point")
                or ent.get("position")
                or {"x": self._to_float(ent.get("x")), "y": self._to_float(ent.get("y"))}
            )

            elements.append({
                "id":                     str(attrs.get("TAG_NAME") or handle),
                "handle":                 handle,
                "type":                   resolved_type,
                "raw_type":               raw_type,
                "layer":                  layer,
                "block_name":             block_name,
                "fire_category":          fire_category,
                "position":               position,
                "bbox":                   ent.get("bbox"),
                "coverage_area_m2":       self._to_float(
                                              attrs.get("COVERAGE") or attrs.get("AREA")
                                          ),
                "installation_height_mm": self._to_float(
                                              attrs.get("HEIGHT") or attrs.get("INSTALL_HEIGHT")
                                          ),
                "standard_type":          ent.get("standard_type") or attrs.get("STANDARD_TYPE"),
                "attributes":             attrs,
            })

        if skipped_arch:
            logging.info(
                "[FireParserAgent] 건축 레이어 비소방 객체 %d건 제외: parsed_elements=%d",
                skipped_arch,
                len(elements),
            )

        # Two-pass: fire_object_role 할당
        aux_bboxes, aux_points = _collect_aux_regions(entities)
        block_counts = _count_block_names(entities)
        _assign_fire_object_roles(elements, aux_bboxes, aux_points, block_counts)
        logging.debug(
            "[FireParserAgent] role_scoring: aux_bboxes=%d aux_points=%d",
            len(aux_bboxes), len(aux_points),
        )

        logging.debug("[FireParserAgent] parse() elements=%d", len(elements))
        _ext_targets = [
            el for el in elements
            if isinstance(el, dict)
            and el.get("fire_category") == "extinguisher"
            and _include_in_topology(el)
            and isinstance(el.get("position"), dict)
        ]
        _ext_topo = {
            # Extinguisher rule is walking-distance coverage, not extinguisher-to-extinguisher spacing.
            "nearest_distances": [],
            "violation_candidates": [],
            "unique_count": len(_dedupe_heads_by_position(_ext_targets)),
            "raw_count": len(_ext_targets),
            "limit_mm": COVERAGE_RADIUS,
        }
        cov_excl = _collect_coverage_exclude_bboxes(entities)
        logging.debug("[CoverageGap] 제외 텍스트 bbox %d개", len(cov_excl))
        _ext_topo["coverage_gaps"] = _compute_extinguisher_coverage_gaps(
            elements, aux_bboxes=cov_excl
        )

        return {
            "elements": elements,
            "fire_topology": {
                "sprinkler":    _compute_nearest_distances(elements, "sprinkler",    2300.0),
                "detector":     _compute_nearest_distances(elements, "detector",     4500.0),
                "hydrant":      _compute_nearest_distances(elements, "hydrant",     25000.0),
                "extinguisher": _ext_topo,
            },
        }

    # ── 레거시 인터페이스 (hasattr 어댑터 호환) ──────────────────────────────

    def execute(self, drawing_data: dict | str) -> dict:
        """
        레거시 호출 경로 — workflow_handler의 hasattr 어댑터가 parse()를 우선 사용하므로
        직접 호출되는 경우는 없지만 하위 호환을 위해 유지합니다.
        """
        if isinstance(drawing_data, str):
            try:
                drawing_data = json.loads(drawing_data)
            except json.JSONDecodeError:
                logging.error("[ParserAgent] 도면 데이터 파싱 실패: 유효하지 않은 JSON 형식")
                return {"error": "Invalid JSON", "parsed_entities": [], "total_count": 0}

        entities = drawing_data.get("entities", [])
        parsed_entities = []

        for entity in entities:
            parsed_entities.append({
                "id":            entity.get("handle") or entity.get("object_id"),
                "standard_type": entity.get("standard_type"),
                "x":             entity.get("x", 0.0),
                "y":             entity.get("y", 0.0),
                "layer":         entity.get("layer"),
                "metadata":      entity.get("metadata", {}),
            })

        return {
            "parsed_entities": parsed_entities,
            "total_count":     len(parsed_entities),
            "status":          "success",
        }

    # ── 다중 객체 매핑 (공통 유틸 사용) ─────────────────────────────────────────

    async def async_map_texts_to_blocks(
        self,
        text_entities: list[dict],
        block_entities: list[dict],
        *,
        ambiguity_threshold: float = 10.0,
    ) -> list[dict]:
        """
        (비동기) 텍스트 → 소방 블록 매핑 + 모호 케이스 LLM fallback.
        OOM 방지 전역 세마포어 및 label 필드 포함.

        Returns
        -------
        [{"text_handle", "block_handle", "label", "score", "method"}, ...]
        """
        return await _map_texts_to_blocks_util(
            text_entities,
            block_entities,
            domain_hint="소방",
            layer_bonus_config=None,   # 소방은 레이어 보너스 없음
            ambiguity_threshold=ambiguity_threshold,
        )

    # ── 헬퍼 ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _to_float(value: Any) -> float:
        try:
            return float(
                str(value)
                .replace("mm", "").replace("m²", "").replace("m2", "")
                .replace("m", "").strip()
            )
        except (ValueError, TypeError):
            return 0.0
