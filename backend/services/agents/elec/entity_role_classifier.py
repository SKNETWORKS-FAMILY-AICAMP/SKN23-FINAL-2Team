"""
entity_role_classifier.py
전기 도면 엔티티 역할 5분류기 (score 기반)

ELECTRIC_CORE    : 전기 심볼·배선·주석 → 위반검토·수정 대상
ELECTRIC_CONTEXT : 트레이 경로·맨홀·심볼 형상 등 → topology 보조, LLM 전달
ARCH_REFERENCE   : 건축 벽체·문·치수·DW 보조선 → 공간 거리 참조에만 사용 (LLM 미전달)
DRAWING_FORM     : 도곽·타이틀블록·주기표 → 완전 제외
NOISE            : 빈 텍스트·잡선 → 분석 제외 (CAD 삭제는 승인 전 절대 금지)

분류 근거 (도면 실측 기반):
    DW  438건 → 치수/보조선 → ARCH_REFERENCE
    TEXT 82건 → 내용별 score → CORE(접지/FGV) or CONTEXT(회로번호) or ARCH
    w   73건 → wire/핸드홀 → linetype+proximity 조건 시 CORE, 아니면 CONTEXT
                 (레이어명 단독으로 STRONG 처리 금지 — 다른 도면에서 wall 레이어일 수 있음)
    0   36건 → CIRCLE(심볼마커)/BLOCK(form) → CONTEXT or FORM
    SYM 22건 → 전기심볼 형상 → ELECTRIC_CONTEXT
    TRAY 18건 → 케이블 트레이 → ELECTRIC_CORE
    AISS 16건 → 프로젝트 전기 레이어 → ELECTRIC_CORE
    LID 13건  → 맨홀/덕트 덮개 → ELECTRIC_CONTEXT
    접지  7건 → 접지 도면 레이어 → ELECTRIC_CORE
    form  6건 → 도곽 텍스트 → DRAWING_FORM
"""
from __future__ import annotations

import logging
import re
from typing import NamedTuple

logger = logging.getLogger(__name__)

# ── 분류 레이블 ────────────────────────────────────────────────────────────────
ELECTRIC_CORE    = "ELECTRIC_CORE"
ELECTRIC_CONTEXT = "ELECTRIC_CONTEXT"
ARCH_REFERENCE   = "ARCH_REFERENCE"
DRAWING_FORM     = "DRAWING_FORM"
NOISE            = "NOISE"

ALL_ROLES = (ELECTRIC_CORE, ELECTRIC_CONTEXT, ARCH_REFERENCE, DRAWING_FORM, NOISE)

# ── 점수 임계값 ────────────────────────────────────────────────────────────────
_CORE_THRESHOLD    = 5   # score ≥ 5 → ELECTRIC_CORE
_CONTEXT_THRESHOLD = 2   # score ≥ 2 → ELECTRIC_CONTEXT
                         # score < 2 → ARCH_REFERENCE / 타입별 fallback

# ── 패턴 ──────────────────────────────────────────────────────────────────────

# 레이어명 전기 신호 (강 +5 / 중 +3)
# W는 제외 — 레이어명 단독으로 STRONG 처리 시 다른 도면에서 wall 레이어와 오탐 가능
_ELEC_LAYER_STRONG = re.compile(
    r"^(TRAY|CABLE|접지|GROUND|EARTH|전기|전등|전열|분전|배선|"
    r"ELEC|ELECT|EL|LIGHT|LGT|POWER|PWR|CONCENT|OUTLET|SOCKET|RECEPT|"
    r"PANEL|PNL|MCC|MCCB|ELCB|BREAKER|CONDUIT|WIRE|GND|"
    r"AISS.*|E[-_].*|e)$",
    re.IGNORECASE,
)
_ELEC_LAYER_MEDIUM = re.compile(
    r"^(SYM|LID)$",   # SYM=심볼형상, LID=맨홀덮개
    re.IGNORECASE,
)

# W 레이어 전용: 전기 경로 linetype이 있을 때만 CORE 승격
# 다른 도면에서 W=wall 일 수 있으므로 레이어명 단독 판단 금지
_W_ELEC_PATH_LINETYPE = re.compile(
    r"(바닥배관|지중매설|G2|F3|CONDUIT|CABLE|TRAY|UNDERGROUND|DUCT)",
    re.IGNORECASE,
)
_W_ELEC_BLOCK_HINT = re.compile(
    r"(_HA\b|_box|핸드홀|맨홀|MANHOLE|HANDHOLE|TRAY|WIRE|접지|GND)",
    re.IGNORECASE,
)

# 레이어명 비전기 신호 (DW=치수보조선 하드룰, ARCH 패턴 등)
_DW_LAYER = re.compile(r"^DW$", re.IGNORECASE)
_ARCH_LAYER = re.compile(
    r"^(ARCH|A-|WALL|DOOR|WINDOW|ROOM|FLOOR|GRID|AXIS|CENTER|CEN|벽|문|창|건축|평면).*",
    re.IGNORECASE,
)

# 도곽/타이틀 패턴
_FORM_LAYER = re.compile(r"(도곽|TITLE|BORDER|FRAME|DEFPOINTS|^TB$|^form$)", re.IGNORECASE)
_FORM_BLOCK = re.compile(
    r"^(FORM|TITLE|TITLEBLK|TITLEBLOCK|TITLE_BLOCK|BORDER|FRAME|도곽|타이틀).*",
    re.IGNORECASE,
)

# 블록명 전기 신호 (강 +5 / 중 +3)
_ELEC_BLOCK_STRONG = re.compile(
    r"(접지|GND|GROUND|CABLE|TRAY|PANEL|MCCB|ELCB|콘센트|RECEPT|스위치|SWITCH|"
    r"분전|수변전|FINEENC|접지봉|접지도체|접지선)",
    re.IGNORECASE,
)
_ELEC_BLOCK_MEDIUM = re.compile(
    r"(_HA\b|_box|핸드홀|맨홀|MANHOLE|HANDHOLE|심볼|SYM|ELEC)",
    re.IGNORECASE,
)

# 텍스트 전기 신호 (강 +5 / 중 +3)
_ELEC_TEXT_STRONG = re.compile(
    r"(FGV|GROUNDING\s*ROD|"
    r"접지\s*(공사|이격|선|봉|저항|도체|설비|시공)|"
    r"이격\s*거리|분전반|수변전|맨홀|핸드홀|"
    r"CABLE\s*(TRAY|DUCT)|케이블\s*(트레이|덕트)|"
    r"전력|전등\s*설비|접지\s*설비|EPS|MCC|MCCB|ELCB|"
    r"[0-9]+\s*(SQ|MM2|MM²)|"
    r"[0-9]+\s*[Pp]\b|[0-9]+\s*[Cc]\b(?!.*[Aa-zZ])|"
    r"중성선\s*공사|[0-9]+회로\s*공사)",
    re.IGNORECASE,
)
_ELEC_TEXT_MEDIUM = re.compile(
    r"(\bTRAY\b|\bDUCT\b|\bCABLE\b|GR\b|TR\b|"
    r"\bE[0-9]+\b|\b[0-9]+C\b|PANEL|SCHEDULE|"
    r"\bkW\b|\bAF\b|\bAT\b|\bMCCB\b|회로|배선|전선|접지)",
    re.IGNORECASE,
)

# 노이즈 텍스트 헤더
_NOISE_HEADERS = ("DATA TABLE", "ANGLE SET DATA TABLE", "ENGINEER MODE")


# ── 결과 타입 ──────────────────────────────────────────────────────────────────
class RoleResult(NamedTuple):
    role: str
    score: int
    signals: list[str]


# ── 메인 분류 함수 ─────────────────────────────────────────────────────────────

def classify_entity_role(el: dict, domain_tags: dict) -> RoleResult:
    """단일 엔티티 역할을 5분류로 판별합니다.

    판별 순서:
      1. DRAWING_FORM (도곽/타이틀 하드룰)
      2. NOISE        (빈 텍스트, 잡선)
      3. ARCH_REFERENCE 하드룰 (DW 레이어, DIMENSION 타입, ARCH 레이어)
      4. domain_tags 기반 분류
      5. 레이어명 score
      6. 블록명 score
      7. 텍스트 score
      8. 전기 속성 score
      9. score 임계값 → 최종 역할
    """
    etype      = str(el.get("type") or el.get("raw_type") or "").upper()
    layer_raw  = str(el.get("layer") or "")
    layer      = layer_raw.upper()
    block_raw  = str(el.get("block_name") or el.get("effective_name") or el.get("name") or "")
    block_name = block_raw.upper()
    text = str(
        el.get("text") or el.get("content") or
        (el.get("attributes") or {}).get("TEXT") or ""
    ).strip()

    signals: list[str] = []
    score = 0

    # ── 1. DRAWING_FORM ───────────────────────────────────────────────────────
    if _FORM_LAYER.search(layer_raw) or _FORM_BLOCK.match(block_raw):
        return RoleResult(DRAWING_FORM, 0, ["form_pattern"])
    if layer_raw.lower() == "form":
        return RoleResult(DRAWING_FORM, 0, ["form_layer"])

    # ── 2. NOISE ──────────────────────────────────────────────────────────────
    if etype in ("TEXT", "MTEXT"):
        if not text:
            return RoleResult(NOISE, 0, ["empty_text"])
        if any(k in text.upper() for k in _NOISE_HEADERS):
            return RoleResult(NOISE, 0, ["noise_header"])
    if etype == "LINE":
        try:
            if float(el.get("length") or 0) < 10.0:
                return RoleResult(NOISE, 0, ["short_line"])
        except (TypeError, ValueError):
            pass
    if etype in ("POLYLINE", "LWPOLYLINE"):
        try:
            if float(el.get("length") or 0) > 100_000.0:
                return RoleResult(NOISE, 0, ["border_polyline"])
        except (TypeError, ValueError):
            pass

    explicit_elec_hint = (
        bool(_ELEC_BLOCK_STRONG.search(block_raw))
        or bool(_ELEC_TEXT_STRONG.search(text))
        or bool(_ELEC_TEXT_MEDIUM.search(text))
    )

    # ── 3. ARCH_REFERENCE 하드룰 ─────────────────────────────────────────────
    if _DW_LAYER.match(layer) and explicit_elec_hint:
        return RoleResult(ELECTRIC_CORE, 5, ["dw_layer_electric_label"])
    if _DW_LAYER.match(layer):
        return RoleResult(ARCH_REFERENCE, -1, ["dw_layer"])
    if etype == "DIMENSION":
        return RoleResult(ARCH_REFERENCE, -1, ["dimension_type"])
    if etype == "HATCH":
        return RoleResult(ARCH_REFERENCE, -1, ["hatch_type"])
    if _ARCH_LAYER.match(layer):
        return RoleResult(ARCH_REFERENCE, -1, ["arch_layer"])

    # ── 4. domain_tags ────────────────────────────────────────────────────────
    tag = domain_tags.get(layer) or domain_tags.get(block_name)
    if tag in {"elec", "electric"}:
        signals.append(f"domain_tag={tag}")
        score += 5
    elif tag == "common":
        signals.append("domain_tag=common")
        score += 2
    elif tag:
        return RoleResult(ARCH_REFERENCE, -1, [f"domain_tag={tag}"])

    # ── 5. 레이어명 score ─────────────────────────────────────────────────────
    if layer == "W":
        # W 레이어: 레이어명 단독 STRONG 금지 — linetype·block으로 실체 확인 후 CORE 승격
        linetype = str(el.get("linetype") or "")
        if _W_ELEC_PATH_LINETYPE.search(linetype):
            signals.append(f"w_path_linetype={linetype[:20]}")
            score += 5  # 전기 경로 linetype 확인 → CORE
        elif _W_ELEC_BLOCK_HINT.search(block_raw):
            signals.append(f"w_block_hint={block_raw[:20]}")
            score += 5  # 전기 블록 힌트 확인 → CORE
        else:
            signals.append("w_no_signal")
            score -= 1  # 전기 근거 없는 W → ARCH_REFERENCE로 내림
    elif _ELEC_LAYER_STRONG.match(layer):
        signals.append(f"layer_strong={layer}")
        score += 5
    elif _ELEC_LAYER_MEDIUM.match(layer):
        signals.append(f"layer_medium={layer}")
        score += 3

    # ── 6. 블록명 score ───────────────────────────────────────────────────────
    if block_name:
        if _ELEC_BLOCK_STRONG.search(block_name):
            signals.append(f"block_strong={block_raw[:20]}")
            score += 5
        elif _ELEC_BLOCK_MEDIUM.search(block_name):
            signals.append(f"block_medium={block_raw[:20]}")
            score += 3

    # ── 7. 텍스트 score ───────────────────────────────────────────────────────
    if text:
        if _ELEC_TEXT_STRONG.search(text):
            signals.append(f"text_strong={text[:30]}")
            score += 5
        elif _ELEC_TEXT_MEDIUM.search(text):
            signals.append(f"text_medium={text[:30]}")
            score += 3

    # ── 8. 전기 속성 score ────────────────────────────────────────────────────
    if el.get("voltage") or el.get("sqmm") or el.get("cable_sqmm"):
        signals.append("elec_attr")
        score += 4

    # ── 9. 점수 기반 최종 분류 ────────────────────────────────────────────────
    if score >= _CORE_THRESHOLD:
        return RoleResult(ELECTRIC_CORE, score, signals or ["score≥5"])
    if score >= _CONTEXT_THRESHOLD:
        return RoleResult(ELECTRIC_CONTEXT, score, signals or ["score≥2"])

    # 점수 없음 → 타입별 fallback
    if etype == "CIRCLE":
        # layer 0의 원은 접지 마커나 심볼 일 가능성 (공간 오염 없음)
        return RoleResult(ELECTRIC_CONTEXT, 1, ["circle_unclassified"])
    if etype in ("ARC", "ELLIPSE"):
        return RoleResult(ELECTRIC_CONTEXT, 1, ["arc_unclassified"])
    if etype in ("INSERT", "BLOCK"):
        return RoleResult(ELECTRIC_CONTEXT, 1, ["block_unclassified"])
    if etype in ("TEXT", "MTEXT"):
        # 점수 없는 텍스트는 건축 주기/범례
        return RoleResult(ARCH_REFERENCE, 0, ["text_no_signal"])
    # LINE / POLYLINE → 건축 참조선
    return RoleResult(ARCH_REFERENCE, 0, ["geometry_no_signal"])


# ── 배치 분류 ──────────────────────────────────────────────────────────────────

def classify_all_entities(
    entities: list[dict],
    domain_tags: dict,
) -> tuple[dict[str, list[dict]], dict[str, int]]:
    """모든 엔티티를 분류하고 각 엔티티에 role 태그를 in-place로 붙입니다.

    Returns:
        buckets       : {ELECTRIC_CORE: [...], ELECTRIC_CONTEXT: [...], ...}
        signal_counts : {"{ROLE}:{signal_key}": count, ...}  ← 디버그 로그용
    """
    buckets: dict[str, list[dict]] = {role: [] for role in ALL_ROLES}
    signal_counts: dict[str, int] = {}

    for el in entities:
        result = classify_entity_role(el, domain_tags)
        el["electric_review_scope"] = result.role
        el["role_score"]            = result.score
        el["role_signals"]          = result.signals
        el["cad_delete_candidate"]  = False   # CAD 원본 삭제 승인 전 절대 금지
        buckets[result.role].append(el)

        # 신호 유형별 집계
        for sig in result.signals:
            key = f"{result.role}:{sig.split('=')[0]}"
            signal_counts[key] = signal_counts.get(key, 0) + 1

    return buckets, signal_counts


def build_scope_log(
    buckets: dict[str, list],
    signal_counts: dict[str, int],
    total: int,
) -> str:
    """분류 결과를 사람이 읽기 좋은 로그 문자열로 반환합니다."""

    def _sig(role: str, key: str) -> int:
        return signal_counts.get(f"{role}:{key}", 0)

    lines = ["[ELECTRIC ROLE]"]

    n_core = len(buckets[ELECTRIC_CORE])
    core_layer   = _sig(ELECTRIC_CORE, "layer_strong") + _sig(ELECTRIC_CORE, "layer_medium")
    core_block   = _sig(ELECTRIC_CORE, "block_strong") + _sig(ELECTRIC_CORE, "block_medium")
    core_text    = _sig(ELECTRIC_CORE, "text_strong")  + _sig(ELECTRIC_CORE, "text_medium")
    core_tag     = _sig(ELECTRIC_CORE, "domain_tag")
    core_w_lt    = _sig(ELECTRIC_CORE,    "w_path_linetype")
    core_w_blk   = _sig(ELECTRIC_CORE,    "w_block_hint")
    ctx_w_near   = _sig(ELECTRIC_CONTEXT, "w_near_electric")
    arch_w_nosig = _sig(ARCH_REFERENCE,   "w_no_signal")

    # CORE 건수는 엔티티 단위 unique 합계 (신호별 합산은 중복 포함될 수 있음)
    lines.append(f"  CORE(unique)={n_core}")
    lines.append(f"    by_signal:")
    lines.append(f"      layer={core_layer}")
    lines.append(f"      block={core_block}")
    lines.append(f"      text={core_text}")
    lines.append(f"      domain_tag={core_tag}")
    lines.append(f"      w_linetype={core_w_lt}")
    lines.append(f"      w_block={core_w_blk}")
    lines.append(f"  CONTEXT(w_near)={ctx_w_near}  ARCH(w_no_signal)={arch_w_nosig}")
    lines.append(f"  ELECTRIC_CONTEXT={len(buckets[ELECTRIC_CONTEXT])}건")
    lines.append(f"  ARCH_REFERENCE  ={len(buckets[ARCH_REFERENCE])}건")
    lines.append(f"  DRAWING_FORM    ={len(buckets[DRAWING_FORM])}건")
    lines.append(f"  NOISE           ={len(buckets[NOISE])}건")
    lines.append(
        f"  전체 {total}건 / LLM전달 {n_core + len(buckets[ELECTRIC_CONTEXT])}건"
        f" (ARCH/FORM/NOISE {total - n_core - len(buckets[ELECTRIC_CONTEXT])}건 제외)"
    )
    return "\n".join(lines)
