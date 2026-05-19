"""
File    : backend/services/agents/elec/elec_layer_split.py
Author  : 김지우
Create  : 2026-05-04
Description :
  전기 도면에서 건축 배경(arch)과 전기 설비(elec)를 분리하는 전처리기.
  arch_pipe_layer_split.py의 전기 도메인 버전.

  처리 흐름:
    1. 레이어명 기반 분류  (E-/EL-/LT- 등 → elec / A-/S- 등 → arch)
    2. unknown 레이어 휴리스틱 분류 (색상, 블록 비율 등)
    3. 엔티티 단위 분리 → arch_reference / elec_review / aux 버킷
    4. 공간 힌트(elec 기기와 가장 가까운 실명 텍스트) 생성

  출력 스키마 (elec_split_v1):
    schema            : "elec_split_v1"
    arch_reference    : 건축 배경 엔티티 (AI 참조용)
    elec_review       : 전기 검토 대상 엔티티
    aux_skipped       : 표제란·치수·주석 등 제외 항목
    layer_roles       : {레이어명: "arch"|"elec"|"aux"|"unknown"}
    spatial_hints     : 전기기기 ↔ 인접 공간명 매핑 (토큰 절감)
    layer_role_stats  : 통계
"""
from __future__ import annotations

import logging
import math
import re
from typing import Any, Literal

_log = logging.getLogger(__name__)

LayerRole = Literal["arch", "elec", "aux", "unknown"]

# ── 레이어 패턴 ───────────────────────────────────────────────────────────────
_ARCH_PREFIX_RE = re.compile(r"^(A-|S-|AR-|ST-|C-|L-)", re.IGNORECASE)
_ELEC_PREFIX_RE = re.compile(r"^(E-|EL-|LT-|PWR-|GND-|EM-|EX-|FD-)", re.IGNORECASE)

_ELEC_HINT_RE = re.compile(
    r"(ELEC|LIGHT|LIGHTING|POWER|PANEL|CIRCUIT|WIRING|CABLE|CONDUIT|SWITCH|OUTLET|"
    r"SOCKET|BREAKER|GROUNDING|NEUTRAL|전기|조명|전원|배선|분전|회로|스위치|콘센트|"
    r"차단기|접지|케이블|전선|전등|비상|EMERGENCY|EXIT|UPS|EPS|배전)",
    re.IGNORECASE,
)
_ARCH_HINT_RE = re.compile(
    r"(WALL|SLAB|COL|BEAM|DOOR|WINDOW|ROOM|STAIR|CEIL|FINISH|FURN|"
    r"벽체|슬라브|기둥|보|문|창문|실명|계단|천장|마감|가구)",
    re.IGNORECASE,
)
_TITLE_HINT_RE = re.compile(
    r"(TITLE|FRAME|BORDER|표제|도면명|SCALE|DWG|DATE|PROJECT|설계|축척|도면번호)",
    re.IGNORECASE,
)
_IGNORE_LAYER_SET: frozenset[str] = frozenset({
    "DEFPOINTS", "DIM", "DIMENSION", "DIMS", "CEN", "CENTER",
    "HAT", "HATCH", "ANNO", "ANNOTATION", "TITLE", "TITLEBLOCK",
    "FRAME", "BORDER", "GRID", "VIEWPORT", "VPORT", "XREF",
    "AI_REVIEW", "AI_RESULT", "AI_CLOUD", "AI_PROPOSAL",
    "NOTE", "NOTES", "LABEL", "SECTION", "DETAIL",
})
_TITLE_TEXT_RE = re.compile(
    r"평면도|계통도|단면도|전개도|SCALE|DATE|도면번호|DWG\s*NO|PROJECT|도면명|축척",
    re.IGNORECASE,
)
_ROOM_LABEL_RE = re.compile(
    r"거실|방\d*|침실|안방|주방|식당|욕실|화장실|현관|복도|계단|발코니|베란다|"
    r"다용도실|보일러실|세탁실|창고|드레스룸|팬트리|홀|로비|사무실|회의실|기계실|전기실",
    re.IGNORECASE,
)
_ARCH_ACI_COLORS: frozenset[str] = frozenset({"8", "9", "250", "251", "252", "253", "254", "255"})

# 공간 힌트 최대 쌍 수 (토큰 절감)
_SPATIAL_HINTS_MAX = 30

# ── 레이어 표준화 매핑 ─────────────────────────────────────────────────────────
# (regex, 표준 레이어명) 순서대로 매칭 — 먼저 일치하는 규칙 적용
STANDARD_LAYER_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"Cable[_\s]?1\.5",          re.IGNORECASE), "Cable_1.5SQ"),
    (re.compile(r"Cable[_\s]?2\.5",          re.IGNORECASE), "Cable_2.5SQ"),
    (re.compile(r"Cable[_\s]?4\.0",          re.IGNORECASE), "Cable_4.0SQ"),
    (re.compile(r"Cable[_\s]?6\.0",          re.IGNORECASE), "Cable_6.0SQ"),
    (re.compile(r"Cable[_\s]?10",            re.IGNORECASE), "Cable_10SQ"),
    (re.compile(r"Cable[_\s]?16",            re.IGNORECASE), "Cable_16SQ"),
    (re.compile(r"WIRE[_\s]?1\.5|W1\.5",    re.IGNORECASE), "Cable_1.5SQ"),
    (re.compile(r"WIRE[_\s]?2\.5|W2\.5",    re.IGNORECASE), "Cable_2.5SQ"),
    (re.compile(r"WIRE[_\s]?4|W4",          re.IGNORECASE), "Cable_4.0SQ"),
    (re.compile(r"(조명|LAMP)\b",            re.IGNORECASE), "E-LIGHT"),
    (re.compile(r"(스위치|SW)\b",            re.IGNORECASE), "E-SWITCH"),
    (re.compile(r"(콘센트|OUTLET|SOCKET)\b", re.IGNORECASE), "E-OUTLET"),
    (re.compile(r"(분전반|PANEL|MDP|SDB)\b", re.IGNORECASE), "E-PANEL"),
    (re.compile(r"(차단기|BREAKER|MCB|ELB)\b",re.IGNORECASE),"E-PANEL"),
    (re.compile(r"^LIGHT$",                  re.IGNORECASE), "E-LIGHT"),
    (re.compile(r"^SWITCH$",                 re.IGNORECASE), "E-SWITCH"),
    (re.compile(r"^POWER$",                  re.IGNORECASE), "E-POWER"),
]


# ── 레이어 역할 분류 ──────────────────────────────────────────────────────────

def classify_layer_role(name: str) -> LayerRole:
    """레이어명 → 역할 분류."""
    n = (name or "").strip()
    if not n:
        return "unknown"
    up = n.upper()

    if up in _IGNORE_LAYER_SET:
        return "aux"
    if bool(_TITLE_HINT_RE.search(n)):
        return "aux"
    if _ELEC_PREFIX_RE.match(n):
        return "elec"
    if _ARCH_PREFIX_RE.match(n):
        return "arch"
    if _ELEC_HINT_RE.search(n):
        return "elec"
    if _ARCH_HINT_RE.search(n):
        return "arch"
    return "unknown"


# ── 엔티티 헬퍼 ──────────────────────────────────────────────────────────────

def _etype(e: dict) -> str:
    return str(e.get("raw_type") or e.get("type") or "").upper()


def _elayer(e: dict) -> str:
    return str(e.get("layer") or "")


def _etext(e: dict) -> str:
    return str(e.get("text") or e.get("content") or "").strip()


def _is_title_text(e: dict) -> bool:
    if _etype(e) not in ("TEXT", "MTEXT", "MLEADER", "LEADER"):
        return False
    return bool(_TITLE_TEXT_RE.search(_etext(e)))


def _is_room_label(e: dict) -> bool:
    if _etype(e) not in ("TEXT", "MTEXT"):
        return False
    return bool(_ROOM_LABEL_RE.search(_etext(e)))


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
    p = e.get("position") or e.get("insert_point") or e.get("center")
    if isinstance(p, dict) and "x" in p:
        try:
            x, y = float(p["x"]), float(p["y"])
            return x, y, x, y
        except (TypeError, ValueError):
            pass
    return None


def _center(ext: tuple) -> tuple[float, float]:
    return (ext[0] + ext[2]) / 2, (ext[1] + ext[3]) / 2


def _separation(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> float:
    ax1, ay1 = min(a[0], a[2]), min(a[1], a[3])
    ax2, ay2 = max(a[0], a[2]), max(a[1], a[3])
    bx1, by1 = min(b[0], b[2]), min(b[1], b[3])
    bx2, by2 = max(b[0], b[2]), max(b[1], b[3])
    if ax2 < bx1:
        dx = bx1 - ax2
    elif bx2 < ax1:
        dx = ax1 - bx2
    else:
        dx = 0.0
    if ay2 < by1:
        dy = by1 - ay2
    elif by2 < ay1:
        dy = ay1 - by2
    else:
        dy = 0.0
    return math.hypot(dx, dy)


# ── 메인 함수 ─────────────────────────────────────────────────────────────────

def build_elec_review_layout(
    drawing_data: dict[str, Any],
    *,
    active_ids: set[str] | None = None,
    include_unknown_in_review: bool = True,
) -> dict[str, Any]:
    """
    전기 도면에서 건축 배경(arch)과 전기 설비(elec)를 분리합니다.

    Args:
        drawing_data            : CadDrawingData JSON (entities, layers 포함)
        active_ids              : 선택된 handle 집합 (None이면 전체 검토)
        include_unknown_in_review: unknown 레이어를 검토 대상에 포함할지 여부

    Returns:
        elec_split_v1 스키마 dict
    """
    raw = drawing_data.get("entities") or drawing_data.get("elements") or []
    entities: list[dict] = [e for e in raw if isinstance(e, dict)]

    # ── 레이어별 통계 수집 ────────────────────────────────────────────────
    layer_stats: dict[str, dict[str, int]] = {}
    for e in entities:
        ln = _elayer(e)
        if not ln:
            continue
        if ln not in layer_stats:
            layer_stats[ln] = {"total": 0, "lines": 0, "blocks": 0, "texts": 0}
        layer_stats[ln]["total"] += 1
        et = _etype(e)
        if et in ("LINE", "POLYLINE", "LWPOLYLINE", "ARC", "SPLINE"):
            layer_stats[ln]["lines"] += 1
        elif et in ("INSERT", "BLOCK"):
            layer_stats[ln]["blocks"] += 1
        elif et in ("TEXT", "MTEXT", "MLEADER", "LEADER"):
            layer_stats[ln]["texts"] += 1

    # ── 레이어 색상 맵 ────────────────────────────────────────────────────
    layer_color_map: dict[str, str] = {}
    for linfo in (drawing_data.get("layers") or []):
        if isinstance(linfo, dict) and linfo.get("name"):
            layer_color_map[linfo["name"]] = str(linfo.get("color") or "")

    # ── 레이어별 역할 확정 ────────────────────────────────────────────────
    layer_roles: dict[str, LayerRole] = {}
    for ln, stats in layer_stats.items():
        role = classify_layer_role(ln)
        if role == "unknown":
            total = stats["total"]
            color = layer_color_map.get(ln, "").split("(")[0].strip()
            line_ratio  = stats["lines"]  / total if total > 0 else 0
            block_ratio = stats["blocks"] / total if total > 0 else 0

            # 회색 계열 = 건축 배경
            if color in _ARCH_ACI_COLORS:
                role = "arch"
            # 전기 블록이 많으면 전기 레이어
            elif block_ratio > 0.1 and total > 2:
                role = "elec"
            # 선분 위주이고 무채색이면 건축 배경
            elif line_ratio > 0.85 and color in _ARCH_ACI_COLORS | {"7", ""}:
                role = "arch"

        layer_roles[ln] = role
        _log.debug(
            "[ElecLayerSplit] Layer: %s | role: %s | stats: %s",
            ln, role, stats,
        )

    # ── 엔티티 분리 ───────────────────────────────────────────────────────
    arch:    list[dict] = []
    elec_all: list[dict] = []
    aux:     list[dict] = []
    unknown: list[dict] = []

    title_extents = [
        ext
        for e in entities
        if _is_title_text(e)
        for ext in [_bbox_extents(e)]
        if ext is not None
    ]

    for e in entities:
        # 표제란 텍스트/그래픽 제거
        if _is_title_text(e):
            aux.append({**e, "layer_role": "aux", "meta": "title_block"})
            continue
        # 실명 텍스트는 건축 참조로
        if _is_room_label(e):
            arch.append({**e, "layer_role": "arch", "meta": "space_label"})
            continue
        # 일반 텍스트(치수·주석)는 aux
        et = _etype(e)
        if et in ("TEXT", "MTEXT", "MLEADER", "LEADER", "DIMENSION"):
            aux.append({**e, "layer_role": "aux", "meta": "annotation"})
            continue

        ln   = _elayer(e)
        role = layer_roles.get(ln, "unknown")

        if role == "arch":
            arch.append({**e, "layer_role": "arch"})
        elif role in ("elec",):
            elec_all.append({**e, "layer_role": "elec"})
        elif role == "aux":
            aux.append({**e, "layer_role": "aux"})
        else:
            unknown.append({**e, "layer_role": "unknown"})
            if include_unknown_in_review:
                elec_all.append({**e, "layer_role": "unknown"})

    # ── active_ids 필터 ───────────────────────────────────────────────────
    if active_ids:
        elec_review = [e for e in elec_all if str(e.get("handle", "")) in active_ids]
    else:
        elec_review = elec_all

    # ── 공간 힌트 생성 (전기기기 ↔ 인접 실명) ────────────────────────────
    spatial_hints = _compute_spatial_hints(elec_review, arch)

    # ── 레이어 인덱스 ─────────────────────────────────────────────────────
    layers_indexed = [
        {"index": i, "name": ln, "layer_role": role}
        for i, (ln, role) in enumerate(sorted(layer_roles.items()))
    ]

    n_elec_only = sum(1 for e in elec_all if e.get("layer_role") == "elec")

    return {
        "schema":           "elec_split_v1",
        "arch_reference":   arch,
        "elec_review":      elec_review,
        "aux_skipped":      aux,
        "layer_roles":      layer_roles,
        "layers_indexed":   layers_indexed,
        "spatial_hints":    spatial_hints,
        "layer_role_stats": {
            "arch_entities":        len(arch),
            "elec_review_entities": len(elec_review),
            "elec_unfiltered":      len(elec_all),
            "elec_only_entities":   n_elec_only,
            "aux_skipped":          len(aux),
            "unknown_count":        len(unknown),
            "unknown_in_review":    len(unknown) if include_unknown_in_review else 0,
        },
    }


def audit_layers(elements: list[dict]) -> list[dict]:
    """
    엔티티 목록의 레이어를 STANDARD_LAYER_MAP과 대조해 비표준 항목을 반환한다.

    Returns:
        [{handle, current_layer, standard_layer, reason}, ...]
    """
    audit: list[dict] = []
    for e in elements:
        handle = str(e.get("handle") or "")
        current = str(e.get("layer") or "")
        if not current:
            continue
        for pat, standard in STANDARD_LAYER_MAP:
            if pat.search(current) and current != standard:
                audit.append({
                    "handle":         handle,
                    "current_layer":  current,
                    "standard_layer": standard,
                    "reason":         f"비표준 레이어 '{current}' → 표준 '{standard}'",
                })
                break
    return audit


def generate_layer_fix_actions(audit_result: list[dict]) -> list[dict]:
    """
    audit_layers() 결과를 AutoCAD modify_property 액션 목록으로 변환한다.

    Returns:
        [{action, target, changes}, ...]  (CAD_ACTION 호환 형식)
    """
    return [
        {
            "action": "modify_property",
            "target": {"handle": item["handle"]},
            "changes": {"layer": item["standard_layer"]},
            "reason": item["reason"],
        }
        for item in audit_result
        if item.get("handle") and item.get("standard_layer")
    ]


def _compute_spatial_hints(
    elec_entities: list[dict],
    arch_entities: list[dict],
    max_pairs: int = _SPATIAL_HINTS_MAX,
) -> list[dict]:
    """전기기기와 가장 가까운 공간명(실명)을 연결해 AI 컨텍스트로 제공."""
    room_labels = [
        (e, ext)
        for e in arch_entities
        if e.get("meta") == "space_label"
        for ext in [_bbox_extents(e)]
        if ext is not None
    ]
    if not room_labels:
        return []

    blocks = [
        (e, ext)
        for e in elec_entities
        if _etype(e) in ("INSERT", "BLOCK")
        for ext in [_bbox_extents(e)]
        if ext is not None
    ]

    hints: list[dict] = []
    for blk_e, blk_ext in blocks:
        if len(hints) >= max_pairs:
            break
        blk_center = _center(blk_ext)
        best_room, best_dist = None, math.inf
        for room_e, room_ext in room_labels:
            d = math.hypot(
                blk_center[0] - _center(room_ext)[0],
                blk_center[1] - _center(room_ext)[1],
            )
            if d < best_dist:
                best_dist = d
                best_room = room_e

        if best_room:
            hints.append({
                "elec_handle":   str(blk_e.get("handle", "")),
                "elec_name":     str(blk_e.get("effective_name") or blk_e.get("block_name") or ""),
                "nearest_space": str(best_room.get("text") or best_room.get("content") or ""),
                "nearest_space_handle": str(best_room.get("handle", "")),
                "distance":      round(best_dist, 2),
            })

    return hints
