"""
File    : backend/services/agents/common/domain_classifier/feature_extractor.py
Author  : 김다빈
Create  : 2026-04-21
Modified: 2026-04-26

Description :
    CAD JSON → 도메인 분류용 피처 벡터 변환기.
    구조적·기하적 특성만 사용해 언어 독립적 분류 달성.

    피처 구성 (총 94차원):
        [A] 엔티티 타입 비율       12차원
        [B] 엔티티 존재 여부        4차원
        [C] 파생 엔티티 비율        4차원
        [D] 수치 통계               4차원
        [E] drawing_unit 원핫       4차원
        [F] 레이어명 길이 통계      4차원
        [G] 레이어명 스타일 비율    5차원  ← korean_ratio 제거 (변별력 없음)
        [H] 레이어 구조 패턴        4차원  ← AIA 접두어 대체 (언어 독립)
        [I] 레이어 분포 집중도      2차원  ← Counter 기반 수정
        [J] 레이어별 엔티티 통계    4차원  ← Counter 기반 수정
        [K] 레이어 속성 패턴        4차원
        [L] 레이어 색상 다양성      2차원
        [M] 엔티티 오버라이드 비율  2차원
        [N] 기하 통계               2차원
        [O] 구조적 밀도 분석        8차원
        [P] 형태 분포 분석          8차원
        [Q] CIRCLE vs ARC 분리      2차원
        [R] 레이어 색상 패턴        2차원
        [S] INSERT/BLOCK 구조 분석  3차원
        [T] 기하 교호작용 비율      2차원
        [U] 엔티티 색상 시그니처    3차원  ← 신규 (소방=빨강/전기=노랑/배관=청록)
        [V] 블록·해치 비율          2차원  ← 신규
        [W] 소프트 블록 키워드 매칭 3차원  ← 신규 (arch↔fire, pipe↔elec 분류 개선)
        [X] 레이어명 키워드 매칭    3차원  ← 신규
        [Y] 긴 선분 비율            1차원  ← 신규

Modification History :
    2026-04-21 | 김다빈 | 최초 작성
    2026-04-21 | 김다빈 | 38차원 → 58차원 확장
    2026-04-21 | 김다빈 | 58차원 → 74차원 (레이어 키워드·텍스트 분석 추가)
    2026-04-22 | 김다빈 | 74차원 → 83차원 (소방/배관 구분 강화)
    2026-04-22 | 김다빈 | 언어 의존 피처 전면 제거 → 구조/기하 피처로 교체
    2026-04-23 | 김다빈 | SOLID 타입 추가 (83→85차원), 데이터 정리 (435→404개)
    2026-04-26 | 김다빈 | 노이즈 제거 + 한국 도면 맞춤 피처 추가 (85→87차원)
                          [G] korean_ratio 제거, [H] AIA 6개 → 구조 패턴 4개 교체,
                          [I][J] entity_count 필드 → Counter 기반 수정,
                          [U][V] 신규 그룹 추가
    2026-04-27 | 김다빈 | 분류 성능 개선 (87→94차원) [W][X][Y] 추가
                          [W] 소프트 블록 키워드 매칭 (3),
                          [X] 레이어명 키워드 매칭 (3),
                          [Y] 긴 선분 비율 (1)
"""

import math
import unicodedata
import re
from collections import Counter

import numpy as np


# ── 타입 목록 ───────────────────────────────────────────────────────────────
ENTITY_TYPES: list[str] = [
    "LINE", "POLYLINE", "BLOCK", "ARC",
    "TEXT", "MTEXT", "HATCH", "ELLIPSE",
    "SPLINE", "CIRCLE", "INSERT", "SOLID",
]
BINARY_ENTITY_TYPES: list[str] = ["HATCH", "ELLIPSE", "SPLINE", "SOLID"]
UNIT_LIST: list[str] = ["mm", "inch", "m", "unknown"]

# 엔티티 색상 시그니처 — AutoCAD 색상 인덱스 (국제 공통 숫자 코드)
_RED_COLORS    = frozenset((1, 10, 14))   # 소방 관행
_YELLOW_COLORS = frozenset((2, 50))       # 전기 관행
_CYAN_COLORS   = frozenset((4,))          # 배관 주배관 관행

# ── 도메인별 블록명 키워드 (소프트 매칭용) ─────────────────────────────────
_FIRE_BLOCK_KW: frozenset[str] = frozenset(k.upper() for k in (
    "감지기", "DETECTOR", "스프링클러", "SPRINKLER", "소화기", "EXTINGUISHER",
    "소화전", "HYDRANT", "FD", "연기", "SMOKE", "방화댐퍼",
))
_ELEC_BLOCK_KW: frozenset[str] = frozenset(k.upper() for k in (
    "분전반", "PANEL", "차단기", "BREAKER", "콘센트", "OUTLET",
    "스위치", "SWITCH", "전등", "LAMP", "형광등", "조명",
))
_PIPE_BLOCK_KW: frozenset[str] = frozenset(k.upper() for k in (
    "밸브", "VALVE", "펌프", "PUMP", "플랜지", "FLANGE",
    "트랩", "TRAP", "스트레이너", "STRAINER",
))

# ── 도메인별 레이어명 키워드 ───────────────────────────────────────────────
_FIRE_LAYER_KW: tuple[str, ...] = (
    "소방", "FIRE", "SP-", "SP_", "SPRK", "FHC", "FD-", "FD_", "감지",
)
_ELEC_LAYER_KW: tuple[str, ...] = (
    "전기", "ELEC", "EL-", "EL_", "LIGHT", "POWER", "PWR", "LT-", "LT_",
)
_PIPE_LAYER_KW: tuple[str, ...] = (
    "배관", "PIPE", "PLMB", "MECH", "HVAC", "WAT-", "WS-", "GAS-",
)


# ── 피처 메타 ───────────────────────────────────────────────────────────────

_FEATURE_NAMES: list[str] = [
    # [A] 엔티티 타입 비율 (12)
    "ent_line_ratio", "ent_polyline_ratio", "ent_block_ratio", "ent_arc_ratio",
    "ent_text_ratio", "ent_mtext_ratio", "ent_hatch_ratio", "ent_ellipse_ratio",
    "ent_spline_ratio", "ent_circle_ratio", "ent_insert_ratio", "ent_solid_ratio",
    # [B] 이진 존재 여부 (4)
    "has_hatch", "has_ellipse", "has_spline", "has_solid",
    # [C] 파생 엔티티 비율 (4)
    "arc_circle_ratio", "text_mtext_ratio", "insert_block_ratio", "hatch_ellipse_ratio",
    # [D] 수치 통계 (4)
    "log_layer_count", "log_entity_count", "entity_per_layer_norm", "layer_count_norm",
    # [E] drawing_unit 원핫 (4)
    "unit_mm", "unit_inch", "unit_m", "unit_unknown",
    # [F] 레이어명 길이 통계 (4)
    "avg_name_len", "std_name_len", "max_name_len", "min_name_len",
    # [G] 레이어명 스타일 비율 (5) — korean_ratio 제거
    "numeric_ratio", "short_ratio", "upper_ratio", "hyphen_ratio", "special_ratio",
    # [H] 레이어 구조 패턴 (4) — AIA 접두어 대체 (언어 독립)
    "layer_count_per_entity", "wall_closure_ratio", "polyline_vertex_avg", "arc_to_polyline_ratio",
    # [I] 레이어 분포 집중도 (2)
    "max_concentration", "concentration_std",
    # [J] 레이어별 엔티티 통계 (4)
    "avg_ent_per_layer_log", "std_ent_per_layer_log",
    "max_ent_per_layer_log", "min_ent_per_layer_log",
    # [K] 레이어 속성 패턴 (4)
    "off_layer_ratio", "locked_layer_ratio", "not_plottable_ratio", "unique_linetype_norm",
    # [L] 색상 다양성 (2)
    "unique_color_norm", "color_entropy",
    # [M] 오버라이드 비율 (2)
    "color_override_ratio", "linetype_override_ratio",
    # [N] 기하 통계 (2)
    "avg_entity_area_log", "std_entity_area_log",
    # [O] 구조적 밀도 분석 (8)
    "poly_to_line_ratio", "circle_per_layer_norm", "arc_per_layer_norm",
    "polyline_dominance", "entity_type_entropy", "line_arc_ratio",
    "circle_arc_diff", "dim_entity_ratio",
    # [P] 형태 분포 분석 (8)
    "bbox_elongation_mean", "bbox_elongation_std", "zero_area_ratio",
    "small_entity_ratio", "text_per_layer_norm",
    "major_circle_ratio", "major_arc_ratio", "major_insert_ratio",
    # [Q] CIRCLE vs ARC 분리 (2)
    "circle_only_ratio", "arc_only_ratio",
    # [R] 레이어 색상 패턴 (2)
    "red_layer_ratio", "yellow_layer_ratio",
    # [S] INSERT/BLOCK 구조 분석 (3)
    "unique_block_norm", "block_repetition_ratio", "text_to_insert_ratio",
    # [T] 기하 교호작용 (2)
    "circle_dominance", "hatch_to_line_ratio",
    # [U] 엔티티 색상 시그니처 (3) — 신규
    "red_entity_ratio", "yellow_entity_ratio", "cyan_entity_ratio",
    # [V] 블록·해치 비율 (2) — 신규
    "block_to_line_ratio", "hatch_to_total_ratio",
    # [W] 소프트 블록 키워드 매칭 (3) — 신규
    "soft_fire_block_ratio", "soft_elec_block_ratio", "soft_pipe_block_ratio",
    # [X] 레이어명 키워드 매칭 (3) — 신규
    "fire_layer_kw_ratio", "elec_layer_kw_ratio", "pipe_layer_kw_ratio",
    # [Y] 긴 선분 비율 (1) — 신규
    "long_line_ratio",
]


def get_feature_names() -> list[str]:
    """94차원 피처 이름 목록 반환"""
    return list(_FEATURE_NAMES)


def get_feature_dim() -> int:
    """피처 벡터 차원 수 반환"""
    return len(_FEATURE_NAMES)


# ── 문자열 유틸 ─────────────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    return unicodedata.normalize("NFC", text).lower()

def _is_numeric_only(text: str) -> bool:
    return bool(re.fullmatch(r"[\d.\-]+", text.strip()))

def _is_short(text: str, threshold: int = 5) -> bool:
    return len(text.strip()) < threshold

def _is_upper_only(text: str) -> bool:
    alpha = re.sub(r"[^a-zA-Z]", "", text)
    return len(alpha) > 0 and alpha.isupper()

def _has_hyphen(text: str) -> bool:
    return "-" in text

def _has_special_chars(text: str) -> bool:
    return bool(re.search(r"[\$\|/]", text))

def _color_int(e: dict) -> int | None:
    """엔티티 color 필드를 AutoCAD 색상 인덱스(정수)로 변환. 실패 시 None."""
    c = e.get("color")
    try:
        return int(str(c).strip())
    except (TypeError, ValueError):
        return None


# ── 주요 레이어 추출 (entity-weighted) ──────────────────────────────────────

def _get_major_layer_entities(entities: list[dict], threshold: float = 0.5) -> list[dict]:
    """
    엔티티 수 기준 상위 레이어들에 속한 엔티티 목록 반환.
    threshold: 누적 엔티티 비율 기준 (기본 0.5 = 상위 50%)
    언어 무관 — 레이어명 대신 엔티티 수로만 판단.
    """
    if not entities:
        return []

    layer_ent_map: dict[str, list] = {}
    for e in entities:
        ln = str(e.get("layer", "") or "")
        layer_ent_map.setdefault(ln, []).append(e)

    sorted_layers = sorted(layer_ent_map.items(), key=lambda x: len(x[1]), reverse=True)

    total = len(entities)
    target = total * threshold
    cumsum = 0
    major = []
    for _, ents in sorted_layers:
        major.extend(ents)
        cumsum += len(ents)
        if cumsum >= target:
            break

    return major


# ── 메인 피처 추출 ──────────────────────────────────────────────────────────

def extract_features(cad_json: dict) -> np.ndarray:
    """
    CAD JSON → 94차원 피처 벡터.

    Parameters
    ----------
    cad_json : dict
        drawing_unit, layers[], entities[] (또는 elements[]) 포함

    Returns
    -------
    np.ndarray  shape=(94,)  dtype=float32
    """
    layers   = cad_json.get("layers", [])
    # REFACTOR: entities 없으면 elements 폴백 (CAD JSON 필드명 차이 대응)
    entities = cad_json.get("entities")
    if entities is None:
        entities = cad_json.get("elements", [])

    unit_raw = _normalize(str(cad_json.get("drawing_unit", "unknown")))
    unit     = unit_raw if unit_raw in UNIT_LIST else "unknown"

    layer_count  = max(len(layers), 1)
    entity_count = max(len(entities), 1)

    layer_names_raw = [unicodedata.normalize("NFC", str(l.get("name", "") or "")) for l in layers]

    # REFACTOR: Counter 기반 레이어별 엔티티 수 — layer.entity_count 필드 신뢰 불가
    _layer_entity_counts = Counter(str(e.get("layer", "") or "") for e in entities)

    # ── [A] 엔티티 타입 비율 (12차원) ────────────────────────────────────────
    type_counts: dict[str, int] = {t: 0 for t in ENTITY_TYPES}
    for e in entities:
        t = str(e.get("type", "")).upper()
        if t in type_counts:
            type_counts[t] += 1
    feat_A = np.array(
        [type_counts[t] / entity_count for t in ENTITY_TYPES],
        dtype=np.float32,
    )

    # ── 자주 쓰이는 카운트 미리 계산 ────────────────────────────────────────
    line_cnt   = type_counts["LINE"]
    poly_cnt   = type_counts["POLYLINE"]
    arc_cnt    = type_counts["ARC"]
    circle_cnt = type_counts["CIRCLE"]
    insert_cnt = type_counts["INSERT"] + type_counts["BLOCK"]
    text_cnt   = type_counts["TEXT"] + type_counts["MTEXT"]

    # ── [B] 이진 존재 여부 (4차원) ───────────────────────────────────────────
    feat_B = np.array(
        [1.0 if type_counts[t] > 0 else 0.0 for t in BINARY_ENTITY_TYPES],
        dtype=np.float32,
    )

    # ── [C] 파생 엔티티 비율 (4차원) ─────────────────────────────────────────
    feat_C = np.array([
        (arc_cnt    + circle_cnt)                          / entity_count,
        (text_cnt)                                         / entity_count,
        (insert_cnt)                                       / entity_count,
        (type_counts["HATCH"] + type_counts["ELLIPSE"])   / entity_count,
    ], dtype=np.float32)

    # ── [D] 수치 통계 (4차원) ────────────────────────────────────────────────
    feat_D = np.array([
        np.log1p(layer_count),
        np.log1p(entity_count),
        min(entity_count / layer_count / 100.0, 1.0),
        min(layer_count / 50.0, 1.0),
    ], dtype=np.float32)

    # ── [E] drawing_unit 원핫 (4차원) ────────────────────────────────────────
    feat_E = np.array(
        [1.0 if unit == u else 0.0 for u in UNIT_LIST],
        dtype=np.float32,
    )

    # ── [F] 레이어명 길이 통계 (4차원) ───────────────────────────────────────
    lengths = [len(n) for n in layer_names_raw] if layer_names_raw else [0]
    feat_F = np.array([
        np.mean(lengths) / 30.0,
        np.std(lengths)  / 20.0,
        min(np.max(lengths) / 50.0, 1.0),
        np.min(lengths) / 20.0,
    ], dtype=np.float32)

    # ── [G] 레이어명 스타일 비율 (5차원) ─────────────────────────────────────
    # REFACTOR: korean_ratio 제거 — 한국 도면은 도메인 무관 ≈1.0으로 변별력 없음
    feat_G = np.array([
        sum(1 for n in layer_names_raw if _is_numeric_only(n))   / layer_count,
        sum(1 for n in layer_names_raw if _is_short(n))          / layer_count,
        sum(1 for n in layer_names_raw if _is_upper_only(n))     / layer_count,
        sum(1 for n in layer_names_raw if _has_hyphen(n))        / layer_count,
        sum(1 for n in layer_names_raw if _has_special_chars(n)) / layer_count,
    ], dtype=np.float32)

    # ── [H] 레이어 구조 패턴 (4차원) ─────────────────────────────────────────
    # REFACTOR: AIA 접두어 6개 → 언어 독립 레이어 구조 패턴으로 교체
    # 1) layer_count_per_entity : 레이어 수 / 엔티티 수 (도면 복잡도 역비례)
    # 2) wall_closure_ratio     : 닫힌 폴리라인 비율 (건축 벽체 특화)
    # 3) polyline_vertex_avg    : 폴리라인 평균 꼭짓점 수 (파이프:2, 건축벽:4+)
    # 4) arc_to_polyline_ratio  : ARC/POLYLINE (배관 피팅: 높음, 건축: 낮음)
    layer_count_per_entity = min(layer_count / entity_count, 1.0)

    poly_ents = [e for e in entities if str(e.get("type", "")).upper() in ("POLYLINE", "LWPOLYLINE")]
    closed_count = 0
    vertex_counts: list[int] = []
    for e in poly_ents:
        pts = e.get("points") or []
        if pts:
            vertex_counts.append(len(pts))
        if len(pts) >= 2:
            try:
                x0 = float(pts[0].get("x", 0))
                y0 = float(pts[0].get("y", 0))
                xn = float(pts[-1].get("x", 0))
                yn = float(pts[-1].get("y", 0))
                if math.hypot(x0 - xn, y0 - yn) < 1.0:
                    closed_count += 1
            except (TypeError, ValueError, AttributeError):
                pass

    wall_closure_ratio  = closed_count / max(len(poly_ents), 1)
    polyline_vertex_avg = min(float(np.mean(vertex_counts)) / 20.0, 1.0) if vertex_counts else 0.0
    arc_to_polyline     = min(arc_cnt / (poly_cnt + 1e-8), 5.0) / 5.0

    feat_H = np.array([
        layer_count_per_entity,
        wall_closure_ratio,
        polyline_vertex_avg,
        arc_to_polyline,
    ], dtype=np.float32)

    # ── [I] 레이어 분포 집중도 (2차원) ───────────────────────────────────────
    # REFACTOR: layer.entity_count → Counter 기반 직접 계산
    layer_ent_counts = [
        float(_layer_entity_counts.get(str(l.get("name", "") or ""), 0))
        for l in layers
    ]

    if layer_ent_counts and sum(layer_ent_counts) > 0:
        total_lec = sum(layer_ent_counts)
        props = [c / total_lec for c in layer_ent_counts]
        max_conc = max(props)
        conc_std = float(np.std(props))
    else:
        max_conc = 1.0
        conc_std = 0.0
    feat_I = np.array([max_conc, min(conc_std * 10.0, 1.0)], dtype=np.float32)

    # ── [J] 레이어별 엔티티 카운트 통계 (4차원) ──────────────────────────────
    # REFACTOR: layer.entity_count → Counter 기반 직접 계산
    if layer_ent_counts and any(c > 0 for c in layer_ent_counts):
        feat_J = np.array([
            np.log1p(float(np.mean(layer_ent_counts))) / 10.0,
            np.log1p(float(np.std(layer_ent_counts)) if len(layer_ent_counts) > 1 else 0.0) / 10.0,
            np.log1p(float(np.max(layer_ent_counts))) / 10.0,
            np.log1p(float(np.min(layer_ent_counts))) / 10.0,
        ], dtype=np.float32)
    else:
        feat_J = np.array([
            np.log1p(entity_count) / 10.0, 0.0,
            np.log1p(entity_count) / 10.0, 0.0,
        ], dtype=np.float32)

    # ── [K] 레이어 속성 패턴 (4차원) ─────────────────────────────────────────
    off_count      = sum(1 for l in layers if not l.get("is_on", True))
    locked_count   = sum(1 for l in layers if l.get("is_locked", False))
    not_plot_count = sum(1 for l in layers if not l.get("is_plottable", True))
    linetypes      = set(l.get("linetype", "") or "" for l in layers)
    linetypes.discard("")
    unique_lt_norm = min(len(linetypes) / layer_count, 1.0)

    feat_K = np.array([
        off_count      / layer_count,
        locked_count   / layer_count,
        not_plot_count / layer_count,
        unique_lt_norm,
    ], dtype=np.float32)

    # ── [L] 레이어 색상 다양성 (2차원) ───────────────────────────────────────
    colors = [str(l.get("color", "")).strip() for l in layers]
    colors = [c for c in colors if c and c not in ("", "None")]
    unique_colors     = len(set(colors))
    unique_color_norm = min(unique_colors / layer_count, 1.0)

    if colors:
        color_counter = Counter(colors)
        total_c = len(colors)
        probs = [v / total_c for v in color_counter.values()]
        entropy = -sum(p * math.log2(p) for p in probs if p > 0)
        entropy_norm = min(entropy / 4.0, 1.0)
    else:
        entropy_norm = 0.0

    feat_L = np.array([unique_color_norm, entropy_norm], dtype=np.float32)

    # ── [M] 엔티티 색상·선종 오버라이드 비율 (2차원) ─────────────────────────
    color_override = sum(
        1 for e in entities
        if str(e.get("color", "BYLAYER")).upper() not in ("BYLAYER", "BYBLOCK", "")
    )
    lt_override = sum(
        1 for e in entities
        if e.get("linetype") and str(e.get("linetype", "")).upper()
           not in ("BYLAYER", "BYBLOCK", "CONTINUOUS", "")
    )
    feat_M = np.array([
        color_override / entity_count,
        lt_override    / entity_count,
    ], dtype=np.float32)

    # ── [N] 기하 통계 — bbox 면적 (2차원) ────────────────────────────────────
    areas = []
    for e in entities:
        bb = e.get("bbox")
        if bb:
            try:
                w = abs(float(bb.get("x2", 0) or 0) - float(bb.get("x1", 0) or 0))
                h = abs(float(bb.get("y2", 0) or 0) - float(bb.get("y1", 0) or 0))
                a = w * h
                if np.isfinite(a):
                    areas.append(a)
            except (TypeError, ValueError):
                pass

    if areas:
        avg_area = float(np.mean(areas))
        std_area = float(np.std(areas)) if len(areas) > 1 else 0.0
        feat_N = np.array([
            np.log1p(avg_area) / 20.0,
            np.log1p(std_area) / 20.0,
        ], dtype=np.float32)
    else:
        feat_N = np.array([0.0, 0.0], dtype=np.float32)

    # ── [O] 구조적 밀도 분석 (8차원) ─────────────────────────────────────────
    poly_to_line       = poly_cnt / (line_cnt + poly_cnt + 1e-8)
    circle_per_layer   = min(circle_cnt / layer_count / 20.0, 1.0)
    arc_per_layer      = min(arc_cnt    / layer_count / 20.0, 1.0)
    polyline_dominance = poly_cnt / (poly_cnt + arc_cnt + 1e-8)

    type_probs = [type_counts[t] / entity_count for t in ENTITY_TYPES if type_counts[t] > 0]
    ent_entropy = -sum(p * math.log2(p) for p in type_probs if p > 0) if type_probs else 0.0
    max_entropy = math.log2(len(ENTITY_TYPES))
    ent_entropy_norm = min(ent_entropy / max_entropy, 1.0) if max_entropy > 0 else 0.0

    line_arc_ratio  = line_cnt / (arc_cnt + line_cnt + 1e-8)
    circle_arc_diff = (circle_cnt - arc_cnt) / (circle_cnt + arc_cnt + 1e-8)

    dim_cnt = sum(1 for e in entities if str(e.get("type", "")).upper() == "DIMENSION")
    dim_entity_ratio = dim_cnt / entity_count

    feat_O = np.array([
        poly_to_line, circle_per_layer, arc_per_layer,
        polyline_dominance, ent_entropy_norm, line_arc_ratio,
        circle_arc_diff, dim_entity_ratio,
    ], dtype=np.float32)

    # ── [P] 형태 분포 분석 (8차원) ───────────────────────────────────────────
    elongations    = []
    zero_area_count = 0
    for e in entities:
        bb = e.get("bbox")
        if bb:
            try:
                w = abs(float(bb.get("x2", 0) or 0) - float(bb.get("x1", 0) or 0))
                h = abs(float(bb.get("y2", 0) or 0) - float(bb.get("y1", 0) or 0))
                a = w * h
                if not np.isfinite(a):
                    continue
                if a < 1e-6:
                    zero_area_count += 1
                else:
                    ratio = max(w, h) / (min(w, h) + 1e-8)
                    if np.isfinite(ratio):
                        elongations.append(min(ratio, 100.0))
            except (TypeError, ValueError):
                pass

    elong_mean = float(np.mean(elongations)) / 100.0 if elongations else 0.0
    elong_std  = float(np.std(elongations))  / 100.0 if elongations else 0.0
    zero_area_ratio = zero_area_count / entity_count

    if areas:
        median_area = float(np.median(areas))
        small_entity_ratio = sum(1 for a in areas if a <= median_area) / len(areas)
    else:
        small_entity_ratio = 0.5

    text_per_layer = min(text_cnt / layer_count / 20.0, 1.0)

    major_ents = _get_major_layer_entities(entities, threshold=0.5)
    major_circle = sum(1 for e in major_ents if str(e.get("type", "")).upper() == "CIRCLE")
    major_arc    = sum(1 for e in major_ents if str(e.get("type", "")).upper() == "ARC")
    major_insert = sum(1 for e in major_ents if str(e.get("type", "")).upper() in ("INSERT", "BLOCK"))

    feat_P = np.array([
        min(elong_mean, 1.0),
        min(elong_std,  1.0),
        zero_area_ratio,
        small_entity_ratio,
        text_per_layer,
        min(major_circle / (circle_cnt + 1e-8), 1.0),
        min(major_arc    / (arc_cnt    + 1e-8), 1.0),
        min(major_insert / (insert_cnt + 1e-8), 1.0),
    ], dtype=np.float32)

    # ── [Q] CIRCLE vs ARC 분리 (2차원) ──────────────────────────────────────
    feat_Q = np.array([
        circle_cnt / entity_count,
        arc_cnt    / entity_count,
    ], dtype=np.float32)

    # ── [R] 레이어 색상 패턴 (2차원) ─────────────────────────────────────────
    layer_colors = []
    for l in layers:
        try:
            layer_colors.append(int(str(l.get("color", "")).strip()))
        except (ValueError, TypeError):
            pass

    total_layc = max(len(layer_colors), 1)
    feat_R = np.array([
        sum(1 for c in layer_colors if c == 1)          / total_layc,  # 소방 빨강
        sum(1 for c in layer_colors if c in (2, 3))     / total_layc,  # 전기 노랑
    ], dtype=np.float32)

    # ── [S] INSERT/BLOCK 구조 분석 (3차원) ───────────────────────────────────
    block_names = []
    for e in entities:
        if str(e.get("type", "")).upper() in ("BLOCK", "INSERT"):
            bn = str(e.get("block_name", "") or "").strip()
            if bn:
                block_names.append(bn.upper())

    n_blocks = max(len(block_names), 1)
    unique_blocks = len(set(block_names)) if block_names else 0

    if block_names:
        most_common_count = Counter(block_names).most_common(1)[0][1]
        block_rep_ratio   = most_common_count / n_blocks
    else:
        block_rep_ratio = 0.0

    feat_S = np.array([
        min(unique_blocks / math.sqrt(n_blocks), 5.0) / 5.0,
        block_rep_ratio,
        min(text_cnt / (insert_cnt + 1), 10.0) / 10.0,
    ], dtype=np.float32)

    # ── [T] 기하 교호작용 비율 (2차원) ───────────────────────────────────────
    feat_T = np.array([
        circle_cnt / (circle_cnt + arc_cnt + 1e-8),          # circle_dominance
        min(type_counts["HATCH"] / (line_cnt + 1e-8), 1.0),  # hatch_to_line_ratio
    ], dtype=np.float32)

    # ── [U] 엔티티 색상 시그니처 (3차원) — 신규 ──────────────────────────────
    # 레이어 색상이 아닌 엔티티에 직접 지정된 색상 (BYLAYER 제외)
    red_cnt = yellow_cnt = cyan_cnt = 0
    for e in entities:
        ci = _color_int(e)
        if ci is None:
            continue
        if ci in _RED_COLORS:
            red_cnt += 1
        if ci in _YELLOW_COLORS:
            yellow_cnt += 1
        if ci in _CYAN_COLORS:
            cyan_cnt += 1

    feat_U = np.array([
        red_cnt    / entity_count,
        yellow_cnt / entity_count,
        cyan_cnt   / entity_count,
    ], dtype=np.float32)

    # ── [V] 블록·해치 비율 (2차원) — 신규 ────────────────────────────────────
    # 1) block_to_line_ratio : 블록 수 / LINE 수 (MEP: 높음, 건축: 낮음)
    # 2) hatch_to_total_ratio: HATCH / 전체 (건축 평면도 단면 해칭: 높음)
    feat_V = np.array([
        min(insert_cnt / (line_cnt + 1e-8), 1.0),
        type_counts["HATCH"] / entity_count,
    ], dtype=np.float32)

    # ── [W] 소프트 블록 키워드 매칭 (3차원) ────────────────────────────────────
    def _kw_match_ratio(names: list[str], keywords: frozenset[str]) -> float:
        if not names:
            return 0.0
        hits = sum(1 for bn in names if any(kw in bn for kw in keywords))
        return hits / len(names)

    feat_W = np.array([
        _kw_match_ratio(block_names, _FIRE_BLOCK_KW),
        _kw_match_ratio(block_names, _ELEC_BLOCK_KW),
        _kw_match_ratio(block_names, _PIPE_BLOCK_KW),
    ], dtype=np.float32)

    # ── [X] 레이어명 키워드 매칭 (3차원) ────────────────────────────────────────
    layer_names_upper = [str(l.get("name", "") or "").upper() for l in layers]

    def _layer_kw_ratio(names: list[str], keywords: tuple[str, ...]) -> float:
        hits = sum(1 for n in names if any(kw in n for kw in keywords))
        return hits / max(len(names), 1)

    feat_X = np.array([
        _layer_kw_ratio(layer_names_upper, _FIRE_LAYER_KW),
        _layer_kw_ratio(layer_names_upper, _ELEC_LAYER_KW),
        _layer_kw_ratio(layer_names_upper, _PIPE_LAYER_KW),
    ], dtype=np.float32)

    # ── [Y] 긴 선분 비율 (1차원) ────────────────────────────────────────────────
    line_lengths_y: list[float] = []
    for e in entities:
        if str(e.get("type", "") or "").upper() == "LINE":
            bb = e.get("bbox")
            if bb:
                try:
                    w = abs(float(bb.get("x2", 0) or 0) - float(bb.get("x1", 0) or 0))
                    h = abs(float(bb.get("y2", 0) or 0) - float(bb.get("y1", 0) or 0))
                    length = math.hypot(w, h)
                    if np.isfinite(length):
                        line_lengths_y.append(length)
                except (TypeError, ValueError):
                    pass

    if line_lengths_y and len(line_lengths_y) > 1:
        median_len = float(np.median(line_lengths_y))
        long_count  = sum(1 for l in line_lengths_y if l > median_len * 2.0)
        long_line_r = long_count / len(line_lengths_y)
    else:
        long_line_r = 0.0

    feat_Y = np.array([long_line_r], dtype=np.float32)

    # ── 최종 결합 (12+4+4+4+4+4+5+4+2+4+4+2+2+2+8+8+2+2+3+2+3+2+3+3+1 = 94차원) ──
    return np.concatenate([
        feat_A,   # 12  엔티티 타입 비율
        feat_B,   # 4   이진 존재 여부
        feat_C,   # 4   파생 엔티티 비율
        feat_D,   # 4   수치 통계
        feat_E,   # 4   drawing_unit
        feat_F,   # 4   레이어명 길이
        feat_G,   # 5   레이어명 스타일 (korean_ratio 제거)
        feat_H,   # 4   레이어 구조 패턴 (AIA 대체)
        feat_I,   # 2   레이어 분포 집중도
        feat_J,   # 4   레이어별 엔티티 통계
        feat_K,   # 4   레이어 속성 패턴
        feat_L,   # 2   색상 다양성
        feat_M,   # 2   오버라이드 비율
        feat_N,   # 2   기하 통계
        feat_O,   # 8   구조적 밀도 분석
        feat_P,   # 8   형태 분포 분석
        feat_Q,   # 2   CIRCLE vs ARC 분리
        feat_R,   # 2   레이어 색상 패턴
        feat_S,   # 3   INSERT/BLOCK 구조 분석
        feat_T,   # 2   기하 교호작용
        feat_U,   # 3   엔티티 색상 시그니처 (신규)
        feat_V,   # 2   블록·해치 비율 (신규)
        feat_W,   # 3   소프트 블록 키워드 매칭 (신규)
        feat_X,   # 3   레이어명 키워드 매칭 (신규)
        feat_Y,   # 1   긴 선분 비율 (신규)
    ])
