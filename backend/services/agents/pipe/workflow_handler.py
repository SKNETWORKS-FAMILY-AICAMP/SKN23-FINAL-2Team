"""
File    : backend/services/agents/piping/workflow_handler.py
Author  : 송주엽
Create  : 2026-04-09
Description : PIPE_SUB_AGENT_TOOLS 스키마 기반 툴 호출 처리 및 서브 에이전트 연동

Modification History :
    - 2026-04-09 (송주엽) : 서브 에이전트 연동 워크플로우 통제 로직 초기 작성
    - 2026-04-14 (송주엽) : 생성자 인자 통일(session, db) 및 async 처리
    - 2026-04-15 (송주엽) : layout_data 폴백, target_id='ALL' 처리
    - 2026-04-19 (김지우) : get_cad_entity_info 툴 연동
    - 2026-04-22 : call_review — ComplianceAgent.check_compliance_parsed
    - 2026-04-29 (송주엽) : #1 DeterministicChecker 통합
                            #3 topology unit_factor 주입, total_length_mm
                            #4 _build_domain_rag_queries 병렬 멀티쿼리
                            #5 confidence 기반 violations 분리 출력
"""

import asyncio
import json
import logging
import math
import re as _re
import time
from collections import Counter
from typing import Dict, Any

from sqlalchemy.ext.asyncio import AsyncSession
from backend.core.config import settings
from backend.core.database import SessionLocal
# from sqlalchemy.orm import Session (제거)

from backend.services.agents.pipe.sub.query import QueryAgent
from backend.services.agents.pipe.sub.review.parser import ParserAgent, TextAttributeExtractor
from backend.services.agents.pipe.sub.review.compliance import ComplianceAgent
from backend.services.agents.pipe.sub.review.report import ReportAgent
from backend.services.agents.pipe.sub.review.revision import RevisionAgent
from backend.services.agents.pipe.sub.action import ActionAgent
from backend.services.agents.pipe.sub.topology import PipeTopologyBuilder
from backend.services.agents.pipe.sub.geometry import GeometryPreprocessor
from backend.services.agents.pipe.sub.deterministic_checker import run_deterministic_checks
from backend.services.agents.pipe.sub.drawing_qa_checker import (
    make_pipe_annotation_text_issue,
    run_drawing_qa_checks,
)
from backend.services.agents.pipe.sub.drawing_type import (
    classify_pipe_drawing_type as _classify_pipe_drawing_type,
    pipe_drawing_type_from_text as _pipe_drawing_type_from_text,
)
from backend.services.cad_progress import emit_pipeline_step
from backend.services import llm_service


_GAS_LAYER_RE = _re.compile(r"GAS|가스", _re.IGNORECASE)
_PIPE_SYMBOL_LAYER_RE = _re.compile(
    r"GAS|PIPE|PIPING|배관|급수|급탕|배수|위생|소화|SPRINK|CWS|HWS|FIRE|^P[-_]|^M[-_]",
    _re.IGNORECASE,
)


_PIPE_CONTINUITY_VIOLATION_TYPES = frozenset({
    "pipe_continuity_isolated_segment",
    "pipe_gap",
    "connection_mismatch",
    "connection_overshoot",
    "drawing_quality_pipe_gap",
    "drawing_quality_connection_mismatch",
    "drawing_quality_connection_overshoot",
    "drawing_quality_dangling_pipe",
})
_WEAK_TOPOLOGY_EVIDENCE = frozenset({"layer_annotation_style", "near_pipe_annotation"})


def _enrich_with_object_mapping(elements: list[dict], obj_mapping: list[dict]) -> None:
    """object_mapping label(텍스트 주석)로 elements의 diameter_mm·material을 보강.

    object_mapping 구조: [{"block_handle": str, "label": str, ...}, ...]
    - 숫자 label (예: "20") → diameter_mm = 20.0
    - "G" 접두어 또는 가스 레이어 → material = "GAS"
    """
    if not obj_mapping:
        return
    handle_map: dict[str, dict] = {
        m["block_handle"]: m for m in obj_mapping if m.get("block_handle")
    }
    for el in elements:
        layer = str(el.get("layer") or "")
        if el.get("material") == "UNKNOWN" and _GAS_LAYER_RE.search(layer):
            el["material"] = "GAS"

        m = handle_map.get(str(el.get("handle") or ""))
        if not m:
            continue
        label = str(m.get("label") or "")
        if el.get("diameter_mm", 0) == 0:
            nm = _re.search(r"(\d+(?:\.\d+)?)", label)
            if nm:
                el["diameter_mm"] = float(nm.group(1))
        if el.get("material") == "UNKNOWN" and _re.match(r"^G", label, _re.IGNORECASE):
            el["material"] = "GAS"


def _has_local_pipe_attrs(el: dict) -> bool:
    attrs = el.get("attributes") or el.get("properties") or {}
    material = str(el.get("material") or "").upper()
    return bool(
        el.get("flag_for_piping_agent")
        or el.get("diameter_mm")
        or material not in {"", "UNKNOWN", "NONE"}
        or attrs.get("SIZE")
        or attrs.get("DIAMETER")
        or attrs.get("MATERIAL")
        or attrs.get("TAG_NAME")
        or attrs.get("PRESSURE")
        or attrs.get("SLOPE")
    )


def _is_weak_generic_continuity_handle(el: dict | None) -> bool:
    if not isinstance(el, dict):
        return False
    return (
        str(el.get("topology_pipe_evidence") or "") in _WEAK_TOPOLOGY_EVIDENCE
        and not _has_local_pipe_attrs(el)
    )


def _has_strong_continuity_context(el: dict | None) -> bool:
    if not isinstance(el, dict):
        return False
    layer = str(el.get("layer") or "")
    role = str(el.get("layer_role") or "").lower()
    if _is_weak_generic_continuity_handle(el):
        return bool(_has_local_pipe_attrs(el) or _PIPE_SYMBOL_LAYER_RE.search(layer))
    return bool(
        _has_local_pipe_attrs(el)
        or role == "mep"
        or _PIPE_SYMBOL_LAYER_RE.search(layer)
    )


def _continuity_violation_handles(violation: dict) -> list[str]:
    handles: list[str] = []
    seen: set[str] = set()
    raw_handles = [
        violation.get("equipment_id"),
        violation.get("object_id"),
        *(violation.get("related_handles") or []),
    ]
    for raw in raw_handles:
        h = str(raw or "").strip()
        if h and h not in seen:
            handles.append(h)
            seen.add(h)
    return handles


def _continuity_evidence_for_handle(el: dict | None, handle: str) -> dict:
    if not isinstance(el, dict):
        return {
            "handle": handle,
            "layer": "",
            "layer_role": "",
            "topology_pipe_evidence": "",
            "local_pipe_attrs": False,
            "strong_continuity_context": False,
            "weak_generic": False,
        }
    return {
        "handle": str(el.get("handle") or el.get("id") or handle),
        "layer": str(el.get("layer") or ""),
        "layer_role": str(el.get("layer_role") or ""),
        "topology_pipe_evidence": str(el.get("topology_pipe_evidence") or ""),
        "local_pipe_attrs": _has_local_pipe_attrs(el),
        "strong_continuity_context": _has_strong_continuity_context(el),
        "weak_generic": _is_weak_generic_continuity_handle(el),
    }


def _continuity_evidence_strength(handle_evidence: list[dict]) -> str:
    if any(item.get("strong_continuity_context") for item in handle_evidence):
        return "strong"
    if any(item.get("weak_generic") for item in handle_evidence):
        return "weak"
    return "none"


def _filter_weak_generic_continuity_violations(
    violations: list[dict],
    elements: list[dict],
    *,
    source_name: str,
    diagnostics: list[dict] | None = None,
) -> list[dict]:
    """Drop continuity claims based only on weak inferred pipe-style hints."""
    if not violations:
        return violations
    by_handle = {
        str(el.get("handle") or el.get("id") or ""): el
        for el in elements or []
        if isinstance(el, dict) and (el.get("handle") or el.get("id"))
    }
    kept: list[dict] = []
    dropped = 0
    for violation in violations:
        if not isinstance(violation, dict):
            kept.append(violation)
            continue
        vtype = str(violation.get("violation_type") or violation.get("issue_type") or "")
        if vtype not in _PIPE_CONTINUITY_VIOLATION_TYPES:
            kept.append(violation)
            continue
        handles = _continuity_violation_handles(violation)
        involved = [by_handle.get(h) for h in handles if h]
        handle_evidence = [
            _continuity_evidence_for_handle(el, handle)
            for handle, el in zip(handles, involved)
        ]
        evidence_strength = _continuity_evidence_strength(handle_evidence)
        violation["evidence_strength"] = evidence_strength
        violation["pipe_evidence"] = {
            "source": source_name,
            "reason": "pipe_continuity_context",
            "handles": handle_evidence,
        }
        has_weak_generic = any(_is_weak_generic_continuity_handle(el) for el in involved)
        has_strong_context = any(_has_strong_continuity_context(el) for el in involved)
        weak_context_allowed = (
            vtype in {"connection_overshoot", "drawing_quality_connection_overshoot"}
            and bool(
                violation.get("_weak_annotation_context_allowed")
                or violation.get("weak_annotation_context_allowed")
            )
        )
        if (
            has_weak_generic
            and not has_strong_context
            and not weak_context_allowed
        ):
            dropped += 1
            if diagnostics is not None:
                diagnostics.append({
                    "source": source_name,
                    "action": "candidate_suppressed",
                    "reason": "weak_generic_continuity_without_strong_context",
                    "violation_type": vtype,
                    "equipment_id": str(violation.get("equipment_id") or ""),
                    "related_handles": [
                        str(h) for h in (violation.get("related_handles") or []) if h
                    ],
                    "evidence_strength": evidence_strength,
                    "handles": handle_evidence,
                })
            continue
        kept.append(violation)
    if dropped:
        logging.info(
            "[WorkflowHandler] dropped weak generic continuity %s violations=%d",
            source_name,
            dropped,
        )
    return kept


_PIPE_TEXT_RAW = frozenset({"TEXT", "MTEXT", "MLEADER"})
_PIPE_LINE_RAW = frozenset({"LINE", "ARC", "POLYLINE", "LWPOLYLINE", "SPLINE"})
_PIPE_SYMBOL_RAW = _PIPE_LINE_RAW | frozenset({"CIRCLE", "ELLIPSE"})
_GENERIC_LAYER_RE = _re.compile(r"^(?:L\d+|LAYER\d*|\d+)$", _re.IGNORECASE)
_NUMERIC_ONLY_TEXT_RE = _re.compile(r"^\s*\d+(?:\.\d+)?\s*$")
_PIPE_TEXT_LLM_MAX_CANDIDATES = 40
_PIPE_TEXT_LLM_MIN_CONFIDENCE = 0.78
_HANGUL_CHAR_CLASS = r"\uac00-\ud7a3"
_HANGUL_JAMO_CLASS = r"\u3131-\u318e"
_TEXT_PREFIX_TOKEN_RE = _re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._/-]*)\s+")
_PIPE_LABEL_RE = _re.compile(
    r"^(?:G|GAS|LPG|LNG|DN\s*\d+(?:\.\d+)?|\d+(?:\.\d+)?\s*A?)$|"
    r"레듀샤|레듀셔|리듀서|휴즈콕|퓨즈콕|후크콕|밸브|"
    r"VALVE|COCK|REDUCER",
    _re.IGNORECASE,
)
_PIPE_TEXT_CONTEXT_RE = _re.compile(
    r"가스|배관|급수|급탕|배수|오수|위생|수전|세탁|세면|양변기|대변기|소변기|"
    r"싱크|트랩|육가|환기|통기관|펌프|밸브|계량기|보일러|"
    r"DRAIN|SANIT|WATER|CWS|HWS|CW|HW|FD|SD|VENT",
    _re.IGNORECASE,
)
_PIPE_TEXT_LAYER_RE = _re.compile(
    r"(?:^|[-_\s])(?:TEX|TEXT|ANNO|NOTE|PIPE|PIPING|PLMB|PLUMB|SAN|WATER|GAS|M|P)(?:$|[-_\s])|"
    r"배관|급수|급탕|배수|위생|가스",
    _re.IGNORECASE,
)
_PIPE_TEXT_INVALID_CORRECTION_RE = _re.compile(r"육가스|육가\s*스")
_PIPE_TEXT_SPACING_FIX_COMPACT_TERMS = frozenset(
    {
        "가스누설경보기",
        "가스누설차단기",
        "가스계량기",
        "가스봄베함",
    }
)
_PIPE_TEXT_BARE_LABEL_RE = _re.compile(r"^\s*(?:G|GAS|LPG|LNG)\s*$", _re.IGNORECASE)
_TITLE_TEXT_RE = _re.compile(
    r"평면도|도면|SCALE|DRAWING|DWG|DATE|PROJECT|A1\s*:|A3\s*:",
    _re.IGNORECASE,
)


_TITLE_TEXT_FALLBACK_RE = _re.compile(
    r"평면도|도면명|도면번호|축척|SCALE|DRAWING|DWG|DATE|PROJECT|A1\s*:|A3\s*:",
    _re.IGNORECASE,
)


_USER_SYMBOL_CLUSTER_PAD_MM = 35.0
_USER_SYMBOL_CLUSTER_MAX_MM = 1400.0
_USER_SYMBOL_MEMBER_MAX_MM = 1200.0
_USER_SYMBOL_LINE_MEMBER_MAX_MM = 300.0
_UNIT_TO_MM: dict[str, float] = {
    "mm": 1.0,
    "millimeter": 1.0,
    "cm": 10.0,
    "centimeter": 10.0,
    "m": 1000.0,
    "meter": 1000.0,
    "inch": 25.4,
    "in": 25.4,
    '"': 25.4,
    "feet": 304.8,
    "foot": 304.8,
    "ft": 304.8,
    "'": 304.8,
}


def _resolve_unit_to_mm_factor(drawing_data: dict[str, Any]) -> float:
    raw_factor = drawing_data.get("unit_to_mm_factor")
    if raw_factor is not None:
        try:
            return float(raw_factor or 1.0)
        except (TypeError, ValueError):
            logging.warning(
                "[WorkflowHandler] invalid unit_to_mm_factor=%r; fallback to drawing_unit",
                raw_factor,
            )

    drawing_unit = str(drawing_data.get("drawing_unit") or "").strip().lower()
    unit_factor = _UNIT_TO_MM.get(drawing_unit)
    if unit_factor is not None:
        logging.info(
            "[WorkflowHandler] unit_to_mm_factor inferred from drawing_unit=%s: %.4f",
            drawing_unit,
            unit_factor,
        )
        return unit_factor

    logging.warning(
        "[WorkflowHandler] unit_to_mm_factor missing and drawing_unit=%r is unknown; "
        "fallback to 1.0 (assume mm). Dimension checks may be inaccurate for inch/feet drawings.",
        drawing_unit or None,
    )
    return 1.0


def _xy(point: dict | None) -> tuple[float, float] | None:
    if not isinstance(point, dict) or "x" not in point or "y" not in point:
        return None
    try:
        return float(point["x"]), float(point["y"])
    except (TypeError, ValueError):
        return None


def _element_position(el: dict) -> tuple[float, float] | None:
    for key in ("position", "insert_point", "center", "start"):
        p = _xy(el.get(key))
        if p:
            return p
    bbox = el.get("bbox")
    if isinstance(bbox, dict):
        try:
            return (
                (float(bbox["x1"]) + float(bbox["x2"])) / 2.0,
                (float(bbox["y1"]) + float(bbox["y2"])) / 2.0,
            )
        except (KeyError, TypeError, ValueError):
            return None
    return None


def _point_to_segment_distance(
    point: tuple[float, float],
    a: tuple[float, float] | None,
    b: tuple[float, float] | None,
) -> float:
    if not a or not b:
        return math.inf
    ax, ay = a
    bx, by = b
    px, py = point
    dx, dy = bx - ax, by - ay
    denom = dx * dx + dy * dy
    if denom <= 1e-12:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / denom))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _distance_to_pipe_line(point: tuple[float, float], el: dict) -> float:
    vertices = el.get("vertices") or el.get("fit_points") or []
    pts = [_xy(v) for v in vertices if isinstance(v, dict)]
    pts = [p for p in pts if p]
    if len(pts) >= 2:
        return min(_point_to_segment_distance(point, pts[i], pts[i + 1]) for i in range(len(pts) - 1))
    return _point_to_segment_distance(point, _xy(el.get("start")), _xy(el.get("end")))


def _symbol_line_length_mm(el: dict, unit_factor: float) -> float:
    try:
        value = el.get("length")
        if value is not None:
            return float(value) * unit_factor
    except (TypeError, ValueError):
        pass
    s = _xy(el.get("start"))
    e = _xy(el.get("end"))
    if s and e:
        return math.hypot(e[0] - s[0], e[1] - s[1]) * unit_factor
    bbox = _element_bbox(el)
    if bbox:
        return _bbox_max_dim_mm(bbox, unit_factor)
    return 0.0


def _polyline_symbol_like(el: dict, unit_factor: float) -> bool:
    raw = str(el.get("raw_type") or "").upper()
    if raw not in {"POLYLINE", "LWPOLYLINE"}:
        return False
    if bool(el.get("is_closed")):
        return True
    try:
        if float(el.get("area") or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass
    vertices = el.get("vertices") or []
    if len(vertices) < 4:
        return False
    bbox = el.get("bbox") or {}
    try:
        if "x1" in bbox:
            w = abs(float(bbox["x2"]) - float(bbox["x1"])) * unit_factor
            h = abs(float(bbox["y2"]) - float(bbox["y1"])) * unit_factor
        elif "min_x" in bbox:
            w = abs(float(bbox["max_x"]) - float(bbox["min_x"])) * unit_factor
            h = abs(float(bbox["max_y"]) - float(bbox["min_y"])) * unit_factor
        else:
            pts = [_xy(v) for v in vertices if isinstance(v, dict)]
            pts = [p for p in pts if p]
            if len(pts) < 4:
                return False
            w = (max(p[0] for p in pts) - min(p[0] for p in pts)) * unit_factor
            h = (max(p[1] for p in pts) - min(p[1] for p in pts)) * unit_factor
    except (KeyError, TypeError, ValueError):
        return False
    if min(w, h) < 20.0:
        return False
    return max(w, h) / max(min(w, h), 1e-9) < 8.0


def _bbox_from_points(points: list[tuple[float, float]]) -> tuple[float, float, float, float] | None:
    pts = [p for p in points if p is not None]
    if not pts:
        return None
    return (
        min(p[0] for p in pts),
        min(p[1] for p in pts),
        max(p[0] for p in pts),
        max(p[1] for p in pts),
    )


def _element_bbox(el: dict) -> tuple[float, float, float, float] | None:
    bbox = el.get("bbox")
    if isinstance(bbox, dict):
        try:
            if "x1" in bbox:
                return float(bbox["x1"]), float(bbox["y1"]), float(bbox["x2"]), float(bbox["y2"])
            if "min_x" in bbox:
                return float(bbox["min_x"]), float(bbox["min_y"]), float(bbox["max_x"]), float(bbox["max_y"])
        except (KeyError, TypeError, ValueError):
            return None

    raw = str(el.get("raw_type") or "").upper()
    if raw in {"LINE", "ARC"}:
        return _bbox_from_points([p for p in (_xy(el.get("start")), _xy(el.get("end"))) if p])
    if raw in {"POLYLINE", "LWPOLYLINE", "SPLINE"}:
        pts = [
            p for p in (_xy(v) for v in (el.get("vertices") or el.get("fit_points") or []) if isinstance(v, dict))
            if p
        ]
        return _bbox_from_points(pts)
    if raw in {"CIRCLE", "ELLIPSE"}:
        center = _xy(el.get("center")) or _element_position(el)
        try:
            r = float(el.get("radius") or 0)
        except (TypeError, ValueError):
            r = 0.0
        if center and r > 0:
            return center[0] - r, center[1] - r, center[0] + r, center[1] + r

    p = _element_position(el)
    if p:
        return p[0], p[1], p[0], p[1]
    return None


def _bbox_merge(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    return min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])


def _bbox_near(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
    pad: float,
) -> bool:
    return not (
        a[2] + pad < b[0]
        or b[2] + pad < a[0]
        or a[3] + pad < b[1]
        or b[3] + pad < a[1]
    )


def _bbox_max_dim_mm(bbox: tuple[float, float, float, float], unit_factor: float) -> float:
    return max(abs(bbox[2] - bbox[0]), abs(bbox[3] - bbox[1])) * unit_factor


def _is_user_drawn_symbol_candidate(el: dict, unit_factor: float) -> bool:
    raw = str(el.get("raw_type") or "").upper()
    if raw not in _PIPE_SYMBOL_RAW or not el.get("handle"):
        return False
    # Nearby pipe text can promote compact fitting/valve glyph pieces to MEP;
    # they still should not become pipe-run centerlines.
    if str(el.get("layer_role") or "").lower() in {"arch", "aux"}:
        return False

    bbox = _element_bbox(el)
    if not bbox:
        return False
    max_dim = _bbox_max_dim_mm(bbox, unit_factor)
    if max_dim > _USER_SYMBOL_MEMBER_MAX_MM:
        return False

    layer = str(el.get("layer") or "").strip()
    line_like = raw in {"LINE", "ARC", "SPLINE"}
    if line_like and _symbol_line_length_mm(el, unit_factor) > _USER_SYMBOL_LINE_MEMBER_MAX_MM:
        return False

    if _GENERIC_LAYER_RE.match(layer):
        return True
    if raw in {"CIRCLE", "ELLIPSE"}:
        return True
    if _polyline_symbol_like(el, unit_factor):
        return True
    if raw in {"LINE", "ARC", "SPLINE"}:
        role = str(el.get("layer_role") or "").lower()
        layer = str(el.get("layer") or "")
        return bool(role == "mep" or el.get("flag_for_piping_agent") or _PIPE_SYMBOL_LAYER_RE.search(layer))
    return str(el.get("source_attributes") or "").lower() == "text_extracted"


def _symbol_line_angle_bin(el: dict) -> int | None:
    s = _xy(el.get("start"))
    e = _xy(el.get("end"))
    if not s or not e:
        return None
    angle = (math.degrees(math.atan2(e[1] - s[1], e[0] - s[0])) + 180.0) % 180.0
    return int((angle + 22.5) // 45.0) % 4


def _line_only_symbol_cluster_like(elements: list[dict]) -> bool:
    if len(elements) < 3:
        return False
    raw_types = {str(el.get("raw_type") or "").upper() for el in elements}
    if not raw_types.issubset({"LINE", "ARC", "SPLINE"}):
        return False
    if raw_types & {"ARC", "SPLINE"}:
        return True
    angle_bins = {
        bin_value
        for el in elements
        for bin_value in [_symbol_line_angle_bin(el)]
        if bin_value is not None
    }
    return len(angle_bins) >= 2


def _mark_user_drawn_pipe_symbols(elements: list[dict], unit_factor: float) -> int:
    """Mark compact user-drawn pipe symbols before topology building.

    Some projects draw valve/meter symbols as loose LINE/POLYLINE/CIRCLE pieces
    instead of using INSERT blocks. Keep line-only symbol fragments available
    for endpoint QA, while excluding closed/non-line glyphs from pipe-run
    continuity topology.
    """
    candidates: list[tuple[dict, tuple[float, float, float, float]]] = []
    for el in elements or []:
        if not isinstance(el, dict) or el.get("exclude_from_pipe_topology"):
            continue
        if not _is_user_drawn_symbol_candidate(el, unit_factor):
            continue
        bbox = _element_bbox(el)
        if bbox:
            candidates.append((el, bbox))

    if not candidates:
        return 0

    pad = _USER_SYMBOL_CLUSTER_PAD_MM / max(unit_factor, 1e-9)
    seen: set[int] = set()
    marked = 0
    for i in range(len(candidates)):
        if i in seen:
            continue
        stack = [i]
        seen.add(i)
        comp: list[int] = []
        cluster_bbox = candidates[i][1]
        while stack:
            idx = stack.pop()
            comp.append(idx)
            cluster_bbox = _bbox_merge(cluster_bbox, candidates[idx][1])
            for j, (_, other_bbox) in enumerate(candidates):
                if j in seen:
                    continue
                if _bbox_near(candidates[idx][1], other_bbox, pad):
                    seen.add(j)
                    stack.append(j)

        max_dim = _bbox_max_dim_mm(cluster_bbox, unit_factor)
        if max_dim > _USER_SYMBOL_CLUSTER_MAX_MM:
            continue

        comp_elements = [candidates[idx][0] for idx in comp]
        has_shape = any(
            str(el.get("raw_type") or "").upper() in {"CIRCLE", "ELLIPSE"}
            or _polyline_symbol_like(el, unit_factor)
            for el in comp_elements
        )
        if len(comp_elements) < 2 and not has_shape:
            continue
        line_only_symbol = _line_only_symbol_cluster_like(comp_elements)
        if not has_shape and not line_only_symbol:
            continue

        group_id = f"user_pipe_symbol:{marked + 1}"
        for el in comp_elements:
            el["geometry_role"] = "pipe_symbol"
            el["connectivity_role"] = "symbol_fragment" if line_only_symbol and not has_shape else "symbol"
            if has_shape:
                el["exclude_from_pipe_topology"] = True
            el["symbol_group_id"] = group_id
            el["symbol_detection_reason"] = "compact_user_drawn_layer_symbol"
            marked += 1

    return marked


def _enrich_nearby_pipe_lines_from_text(elements: list[dict], unit_factor: float) -> int:
    """Propagate nearby TEXT/MTEXT/MLEADER pipe labels to line-like pipe elements."""
    if not elements:
        return 0
    line_like = [
        e for e in elements
        if str(e.get("raw_type") or "").upper() in _PIPE_LINE_RAW
        and e.get("handle")
        and str(e.get("layer_role") or "").lower() not in {"arch", "aux"}
        and not (
            _GENERIC_LAYER_RE.match(str(e.get("layer") or "").strip())
            and _polyline_symbol_like(e, unit_factor)
            and not e.get("flag_for_piping_agent")
        )
    ]
    if not line_like:
        return 0

    near_tol = max(250.0 / max(unit_factor, 1e-9), 80.0)
    enriched = 0
    for ann in elements:
        if str(ann.get("raw_type") or "").upper() not in _PIPE_TEXT_RAW:
            continue
        text = str(ann.get("text") or ann.get("content") or "").strip()
        if not text or (
            len(text) > 8
            and (_TITLE_TEXT_RE.search(text) or _TITLE_TEXT_FALLBACK_RE.search(text))
        ):
            continue
        extracted = TextAttributeExtractor.extract_from_text(text)
        if not extracted and not _PIPE_LABEL_RE.search(text):
            continue
        if _re.search(r"^(?:G|GAS)\b|가스", text, _re.IGNORECASE):
            extracted = {**extracted, "material": "GAS"}
        if not extracted:
            continue
        pos = _element_position(ann)
        if not pos:
            continue
        best = min(
            ((_distance_to_pipe_line(pos, line), line) for line in line_like),
            key=lambda item: item[0],
            default=(math.inf, None),
        )
        dist, target = best
        if not target or dist > near_tol:
            continue
        before = dict(target)
        TextAttributeExtractor.enrich_element(target, extracted)
        if target != before:
            enriched += 1
    return enriched


_DOMAIN_RAG: dict[str, str] = {
    "GAS":   "도시가스설비공사 도시가스설비 가스 배관 KCS 31 50 10 KDS 31 50 10 KGS 가스누설차단기 밸브 이격거리 설치위치 기준",
    "WATER": "급수설비공사 급탕설비공사 급수 급탕 배관 KCS 31 30 15 KCS 31 30 20 이격거리 관경 재질 기준 규정",
    "DRAIN": "배수통기설비공사 배수 통기 오수 위생 배관 구배 트랩 청소구 KCS 31 30 25 KDS 31 30 25 설치기준",
    "FIRE":  "소화 스프링클러 배관 이격거리 헤드 간격 기준 규정",
    "HVAC":  "냉난방 공조 덕트 배관 이격거리 설치 기준 규정",
}
_PIPE_DOMAIN_CODES = frozenset(_DOMAIN_RAG.keys())
_PIPE_CORE_RAG_QUERIES: tuple[str, ...] = (
    "배관 도면검토 기준 관경 재질 압력 기울기 행거 지지 간격 밸브 접근성 관통부 방화 충전 이격거리",
    "배관 CAD 설계도서 품질검토 끊김 연결불량 중복배관 고립배관 충돌 간섭 표기 누락 기준",
)
_PIPE_MAX_RAG_QUERIES = 8
_PIPE_RAG_LIMIT_PER_QUERY = 6
_PIPE_MAX_RAG_RESULTS = 18


def _pipe_rag_concurrency(query_count: int) -> int:
    """Limit DB fan-out only when the connection goes through an SSH tunnel."""
    query_count = max(1, int(query_count or 1))
    if getattr(settings, "USE_SSH_TUNNEL", False):
        try:
            configured = int(getattr(settings, "PIPE_RAG_SSH_MAX_CONCURRENT", 4) or 4)
        except (TypeError, ValueError):
            configured = 4
        return min(query_count, max(1, configured))

    try:
        configured = int(getattr(settings, "PIPE_RAG_MAX_CONCURRENT", 4) or 4)
    except (TypeError, ValueError):
        configured = 4
    return min(query_count, max(1, configured))


_DOMAIN_LAYER_RE = _re.compile(
    r"GAS|가스|FIRE|소화|SPRINK|CWS|HWS|급수|급탕|DRAIN|SANIT|배수|오수|통기|위생|HVAC|냉난방",
    _re.IGNORECASE,
)

_FIRE_LAYER_WH_RE = _re.compile(r"FIRE|SP[-_]|소화|SPRINK", _re.IGNORECASE)
_WATER_LAYER_WH_RE = _re.compile(r"CWS|HWS|급수|급탕|WATER", _re.IGNORECASE)
_DRAIN_LAYER_WH_RE = _re.compile(
    r"DRAIN|SANIT|SEWER|VENT|배수|오수|통기|위생|(?:^|[-_\s])(?:SD|FD|VP)(?:[-_\s]|$)",
    _re.IGNORECASE,
)
_DRAIN_TEXT_RE = _re.compile(
    r"배수|오수|통기|위생|오배수|잡배수|트랩|청소구|DRAIN|SANIT|SEWER|VENT",
    _re.IGNORECASE,
)
_HVAC_LAYER_WH_RE  = _re.compile(r"HVAC|냉난방|공조|DUCT", _re.IGNORECASE)


def _infer_pipe_rag_domains(
    elements: list[dict],
    *,
    drawing_title: str | None = None,
    text_keywords: list[str] | None = None,
) -> list[str]:
    """Infer pipe RAG subdomains without relying on the LLM classifier."""
    domains: list[str] = []
    seen: set[str] = set()

    def _add(domain: str) -> None:
        if domain in _PIPE_DOMAIN_CODES and domain not in seen:
            seen.add(domain)
            domains.append(domain)

    materials = {str(e.get("material") or "").upper() for e in elements or []}
    layers = [str(e.get("layer") or "") for e in elements or []]
    layer_str = " ".join(layers)
    text_str = " ".join(
        [str(drawing_title or ""), *(str(x or "") for x in (text_keywords or []))]
    )

    if "GAS" in materials or _GAS_LAYER_RE.search(layer_str) or _re.search(r"가스|GAS|LPG|LNG", text_str, _re.IGNORECASE):
        _add("GAS")
    if _DRAIN_LAYER_WH_RE.search(layer_str) or _DRAIN_TEXT_RE.search(text_str) or materials & {
        "DRAIN",
        "SANITARY",
        "SEWER",
        "WASTE",
        "VENT",
    }:
        _add("DRAIN")
    if _FIRE_LAYER_WH_RE.search(layer_str) or _re.search(r"소화|SPRINK|FIRE", text_str, _re.IGNORECASE):
        _add("FIRE")
    if _WATER_LAYER_WH_RE.search(layer_str) or materials & {
        "WATER",
        "WATER_SUPPLY",
        "HOT_WATER",
        "CWS",
        "HWS",
    } or _re.search(r"급수|급탕|냉수|온수|WATER|CWS|HWS", text_str, _re.IGNORECASE):
        _add("WATER")
    if _HVAC_LAYER_WH_RE.search(layer_str) or _re.search(r"냉난방|공조|HVAC|DUCT", text_str, _re.IGNORECASE):
        _add("HVAC")

    return domains


def _is_pipe_text_review_context(el: dict, text: str) -> bool:
    role = str(el.get("layer_role") or "").lower()
    if role == "mep" or el.get("flag_for_piping_agent"):
        return True
    if _PIPE_LABEL_RE.search(text) or _PIPE_TEXT_CONTEXT_RE.search(text):
        return True
    layer = str(el.get("layer") or "")
    return bool(
        _PIPE_TEXT_LAYER_RE.search(layer)
        and _PIPE_TEXT_CONTEXT_RE.search(f"{layer} {text}")
    )


def _extract_text_keywords(elements: list[dict]) -> list[str]:
    """Extract pipe equipment/spec keywords from text-like CAD entities."""
    seen: set[str] = set()
    scored: list[tuple[int, int, str]] = []
    for idx, e in enumerate(elements or []):
        if str(e.get("raw_type") or e.get("type") or "").upper() not in _PIPE_TEXT_RAW:
            continue
        text = str(e.get("text") or e.get("content") or "").strip()
        if not text or _NUMERIC_ONLY_TEXT_RE.match(text):
            continue
        if _TITLE_TEXT_RE.search(text) or _TITLE_TEXT_FALLBACK_RE.search(text):
            continue
        role = str(e.get("layer_role") or "").lower()
        if _PIPE_LABEL_RE.search(text):
            score = 0
        elif _pipe_drawing_type_from_text(text) != "unknown":
            score = 1
        elif role == "mep" or e.get("flag_for_piping_agent"):
            score = 2
        elif _is_pipe_text_review_context(e, text):
            score = 3
        else:
            continue
        scored.append((score, idx, text))
    keywords: list[str] = []
    for _score, _idx, text in sorted(scored):
        if text in seen:
            continue
        seen.add(text)
        keywords.append(text)
        if len(keywords) >= 8:
            break
    return keywords


def _extract_drawing_title_from_raw(drawing_data: dict | None) -> str:
    """Best-effort title extraction from raw CAD text entities."""
    if not isinstance(drawing_data, dict):
        return ""
    candidates: list[str] = []
    for ent in drawing_data.get("entities") or drawing_data.get("elements") or []:
        if not isinstance(ent, dict):
            continue
        raw = str(ent.get("raw_type") or ent.get("type") or "").upper()
        if raw not in _PIPE_TEXT_RAW:
            continue
        text = str(ent.get("text") or ent.get("content") or "").strip()
        if not text:
            continue
        if _pipe_drawing_type_from_text(text) != "unknown":
            candidates.append(text)
    return max(candidates, key=len) if candidates else ""


def _pipe_text_review_rank(item: dict, original_index: int) -> tuple[int, int, int]:
    role = str(item.get("layer_role") or "").lower()
    return (
        0 if role == "mep" or item.get("flag_for_piping_agent") else 1,
        len(str(item.get("text") or "")),
        original_index,
    )


def _pipe_text_review_candidates(elements: list[dict]) -> list[dict]:
    candidates: list[tuple[int, dict]] = []
    seen_handles: set[str] = set()
    for el in elements or []:
        raw = str(el.get("raw_type") or el.get("type") or "").upper()
        if raw not in _PIPE_TEXT_RAW:
            continue
        handle = str(el.get("handle") or el.get("id") or "")
        if not handle or handle in seen_handles:
            continue
        text = str(el.get("text") or el.get("content") or "").strip()
        if not text or len(text) > 80:
            continue
        if _NUMERIC_ONLY_TEXT_RE.match(text):
            continue
        if _PIPE_TEXT_BARE_LABEL_RE.match(text):
            continue
        if _TITLE_TEXT_RE.search(text) or _TITLE_TEXT_FALLBACK_RE.search(text):
            continue
        role = str(el.get("layer_role") or "").lower()
        if not _is_pipe_text_review_context(el, text):
            continue
        seen_handles.add(handle)
        candidates.append((len(candidates), {
            "handle": handle,
            "text": text,
            "layer": el.get("layer"),
            "layer_role": role,
            "flag_for_piping_agent": bool(el.get("flag_for_piping_agent")),
            "raw_type": raw,
        }))
    candidates.sort(key=lambda pair: _pipe_text_review_rank(pair[1], pair[0]))
    return [item for _idx, item in candidates[:_PIPE_TEXT_LLM_MAX_CANDIDATES]]


_HANGUL_CHO = tuple("ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ")
_HANGUL_JUNG = tuple("ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ")
_HANGUL_JONG = (
    "",
    "ㄱ", "ㄲ", "ㄳ", "ㄴ", "ㄵ", "ㄶ", "ㄷ", "ㄹ", "ㄺ", "ㄻ", "ㄼ", "ㄽ",
    "ㄾ", "ㄿ", "ㅀ", "ㅁ", "ㅂ", "ㅄ", "ㅅ", "ㅆ", "ㅇ", "ㅈ", "ㅊ", "ㅋ",
    "ㅌ", "ㅍ", "ㅎ",
)
_HANGUL_CHO_SET = set(_HANGUL_CHO)
_HANGUL_JUNG_SET = set(_HANGUL_JUNG)
_HANGUL_JONG_SET = set(_HANGUL_JONG) - {""}


def _has_compat_jamo(text: str) -> bool:
    return bool(_re.search(rf"[{_HANGUL_JAMO_CLASS}]", text or ""))


def _compose_compat_jamo(text: str) -> str:
    compact = _re.sub(r"\s+", "", str(text or ""))
    out: list[str] = []
    i = 0
    while i < len(compact):
        ch = compact[i]
        if (
            ch in _HANGUL_CHO_SET
            and i + 1 < len(compact)
            and compact[i + 1] in _HANGUL_JUNG_SET
        ):
            cho = _HANGUL_CHO.index(ch)
            jung = _HANGUL_JUNG.index(compact[i + 1])
            jong = 0
            consumed = 2
            if i + 2 < len(compact) and compact[i + 2] in _HANGUL_JONG_SET:
                next_is_new_syllable = (
                    i + 3 < len(compact)
                    and compact[i + 3] in _HANGUL_JUNG_SET
                )
                if not next_is_new_syllable:
                    jong = _HANGUL_JONG.index(compact[i + 2])
                    consumed = 3
            out.append(chr(0xAC00 + (cho * 21 + jung) * 28 + jong))
            i += consumed
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _normalize_pipe_text_source_spacing(text: str) -> str:
    raw_tokens = str(text or "").split()
    if not raw_tokens:
        return ""

    normalized: list[str] = []
    i = 0
    while i < len(raw_tokens):
        token = raw_tokens[i]
        if not _has_compat_jamo(token):
            normalized.append(_compose_compat_jamo(token))
            i += 1
            continue

        parts = [token]
        i += 1
        while i < len(raw_tokens) and _has_compat_jamo(raw_tokens[i]):
            parts.append(raw_tokens[i])
            i += 1

        joined = "".join(parts)
        # A lone compatibility consonant between terms is almost always an OCR/
        # drafting artifact. Remove it without changing surrounding word spacing.
        if _re.fullmatch(rf"[{_HANGUL_JAMO_CLASS}]+", joined) and not any(
            ch in _HANGUL_JUNG_SET for ch in joined
        ):
            continue
        normalized.append(_compose_compat_jamo(joined))

    return " ".join(t for t in normalized if t)


def _normalize_pipe_annotation_terms(text: str) -> str:
    corrected = _re.sub(r"\s+", " ", str(text or "")).strip()
    if not corrected:
        return ""

    corrected = _re.sub(r"노\s*설", "누설", corrected)
    corrected = _re.sub(r"누\s+설", "누설", corrected)
    corrected = _re.sub(r"경보\s+기", "경보기", corrected)
    corrected = _re.sub(r"[Cc]\s*창\s*단\s*기", "차단기", corrected)
    corrected = _re.sub(r"[Cc]\s*차\s*단\s*기", "차단기", corrected)
    corrected = _re.sub(r"창\s*단\s*기", "차단기", corrected)
    corrected = _re.sub(r"차\s*단\s*기", "차단기", corrected)
    corrected = _re.sub(r"차단\s+기", "차단기", corrected)
    corrected = _re.sub(r"계량\s+기", "계량기", corrected)
    corrected = _re.sub(r"봄베\s+함", "봄베함", corrected)

    corrected = _re.sub(r"가스\s*누설\s*경보기", "가스 누설 경보기", corrected)
    corrected = _re.sub(r"가스\s*누설\s*차단기", "가스 누설 차단기", corrected)
    corrected = _re.sub(r"가스\s*계량기", "가스 계량기", corrected)
    return corrected


def _is_allowed_pipe_text_spacing_fix(original: str, corrected: str) -> bool:
    original_compact = _re.sub(r"\s+", "", str(original or ""))
    corrected_compact = _re.sub(r"\s+", "", str(corrected or ""))
    if not original_compact or original_compact != corrected_compact:
        return False
    normalized = _normalize_pipe_annotation_terms(original)
    if normalized != corrected:
        return False
    compact = _re.sub(r"\s+", "", normalized)
    return bool(
        any(term in compact for term in _PIPE_TEXT_SPACING_FIX_COMPACT_TERMS)
    )


def _sanitize_pipe_text_correction(original: str, corrected: str) -> str | None:
    """Keep LLM text fixes scoped to the current CAD TEXT entity.

    CAD drawings often split prefixes such as "3M", "25A", or model codes
    into separate TEXT objects from the following equipment label. We may fix
    an internal Korean compound space, but should not attach that prefix to the
    Hangul equipment name.
    """
    original = str(original or "").strip()
    corrected = str(corrected or "").strip()
    if not original or not corrected or corrected == original:
        return None

    original_compact = _re.sub(r"\s+", "", original)
    corrected_compact = _re.sub(r"\s+", "", corrected)
    normalized_source = _normalize_pipe_text_source_spacing(original)
    if normalized_source and normalized_source != original:
        normalized_source_compact = _re.sub(r"\s+", "", normalized_source)
        if corrected_compact == normalized_source_compact:
            corrected = normalized_source
            corrected_compact = normalized_source_compact
    if (
        original_compact == corrected_compact
        and original != corrected
        and not _re.search(
            rf"(?<![{_HANGUL_CHAR_CLASS}])[{_HANGUL_CHAR_CLASS}]\s+[{_HANGUL_CHAR_CLASS}](?![{_HANGUL_CHAR_CLASS}])",
            original,
        )
        and not _is_allowed_pipe_text_spacing_fix(original, corrected)
    ):
        return None
    if (
        _PIPE_TEXT_INVALID_CORRECTION_RE.search(corrected_compact)
        and not _PIPE_TEXT_INVALID_CORRECTION_RE.search(original_compact)
    ):
        return None
    if (
        _re.search(rf"[{_HANGUL_CHAR_CLASS}]", original)
        and corrected_compact.endswith(original_compact)
        and corrected_compact != original_compact
    ):
        added_prefix = corrected_compact[: -len(original_compact)]
        if _re.fullmatch(r"[A-Za-z0-9._/-]+", added_prefix):
            return None
    if (
        _re.search(rf"[{_HANGUL_CHAR_CLASS}]", original)
        and corrected_compact.startswith(original_compact)
        and corrected_compact != original_compact
    ):
        added_suffix = corrected_compact[len(original_compact):]
        if _re.fullmatch(r"[A-Za-z0-9._/-]+|[가-힣]", added_suffix):
            return None

    prefix_match = _TEXT_PREFIX_TOKEN_RE.match(original)
    if prefix_match:
        prefix = prefix_match.group(1)
        remainder = original[prefix_match.end():].lstrip()
        starts_with_hangul = bool(
            remainder and _re.match(rf"[{_HANGUL_CHAR_CLASS}]", remainder)
        )
        if starts_with_hangul:
            corrected = _re.sub(
                rf"^(\s*{_re.escape(prefix)})(?=[{_HANGUL_CHAR_CLASS}])",
                r"\1 ",
                corrected,
                count=1,
            ).strip()

    if corrected == original:
        return None
    return corrected


def _apply_pipe_text_typo_rules(text: str) -> tuple[str, str] | None:
    original = str(text or "").strip()
    if not original:
        return None
    reasons: list[str] = []
    corrected = original
    if _has_compat_jamo(original):
        corrected = _normalize_pipe_text_source_spacing(original)
        if corrected and corrected != original:
            reasons.append("분리 자모 복원")
    term_corrected = _normalize_pipe_annotation_terms(corrected)
    if term_corrected and term_corrected != corrected:
        corrected = term_corrected
        reasons.append("배관 주석 맞춤법/띄어쓰기 교정")
    if not corrected or corrected == original:
        return None
    return corrected, " / ".join(reasons) if reasons else "배관 주석 맞춤법/띄어쓰기 교정"


def _clean_pipe_text_review_reason(reason: str) -> str:
    text = str(reason or "").strip()
    if not text:
        return "배관 주석 표기 검수"
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    if ascii_chars / max(len(text), 1) > 0.65:
        return "배관 주석 표기 검수"
    return text[:120]


def _review_pipe_annotation_texts_deterministic(elements: list[dict]) -> list[dict]:
    issues: list[dict] = []
    seen_handles: set[str] = set()
    for el in elements or []:
        raw = str(el.get("raw_type") or el.get("type") or "").upper()
        if raw not in _PIPE_TEXT_RAW:
            continue
        handle = str(el.get("handle") or el.get("id") or "")
        if not handle or handle in seen_handles:
            continue
        text = str(el.get("text") or el.get("content") or "").strip()
        if not text or len(text) > 120:
            continue
        if _TITLE_TEXT_RE.search(text) or _TITLE_TEXT_FALLBACK_RE.search(text):
            continue
        if not _is_pipe_text_review_context(el, text):
            continue
        correction = _apply_pipe_text_typo_rules(text)
        if not correction:
            continue
        corrected, reason = correction
        corrected = _sanitize_pipe_text_correction(text, corrected) or ""
        if not corrected:
            continue
        seen_handles.add(handle)
        el["_qa_original_text"] = text
        el["_qa_text_review_reason"] = reason
        issue = make_pipe_annotation_text_issue(
            el,
            corrected,
            reason=reason,
            confidence_score=0.96,
            confidence_reason="pipe_annotation_text_deterministic_typo",
        )
        if issue:
            issues.append(issue)
    return issues


async def _review_pipe_annotation_texts_llm(elements: list[dict]) -> list[dict]:
    deterministic_issues = _review_pipe_annotation_texts_deterministic(elements)
    deterministic_handles = {
        str(issue.get("equipment_id") or "")
        for issue in deterministic_issues
        if issue.get("equipment_id")
    }
    candidates = [
        item
        for item in _pipe_text_review_candidates(elements)
        if str(item.get("handle") or "") not in deterministic_handles
    ]
    logging.info(
        "[WorkflowHandler] pipe annotation text QA deterministic=%d llm_candidates=%d samples=%s",
        len(deterministic_issues),
        len(candidates),
        [
            {
                "handle": item.get("handle"),
                "layer": item.get("layer"),
                "text": item.get("text"),
            }
            for item in candidates[:5]
        ],
    )
    if not candidates:
        return deterministic_issues

    try:
        result = await asyncio.wait_for(
            llm_service.generate_answer(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You review Korean/English CAD piping annotation text. "
                            "Return JSON only with corrections array. "
                            "Use your language judgment and the piping drawing context. Only fix text when the "
                            "original is clearly broken and one corrected form is highly likely. Acceptable fixes "
                            "include broken Korean jamo, letters split inside a word, obvious token typos, and "
                            "clear unit or standard-term mistakes. Do not rewrite numbers, pipe sizes, proper "
                            "nouns, drawing notes, or engineering meaning. "
                            "Whitespace by itself is only a reason to create an issue for known pipe/gas annotation "
                            "terms where the original splits a Korean word or hides a standard Korean term boundary, "
                            "such as 가스 누설 경보기, 가스 누설 차단기, or 가스 계량기. Do not create an issue "
                            "only to add spaces around unit/count packs such as 50KGX2EA. Preserve the original word "
                            "boundaries between complete Korean terms unless a boundary is inside a broken word, "
                            "a known pipe/gas term, or a broken jamo sequence. When restoring broken jamo, repair "
                            "the syllable but keep the spacing between surrounding semantic terms. Do not concatenate "
                            "adjacent complete terms merely because a compact form is also seen in drawings. "
                            "Do not add leading/trailing characters or absorb neighboring CAD text fragments into "
                            "this handle's corrected_text. If the intended term is uncertain, or the change is "
                            "only cosmetic spacing, set needs_fix=false. For each correction give a short Korean "
                            "reason."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "schema": {
                                    "corrections": [
                                        {
                                            "handle": "CAD handle",
                                            "needs_fix": True,
                                            "corrected_text": "corrected text",
                                            "confidence": 0.0,
                                            "reason": "short reason",
                                        }
                                    ]
                                },
                                "candidates": candidates,
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                response_format={"type": "json_object"},
                temperature=0.0,
            ),
            timeout=20.0,
        )
    except Exception as exc:
        logging.warning("[WorkflowHandler] pipe text LLM QA skipped: %s", exc)
        return []

    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError:
            return []
    if not isinstance(result, dict):
        return []

    handle_map = {str(el.get("handle") or el.get("id") or ""): el for el in elements or []}
    candidate_text = {str(item.get("handle") or ""): str(item.get("text") or "") for item in candidates}
    issues: list[dict] = list(deterministic_issues)
    for item in result.get("corrections") or []:
        if not isinstance(item, dict) or not item.get("needs_fix"):
            continue
        handle = str(item.get("handle") or "")
        if handle in deterministic_handles:
            continue
        original = candidate_text.get(handle, "").strip()
        corrected = _sanitize_pipe_text_correction(
            original,
            str(item.get("corrected_text") or ""),
        )
        if not handle or not original or not corrected:
            continue
        try:
            confidence = float(item.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence < _PIPE_TEXT_LLM_MIN_CONFIDENCE:
            continue
        el = handle_map.get(handle)
        if not el:
            continue
        el["_qa_original_text"] = original
        el["_qa_text_review_reason"] = _clean_pipe_text_review_reason(
            str(item.get("reason") or "")
        )
        if "text" in el:
            el["text"] = corrected
        if "content" in el:
            el["content"] = corrected
        issue = make_pipe_annotation_text_issue(
            el,
            corrected,
            reason=el["_qa_text_review_reason"],
            confidence_score=min(max(confidence, 0.0), 0.95),
        )
        if issue:
            issues.append(issue)
    return issues


def _pipe_domain_evidence(elements: list[dict]) -> dict:
    """Compact drawing evidence for LLM domain classification and debug metadata."""
    layer_counts: Counter[str] = Counter()
    material_counts: Counter[str] = Counter()
    raw_type_counts: Counter[str] = Counter()
    block_counts: Counter[str] = Counter()
    text_samples: list[str] = []
    seen_text: set[str] = set()

    for el in elements or []:
        layer = str(el.get("layer") or "").strip()
        if layer:
            layer_counts[layer] += 1
        material = str(el.get("material") or "").strip().upper()
        if material and material != "UNKNOWN":
            material_counts[material] += 1
        raw = str(el.get("raw_type") or el.get("type") or "").strip().upper()
        if raw:
            raw_type_counts[raw] += 1
        block = str(el.get("name") or el.get("block_name") or "").strip()
        if block:
            block_counts[block] += 1
        text = str(el.get("text") or el.get("content") or "").strip()
        if text and text not in seen_text and len(text) <= 80 and len(text_samples) < 10:
            seen_text.add(text)
            text_samples.append(text)

    return {
        "layers": [name for name, _ in layer_counts.most_common(12)],
        "materials": [name for name, _ in material_counts.most_common(8)],
        "raw_types": [name for name, _ in raw_type_counts.most_common(8)],
        "blocks": [name for name, _ in block_counts.most_common(8)],
        "text_samples": text_samples,
    }


def _normalize_pipe_domain_codes(values: object) -> list[str]:
    if isinstance(values, str):
        raw_values = [values]
    elif isinstance(values, list):
        raw_values = values
    else:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for value in raw_values:
        code = str(value or "").strip().upper()
        if code in _PIPE_DOMAIN_CODES and code not in seen:
            seen.add(code)
            out.append(code)
    return out


async def _classify_pipe_domain_hint_llm(
    *,
    domain_hint: str | None,
    drawing_title: str | None,
    text_keywords: list[str] | None,
    evidence: dict | None = None,
) -> list[str]:
    """Classify natural-language drawing metadata into pipe subdomain codes."""
    exact = _normalize_pipe_domain_codes(domain_hint)
    if exact:
        return exact

    hint = str(domain_hint or "").strip()
    title = str(drawing_title or "").strip()
    text_items = [str(x).strip() for x in (text_keywords or []) if str(x or "").strip()][:8]
    ev = evidence or {}
    if not hint and not title and not text_items and not any(ev.values()):
        return []

    try:
        result = await asyncio.wait_for(
            llm_service.generate_answer(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You classify Korean/English CAD drawing metadata for a piping review agent. "
                            "Return only JSON with fields domains and reason. "
                            "domains must be an array using only these codes: GAS, WATER, DRAIN, FIRE, HVAC. "
                            "DRAIN means sanitary drain, waste, sewer, or vent piping (배수/오수/통기/위생), "
                            "and is separate from WATER supply or hot-water piping. "
                            "Use multiple codes only when the metadata clearly contains multiple piping systems. "
                            "Return an empty domains array when the metadata is too generic or not about piping."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                        {
                            "domain_hint": hint,
                            "drawing_title": title,
                            "text_keywords": text_items,
                            "drawing_evidence": ev,
                        },
                            ensure_ascii=False,
                        ),
                    },
                ],
                response_format={"type": "json_object"},
                temperature=0.0,
            ),
            timeout=12.0,
        )
    except Exception as exc:
        logging.debug("[WorkflowHandler] pipe domain LLM classification failed: %s", exc)
        return []

    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError:
            return []
    if not isinstance(result, dict):
        return []
    domains = _normalize_pipe_domain_codes(result.get("domains"))
    if domains:
        logging.info("[WorkflowHandler] pipe domain LLM classification=%s", domains)
    return domains

# confidence 임계값 — violations 분리 기준
_CONFIDENCE_THRESHOLD = 0.7

_PIPE_FALLBACK_SPEC = """\
[배관 도면검토 내장 fallback 기준]
주의: 이 문맥은 RAG 시방/법규 검색이 0건일 때만 사용하는 보수적 기본 점검 기준입니다.
공식 법규 원문을 대체하지 않으며, reference_rule에는 "배관 기본 검토 기준(내장 fallback)"으로 표시하십시오.

1. 배관 객체 식별
- 배관, 밸브, 계량기, 펌프, 탱크, 행거, 관통부, 보온재, 가스·급수·급탕·소화·공조 관련 객체만 검토 대상으로 삼습니다.
- 건축 벽체, 치수선, 문자표제, 해치, 범례, 일반 보조선은 배관 위반 대상으로 직접 지정하지 않습니다.

2. 안전·시공성 기본 점검
- 배관은 끊김, 고립 세그먼트, 연결 불일치, 중복 위치, 과도한 충돌 없이 연속성이 유지되어야 합니다.
- 밸브·차단장치·계량기 주변은 접근·점검 가능한 위치여야 하며, 임의 삭제 또는 위험 수정은 제안 레이어/수동 확인 대상으로 분류합니다.
- 관통부, 방화구획, 벽체 인접부는 충전·이격·간섭 여부를 보수적으로 점검합니다.

3. 수치 근거
- 도면에 관경, 기울기, 압력, 재질, 행거 간격, 보온 두께 같은 수치가 명확히 있는 경우에만 수치 위반을 생성합니다.
- 수치 근거가 없거나 대상 객체가 불명확하면 confidence_score를 낮추거나 MANUAL_REVIEW로 제안합니다.
"""


async def _emit_review_progress(context: dict, stage: str, message: str) -> None:
    """Send sub-step progress for the pipe review workflow when timing context exists."""
    session_id = str(context.get("progress_session_id") or "")
    t0_monotonic = context.get("progress_t0_monotonic")
    wall_start_ts = context.get("progress_wall_start_ts")
    last_t = context.get("progress_last_t")
    if not session_id or t0_monotonic is None or wall_start_ts is None or last_t is None:
        return
    try:
        context["progress_last_t"] = await emit_pipeline_step(
            session_id=session_id,
            stage=stage,
            message=message,
            t0_monotonic=float(t0_monotonic),
            wall_start_ts=float(wall_start_ts),
            last_t=float(last_t),
        )
    except Exception:
        logging.debug("[WorkflowHandler] progress emit skipped", exc_info=True)


def _build_domain_rag_queries(
    elements: list[dict],
    domain_hint: str | None = None,
    text_keywords: list[str] | None = None,
    classified_domains: list[str] | None = None,
    drawing_title: str | None = None,
) -> list[str]:
    """배관 도면 검토용 다중 RAG 쿼리를 합성합니다.

    우선순위 (앞쪽이 더 신뢰):
      1. ``classified_domains`` — drawing_title/text metadata LLM domain classification
      2. 정규화된 ``domain_hint`` enum 코드
      3. Text entity equipment/spec keywords
      4. elements ``material`` / ``layer`` 패턴
      5. 정적 ``_PIPE_CORE_RAG_QUERIES`` (recall 보강용 안전망)
      6. ``_build_rag_query(elements)`` — 단일 추론 쿼리 (앞 단계가 모두 미발동일 때)

    Text keywords are fanned out into multiple query shapes:
      - 도메인 특화 ``설치기준 규정 {kw}`` (구체)
      - ``배관 도면 검토 기준 {kw}`` (일반)

    반환은 중복 제거된 쿼리 리스트이며 ``_PIPE_MAX_RAG_QUERIES`` 상한을 적용합니다.
    """
    queries: list[str] = []
    seen: set[str] = set()

    def _add(q: str) -> None:
        if q and q not in seen:
            seen.add(q)
            queries.append(q)

    # 1순위: 자연어 메타데이터는 LLM 분류 결과만 사용한다.
    for domain in _normalize_pipe_domain_codes(classified_domains):
        _add(_DOMAIN_RAG[domain])

    # 2순위: 이미 정규화된 enum 코드만 직접 수용한다.
    for domain in _normalize_pipe_domain_codes(domain_hint):
        _add(_DOMAIN_RAG[domain])

    # 3순위: 제목/TEXT/레이어/재질에서 결정적으로 드러나는 배관 도메인.
    for domain in _infer_pipe_rag_domains(
        elements,
        drawing_title=drawing_title,
        text_keywords=text_keywords,
    ):
        _add(_DOMAIN_RAG[domain])

    # 4순위: TEXT 계열 엔티티의 장비명·규격 키워드.
    if text_keywords:
        text_primary = " ".join(text_keywords[:5])
        text_secondary = " ".join(text_keywords[5:10]) if len(text_keywords) > 5 else ""
        if text_primary:
            _add(f"배관 설비 법규 설치기준 규정 {text_primary}")
            _add(f"배관 도면 검토 기준 자재 규격 {text_primary}")
        if text_secondary:
            _add(f"배관 설비 설치기준 부속자재 {text_secondary}")

    # 5순위: elements material / layer 패턴
    materials = {str(e.get("material") or "").upper() for e in elements}
    layers    = [str(e.get("layer") or "") for e in elements]
    layer_str = " ".join(layers)

    if "GAS" in materials or _GAS_LAYER_RE.search(layer_str):
        _add(_DOMAIN_RAG["GAS"])
    if _FIRE_LAYER_WH_RE.search(layer_str):
        _add(_DOMAIN_RAG["FIRE"])
    if _WATER_LAYER_WH_RE.search(layer_str):
        _add(_DOMAIN_RAG["WATER"])
    if _DRAIN_LAYER_WH_RE.search(layer_str) or "DRAIN" in materials:
        _add(_DOMAIN_RAG["DRAIN"])
    if _HVAC_LAYER_WH_RE.search(layer_str):
        _add(_DOMAIN_RAG["HVAC"])

    # 6순위: 배관 전수 검토 공통 기준 (안전망)
    for query in _PIPE_CORE_RAG_QUERIES:
        _add(query)

    # 7순위: 위에서 어떤 쿼리도 추가되지 않거나 슬롯이 남으면 자동 추론
    if len(queries) < _PIPE_MAX_RAG_QUERIES:
        _add(_build_rag_query(elements))

    return queries[:_PIPE_MAX_RAG_QUERIES]


def _rag_result_key(row: dict) -> tuple:
    """Return a stable key for RAG dedupe without dropping same-prefix clauses."""
    source = str(row.get("source") or "")
    doc_id = row.get("document_id")
    chunk_index = row.get("chunk_index")
    section_id = row.get("section_id")
    if doc_id is not None or chunk_index is not None or section_id:
        return ("chunk", source, str(doc_id or ""), str(chunk_index or ""), str(section_id or ""))
    content = " ".join(str(row.get("content") or "").split())
    return ("content", content[:500])


def _merge_rag_batches(rag_batches: list) -> list[dict]:
    """멀티쿼리 RAG 결과를 통합·재정렬합니다.

    멀티쿼리는 같은 청크가 여러 쿼리에서 잡히면 신뢰도가 올라가지만 단순 hit_count 합산은
    낮은 품질의 청크가 다양한 쿼리에 우연히 잡힐 때 부당하게 상승하는 문제가 있습니다.
    여기서는 unique 쿼리 인덱스 수(distinct queries)를 사용해 중복 hit 부풀리기를 차단하고,
    배치 내 원본 순위(rank)를 가중치로 합산해 동률 청크 사이의 안정적 정렬을 보장합니다.

    스코어링:
      ``score = unique_hits + sum(1 / (1 + rank_in_batch))``  (높을수록 우선)

    Tie-breaker:
      - source == "temp" (사용자 업로드 시방서) 우선
      - 첫 등장 순서
    """
    merged: dict[tuple, dict] = {}
    first_order: dict[tuple, int] = {}
    rank_sum: dict[tuple, float] = {}
    order = 0
    for query_idx, batch in enumerate(rag_batches or []):
        if isinstance(batch, Exception):
            logging.warning("[WorkflowHandler] RAG query failed: %s", batch)
            continue
        for rank_in_batch, row in enumerate(batch or []):
            if not isinstance(row, dict) or not row.get("content"):
                continue
            key = _rag_result_key(row)
            if key not in merged:
                item = dict(row)
                item["_rag_hit_count"] = 0
                item["_rag_query_indices"] = []
                merged[key] = item
                first_order[key] = order
                rank_sum[key] = 0.0
                order += 1
            item = merged[key]
            indices_list = item.setdefault("_rag_query_indices", [])
            new_indices = row.get("_rag_query_indices") or [query_idx]
            indices_list.extend(new_indices)
            # unique queries 만 카운트 — 동일 쿼리에서 같은 청크가 여러 번 잡혀도 1회만
            unique_count = len({int(x) for x in indices_list if isinstance(x, int)})
            item["_rag_hit_count"] = unique_count or 1
            rank_sum[key] += 1.0 / (1.0 + rank_in_batch)

    def _score(item: dict, key: tuple) -> tuple:
        source_boost = 1 if str(item.get("source") or "") == "temp" else 0
        hit_count = int(item.get("_rag_hit_count") or 0)
        rank_score = rank_sum.get(key, 0.0)
        return (-source_boost, -(hit_count + rank_score), first_order.get(key, 10_000))

    return [
        item
        for key, item in sorted(merged.items(), key=lambda kv: _score(kv[1], kv[0]))
    ][: _PIPE_MAX_RAG_RESULTS]


def _rag_doc_key(row: dict) -> str:
    return str(row.get("document_id") or row.get("doc_name") or "").strip()


def _rag_doc_summaries(rag_results: list[dict]) -> list[dict]:
    docs: dict[str, dict] = {}
    for row in rag_results or []:
        if not isinstance(row, dict):
            continue
        key = _rag_doc_key(row)
        if not key:
            continue
        doc = docs.setdefault(
            key,
            {
                "document_id": str(row.get("document_id") or ""),
                "doc_name": str(row.get("doc_name") or ""),
                "source": str(row.get("source") or ""),
                "domain": str(row.get("domain") or ""),
                "category": str(row.get("category") or ""),
                "sections": [],
                "hit_count": 0,
                "content_preview": "",
            },
        )
        section = str(row.get("section_id") or "")
        if section and section not in doc["sections"]:
            doc["sections"].append(section)
        doc["hit_count"] += int(row.get("_rag_hit_count") or 1)
        if not doc["content_preview"]:
            doc["content_preview"] = str(row.get("content") or "")[:350]
    return list(docs.values())[:10]


async def _assess_rag_documents_llm(
    *,
    rag_results: list[dict],
    drawing_title: str | None,
    domain_hint: str | None,
    classified_domains: list[str],
    domain_evidence: dict,
) -> dict:
    """Ask the LLM whether retrieved documents fit this pipe drawing."""
    docs = _rag_doc_summaries(rag_results)
    if not docs:
        return {"documents": [], "missing_queries": []}

    try:
        result = await asyncio.wait_for(
            llm_service.generate_answer(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You evaluate whether retrieved regulation/spec documents are suitable for a CAD piping drawing review. "
                            "Return JSON only. For each document, provide document_id, doc_name, suitability_score from 0 to 1, "
                            "and a short Korean reason. Also provide missing_queries: up to 3 Korean RAG queries for important "
                            "laws/specs likely missing for this drawing. Do not invent exact law article numbers."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "drawing_title": drawing_title or "",
                                "domain_hint": domain_hint or "",
                                "classified_domains": classified_domains,
                                "drawing_evidence": domain_evidence,
                                "retrieved_documents": docs,
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                response_format={"type": "json_object"},
                temperature=0.0,
            ),
            timeout=15.0,
        )
    except Exception as exc:
        logging.debug("[WorkflowHandler] RAG document suitability LLM failed: %s", exc)
        return {"documents": [], "missing_queries": []}

    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError:
            return {"documents": [], "missing_queries": []}
    if not isinstance(result, dict):
        return {"documents": [], "missing_queries": []}

    docs_out = result.get("documents") if isinstance(result.get("documents"), list) else []
    queries = result.get("missing_queries") if isinstance(result.get("missing_queries"), list) else []
    safe_queries = [str(q).strip() for q in queries if str(q or "").strip()][:3]
    return {"documents": docs_out[:10], "missing_queries": safe_queries}


def _apply_rag_doc_assessment(rag_results: list[dict], assessment: dict) -> list[dict]:
    docs = assessment.get("documents") if isinstance(assessment, dict) else []
    score_by_key: dict[str, tuple[float, str]] = {}
    for doc in docs or []:
        if not isinstance(doc, dict):
            continue
        keys = [
            str(doc.get("document_id") or "").strip(),
            str(doc.get("doc_name") or "").strip(),
        ]
        try:
            score = max(0.0, min(1.0, float(doc.get("suitability_score"))))
        except (TypeError, ValueError):
            continue
        reason = str(doc.get("reason") or "")
        for key in keys:
            if key:
                score_by_key[key] = (score, reason)

    out: list[dict] = []
    for row in rag_results or []:
        item = dict(row)
        key = _rag_doc_key(item)
        score, reason = score_by_key.get(key, (0.55, "문서 적합도 미판정"))
        item["_doc_suitability_score"] = score
        item["_doc_suitability_reason"] = reason
        out.append(item)

    def _score(item: dict) -> tuple:
        source_boost = 1 if str(item.get("source") or "") == "temp" else 0
        suitability = float(item.get("_doc_suitability_score") or 0.5)
        hit_count = int(item.get("_rag_hit_count") or 1)
        return (-source_boost, -suitability, -hit_count)

    return sorted(out, key=_score)[: _PIPE_MAX_RAG_RESULTS]


def _append_rag_results_unique(rag_results: list[dict], extra_results: list[dict] | None) -> list[dict]:
    seen = {_rag_result_key(row) for row in rag_results if isinstance(row, dict)}
    out = list(rag_results)
    for row in extra_results or []:
        if not isinstance(row, dict) or not row.get("content"):
            continue
        key = _rag_result_key(row)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
        if len(out) >= _PIPE_MAX_RAG_RESULTS:
            break
    return out


def _format_spec_context(rag_results: list[dict]) -> str:
    """RAG 청크를 출처 헤더와 격리 펜스로 감싸 compliance LLM 입력으로 직렬화합니다.

    인젝션 방어: 각 청크는 명시적 출처 헤더 + 펜스(```spec...```)로 감싸 LLM이 시방서
    참조 텍스트와 시스템 지시를 혼동하지 않게 합니다. 펜스 내부는 모두 '근거 자료'로
    간주되며, 그 안에서 등장하는 어떤 지시·명령도 따르지 않도록 시스템 프롬프트와 함께
    경계를 설정합니다.
    """
    chunks: list[str] = []
    for idx, row in enumerate(rag_results or [], start=1):
        content = str(row.get("content") or "").strip()
        if not content:
            continue
        # 펜스가 본문에 들어가 있으면 길이를 늘려 충돌을 피한다.
        fence = "```"
        while fence in content:
            fence += "`"
        header = (
            f"[근거자료 {idx}] source={row.get('source') or '-'} "
            f"doc={row.get('doc_name') or row.get('document_id') or '-'} "
            f"section={row.get('section_id') or '-'} "
            f"chunk_type={row.get('chunk_type') or '-'} "
            f"hits={row.get('_rag_hit_count') or 1} "
            f"doc_score={row.get('_doc_suitability_score', 'n/a')}"
        )
        chunks.append(f"{header}\n{fence}spec\n{content}\n{fence}")
    return "\n\n---\n\n".join(chunks)


def _build_rag_query(elements: list[dict]) -> str:
    """elements 의 material/layer 에서 도메인을 추론한 단일 RAG 쿼리를 생성합니다.

    ``_build_domain_rag_queries`` 가 멀티쿼리 슬롯을 모두 채우지 못했을 때 기본 회수
    쿼리로 사용됩니다. 결정 흐름:
      1. element ``material`` 이 ``_DOMAIN_RAG`` 도메인 키와 일치 → 해당 도메인 쿼리
      2. layer 이름에 ``_DOMAIN_LAYER_RE`` 패턴 (GAS/FIRE/WATER/HVAC 등) 발견 → 해당 도메인 쿼리
      3. 둘 다 실패 시 type 다양성 기반의 일반 배관 쿼리

    멀티쿼리에서 이미 추가된 결과와 중복되면 ``_build_domain_rag_queries`` 의 ``_add``
    가 자동 dedupe 합니다.
    """
    materials = {str(e.get("material") or "").upper() for e in elements}
    layers    = {str(e.get("layer") or "") for e in elements}

    for domain, query in _DOMAIN_RAG.items():
        if domain in materials:
            return query
    # 레이어명으로 폴백
    for layer in layers:
        m = _DOMAIN_LAYER_RE.search(layer)
        if m:
            kw = m.group(0).upper()
            for domain, query in _DOMAIN_RAG.items():
                if kw in query.upper() or kw in domain:
                    return query
    # 일반 배관
    type_set = list({e.get("type", "") for e in elements if e.get("type")})[:4]
    return f"배관 설비 이격거리 설치위치 직경 기준 규정: {', '.join(type_set)}"


class PipeWorkflowHandler:
    def __init__(self, session, db: AsyncSession):
        """
        session : 채팅 세션 컨텍스트 (org_id, current_drawing_id, raw_layout_data 등 포함)
        db      : AsyncSession (vector_service 검색용)
        """
        self.session = session
        self.db = db

        self.query_agent      = QueryAgent(db)
        self.parser_agent     = ParserAgent()
        self.compliance_agent = ComplianceAgent()
        self.report_agent     = ReportAgent()
        self.revision_agent   = RevisionAgent()
        self.action_agent     = ActionAgent()

    async def handle_tool_calls(
        self, tool_calls: list, context: Dict[str, Any]
    ) -> list:
        import json
        final_actions = []

        for call in tool_calls:
            func_name = call["function"]["name"]
            raw_args  = call["function"].get("arguments", "{}")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                args = {}

            # ── call_query_agent ──────────────────────────────────────────
            if func_name == "call_query_agent":
                try:
                    query_limit = int(args.get("limit") or 8)
                except (TypeError, ValueError):
                    query_limit = 8
                query_limit = min(max(query_limit, 5), 12)
                result = await self.query_agent.execute(
                    args.get("query", ""),
                    spec_guid=context.get("spec_guid"),
                    org_id=context.get("org_id"),
                    domain="pipe",
                    limit=query_limit,
                )
                logging.info(
                    "[PipingDebug] call_query_agent chunks=%s",
                    len(result) if isinstance(result, list) else 0,
                )
                final_actions.append({"agent": "query", "result": result})

            # ── call_review_agent ─────────────────────────────────────────
            elif func_name == "call_review_agent":
                target_id = args.get("target_id", "ALL")
                import time
                t_start = time.time()

                # layout_data: LLM 인자 → context fallback (LLM은 spec_context·layout_data를 제공하지 않아도 됨)
                raw_layout = context.get("raw_layout_data", "{}")
                # [DEBUG] 원본 데이터에 존재하는 모든 레이어명 확인
                try:
                    raw_json = json.loads(raw_layout)
                    raw_ents = raw_json.get("entities") or raw_json.get("elements") or []
                    all_raw_layers = {str(e.get("layer") or "0") for e in raw_ents if isinstance(e, dict)}
                    logging.info("[Workflow Raw Debug] ALL layers sent from CAD: %s", sorted(list(all_raw_layers)))
                except Exception as exc:
                    logging.debug("[Workflow Raw Debug] raw layer logging skipped: %s", exc)

                # 1. 도면 파싱 (C# 엔티티 형식 → 정규화)
                t0 = time.time()
                parsed = self.parser_agent.parse(
                    raw_layout,
                    mapping_table=context.get("mapping_table"),
                    layer_resolved_roles=context.get("layer_resolved_roles"),
                )
                elements = parsed.get("elements", [])
                _enrich_with_object_mapping(elements, context.get("object_mapping") or [])
                await _emit_review_progress(
                    context,
                    "pipe_review_parse",
                    f"도면 요소 파싱 완료 — 검토 대상 {len(elements)}개",
                )
                
                # [DEBUG LOG] 검증기로 넘어가기 전 최종 데이터 상태
                final_layers = {e.get("layer") for e in elements}
                logging.info(
                    "[Workflow Debug] Final elements count for checker: %d | Remaining Layers: %s",
                    len(elements), sorted(list(final_layers))
                )
                t_parser = time.time() - t0

                if not elements:
                    logging.warning(
                        "[WorkflowHandler] 파싱된 요소 없음 — "
                        "drawing_data에 'entities' 키가 있는지 확인하세요."
                    )

                # 1b+1c+RAG — topology(CPU)·geometry(CPU)·RAG(I/O) 동시 실행
                effective_target = target_id
                _drawing_data = context.get("drawing_data") or {}
                if "unit_to_mm_factor" not in _drawing_data:
                    _drawing_data["unit_to_mm_factor"] = _resolve_unit_to_mm_factor(_drawing_data)
                _raw_unit = _drawing_data.get("unit_to_mm_factor")
                if _raw_unit is None:
                    logging.warning(
                        "[WorkflowHandler] unit_to_mm_factor 없음 — 기본값 1.0 적용 (mm 단위 가정). "
                        "도면 단위가 인치/feet라면 치수 검사 결과가 부정확할 수 있습니다."
                    )
                _unit_factor = float(_raw_unit or 1.0)
                _text_enriched = _enrich_nearby_pipe_lines_from_text(elements, _unit_factor)
                if _text_enriched:
                    logging.info(
                        "[WorkflowHandler] TEXT/MLEADER pipe attributes propagated to %d line elements",
                        _text_enriched,
                    )
                _symbol_marked = _mark_user_drawn_pipe_symbols(elements, _unit_factor)
                if _symbol_marked:
                    logging.info(
                        "[WorkflowHandler] user-drawn pipe symbol pieces excluded from topology: %d",
                        _symbol_marked,
                    )

                # #4 멀티쿼리: domain_hint + drawing_title + TEXT 키워드 통합
                text_qa_issues = await _review_pipe_annotation_texts_llm(elements)
                if text_qa_issues:
                    logging.info(
                        "[WorkflowHandler] pipe annotation text LLM QA issues=%d",
                        len(text_qa_issues),
                    )

                domain_hint    = _drawing_data.get("domain_hint") or None
                drawing_title  = (
                    _drawing_data.get("drawing_title")
                    or _extract_drawing_title_from_raw(_drawing_data)
                    or None
                )
                text_keywords  = _extract_text_keywords(elements)
                drawing_type   = _classify_pipe_drawing_type(drawing_title, text_keywords)
                if drawing_title:
                    parsed["drawing_title"] = drawing_title
                parsed["drawing_type"] = drawing_type
                domain_evidence = _pipe_domain_evidence(elements)
                if text_keywords:
                    logging.info("[WorkflowHandler] text keywords for RAG: %s", text_keywords)
                classified_domains = await _classify_pipe_domain_hint_llm(
                    domain_hint=domain_hint,
                    drawing_title=drawing_title,
                    text_keywords=text_keywords,
                    evidence=domain_evidence,
                )
                rag_queries = _build_domain_rag_queries(
                    elements,
                    domain_hint=domain_hint,
                    text_keywords=text_keywords,
                    classified_domains=classified_domains,
                    drawing_title=drawing_title,
                )
                logging.info(
                    "[WorkflowHandler] RAG queries (multi-domain, %d, classified=%s): %s",
                    len(rag_queries), classified_domains, rag_queries,
                )
                rag_concurrency = _pipe_rag_concurrency(len(rag_queries))
                await _emit_review_progress(
                    context,
                    "pipe_review_rag_topology",
                    (
                        "Topology/Geometry 병렬 + RAG 병렬 검색 시작 — "
                        f"RAG 쿼리 {len(rag_queries)}개, 동시 {rag_concurrency}개"
                    ),
                )

                t0_parallel = time.time()

                # 병렬 실행: topology + geometry + RAG worker.
                # RAG 내부 질의는 별도 AsyncSession으로 동시 실행한다.
                async def _run_one_rag_query(q: str) -> list[dict]:
                    async with SessionLocal() as rag_db:
                        return await QueryAgent(rag_db).execute(
                            q,
                            spec_guid=context.get("spec_guid"),
                            org_id=context.get("org_id"),
                            domain="pipe",
                            limit=_PIPE_RAG_LIMIT_PER_QUERY,
                        )

                async def _run_rag_queries_parallel(queries: list[str]) -> list:
                    if not queries:
                        return []
                    sem = asyncio.Semaphore(_pipe_rag_concurrency(len(queries)))

                    async def _one(q: str) -> list | Exception:
                        async with sem:
                            try:
                                return await _run_one_rag_query(q)
                            except Exception as exc:
                                return exc

                    return await asyncio.gather(*(_one(q) for q in queries))

                parallel_tasks = [
                    asyncio.to_thread(
                        PipeTopologyBuilder().build, elements, _unit_factor  # #3
                    ),
                    asyncio.to_thread(
                        GeometryPreprocessor(unit_factor=_unit_factor).process,
                        elements,
                        arch_elements=parsed.get("arch_elements"),
                    ),
                    _run_rag_queries_parallel(rag_queries),
                ]
                parallel_results = await asyncio.gather(*parallel_tasks, return_exceptions=True)

                # topology / geo 실패 시 방어적 기본값
                topology = (
                    parallel_results[0]
                    if not isinstance(parallel_results[0], Exception)
                    else {"pipe_runs": [], "summary": {"run_count": 0, "unconnected_lines": 0, "block_count": 0}}
                )
                if isinstance(parallel_results[0], Exception):
                    logging.error("[WorkflowHandler] topology 빌드 실패: %s", parallel_results[0])

                geo = (
                    parallel_results[1]
                    if not isinstance(parallel_results[1], Exception)
                    else {"mep_clearances": [], "wall_clearances": [], "proxy_walls": []}
                )
                if isinstance(parallel_results[1], Exception):
                    logging.error("[WorkflowHandler] geometry 빌드 실패: %s", parallel_results[1])

                # 멀티 RAG 결과 병합 — 반복 검색된 청크와 사용자 업로드 시방서 청크를 우선한다.
                rag_batches = [] if isinstance(parallel_results[2], Exception) else (parallel_results[2] or [])
                if isinstance(parallel_results[2], Exception):
                    logging.warning("[WorkflowHandler] RAG queries failed: %s", parallel_results[2])
                rag_results = _merge_rag_batches(rag_batches)
                rag_doc_assessment = await _assess_rag_documents_llm(
                    rag_results=rag_results,
                    drawing_title=drawing_title,
                    domain_hint=domain_hint,
                    classified_domains=classified_domains,
                    domain_evidence=domain_evidence,
                )
                supplemental_queries = [
                    q for q in (rag_doc_assessment.get("missing_queries") or [])
                    if q and q not in rag_queries
                ][:3]
                if supplemental_queries:
                    logging.info(
                        "[WorkflowHandler] supplemental RAG queries from doc assessment: %s",
                        supplemental_queries,
                    )
                    supplemental_batches = await _run_rag_queries_parallel(supplemental_queries)
                    rag_results = _merge_rag_batches([rag_results, *supplemental_batches])
                rag_results = _apply_rag_doc_assessment(rag_results, rag_doc_assessment)
                t_parallel = time.time() - t0_parallel
                await _emit_review_progress(
                    context,
                    "pipe_review_rag_topology",
                    (
                        "Topology/Geometry 병렬 + RAG 병렬 검색 완료 "
                        f"(runs={(topology.get('summary') or {}).get('run_count', 0)}, "
                        f"unconnected={(topology.get('summary') or {}).get('unconnected_lines', 0)}, "
                        f"RAG={len(rag_results)}건)"
                    ),
                )

                # topology 결과 주입
                parsed["pipe_topology"] = topology
                _topo_summary = topology.get("summary") or {}
                logging.info(
                    "[WorkflowHandler] topology runs=%d unconnected=%d blocks=%d | parallel=%.2fs",
                    _topo_summary.get("run_count", 0),
                    _topo_summary.get("unconnected_lines", 0),
                    _topo_summary.get("block_count", 0),
                    t_parallel,
                )

                # geometry 결과 주입
                parsed["mep_clearances"] = geo["mep_clearances"]
                parsed["wall_clearances"] = geo["wall_clearances"]
                if not parsed.get("arch_elements") and geo["proxy_walls"]:
                    parsed["arch_elements"] = geo["proxy_walls"]
                    logging.info(
                        "[WorkflowHandler] arch_elements 없음 → proxy_walls %d개 주입",
                        len(geo["proxy_walls"]),
                    )

                # RAG 결과
                fallback_spec_used = False
                spec_context = _format_spec_context(rag_results)

                if not spec_context:
                    try:
                        fallback_query = (
                            "배관 설비 일반 기준 관경 재질 압력 기울기 지지 간격 "
                            "밸브 위치 관통부 방화 충전 이격거리"
                        )
                        fallback_results = await _run_one_rag_query(fallback_query)
                        rag_results = _append_rag_results_unique(rag_results, fallback_results)
                        spec_context = _format_spec_context(rag_results)
                        logging.info(
                            "[WorkflowHandler] fallback RAG results=%d spec_context=%s",
                            len(fallback_results or []),
                            bool(spec_context),
                        )
                    except Exception as exc:
                        logging.warning("[WorkflowHandler] fallback RAG query failed: %s", exc)

                if not spec_context:
                    fallback_spec_used = True
                    spec_context = _PIPE_FALLBACK_SPEC
                    logging.warning(
                        "[WorkflowHandler] spec_context RAG 결과 없음 "
                        "(target_id=%s). 내장 fallback 기준은 메타로만 표시하고 compliance LLM은 건너뜁니다.",
                        effective_target,
                    )
                # 4. Run compliance LLM while deterministic/QA checks run in parallel.
                async def _run_compliance_checks() -> tuple[list, float]:
                    t0_inner = time.time()
                    if fallback_spec_used:
                        logging.info(
                            "[WorkflowHandler] authoritative RAG unavailable; "
                            "skipping compliance LLM on built-in fallback target=%s",
                            effective_target,
                        )
                        return [], time.time() - t0_inner
                    try:
                        result = await asyncio.wait_for(
                            self.compliance_agent.check_compliance_parsed(
                                effective_target, spec_context, parsed
                            ),
                            timeout=120.0,
                        )
                    except asyncio.TimeoutError:
                        logging.error(
                            "[WorkflowHandler] compliance timeout(120s) target=%s; returning empty violations",
                            effective_target,
                        )
                        result = []
                    except Exception as exc:
                        logging.exception("[WorkflowHandler] compliance failed: %s", exc)
                        result = []
                    return result, time.time() - t0_inner

                async def _run_deterministic_checks() -> tuple[list, float]:
                    t0_inner = time.time()
                    try:
                        result = await asyncio.to_thread(
                            run_deterministic_checks,
                            elements,
                            topology,
                            geo,
                            unit_factor=_unit_factor,
                        )
                    except Exception as exc:
                        logging.exception("[WorkflowHandler] deterministic checks failed: %s", exc)
                        result = []
                    return result, time.time() - t0_inner

                async def _run_qa_checks() -> tuple[list, float]:
                    t0_inner = time.time()
                    try:
                        result = await asyncio.to_thread(
                            run_drawing_qa_checks,
                            elements,
                            topology,
                            geo,
                            unit_factor=_unit_factor,
                        )
                    except Exception as exc:
                        logging.exception("[WorkflowHandler] drawing QA checks failed: %s", exc)
                        result = []
                    return result, time.time() - t0_inner

                compliance_task = asyncio.create_task(_run_compliance_checks())
                det_task = asyncio.create_task(_run_deterministic_checks())
                qa_task = asyncio.create_task(_run_qa_checks())

                continuity_filter_diagnostics: list[dict] = []
                candidate_drawing_quality_count = 0

                det_violations, t_det = await det_task
                det_violations = _filter_weak_generic_continuity_violations(
                    det_violations,
                    elements,
                    source_name="deterministic",
                    diagnostics=continuity_filter_diagnostics,
                )
                await _emit_review_progress(
                    context,
                    "pipe_review_deterministic",
                    f"결정 규칙 검사 완료 - {len(det_violations)}건",
                )

                qa_issues, t_qa = await qa_task
                candidate_drawing_quality_count = len(qa_issues)
                if text_qa_issues:
                    by_key = {
                        (
                            str(issue.get("equipment_id") or ""),
                            str(issue.get("violation_type") or issue.get("issue_type") or ""),
                        ): issue
                        for issue in [*text_qa_issues, *qa_issues]
                    }
                    qa_issues = list(by_key.values())
                qa_issues = _filter_weak_generic_continuity_violations(
                    qa_issues,
                    elements,
                    source_name="drawing_qa",
                    diagnostics=continuity_filter_diagnostics,
                )
                candidate_drawing_quality_diagnostics = [
                    d for d in continuity_filter_diagnostics
                    if d.get("source") == "drawing_qa"
                ]
                await _emit_review_progress(
                    context,
                    "pipe_review_qa",
                    f"도면 QA 검사 완료 - QA 이슈 {len(qa_issues)}건",
                )

                llm_violations, t_compliance = await compliance_task
                llm_violations = _filter_weak_generic_continuity_violations(
                    llm_violations,
                    elements,
                    source_name="llm",
                    diagnostics=continuity_filter_diagnostics,
                )
                compliance_message = (
                    "시방/규정 검증 생략 - RAG 근거 없음(내장 fallback 위반 생성 차단)"
                    if fallback_spec_used
                    else f"시방/규정 검증 완료 - LLM 후보 {len(llm_violations)}건"
                )
                await _emit_review_progress(
                    context,
                    "pipe_review_compliance",
                    compliance_message,
                )

                # LLM + 확정적 violations + QA issues 병합 (handle+type 중복 제거)
                _seen_viol: set[tuple] = {
                    (str(v.get("equipment_id") or ""), str(v.get("violation_type") or ""))
                    for v in llm_violations
                }
                merged_violations = list(llm_violations)
                for dv in [*det_violations, *qa_issues]:
                    key = (str(dv.get("equipment_id") or ""), str(dv.get("violation_type") or ""))
                    if key not in _seen_viol:
                        merged_violations.append(dv)
                        _seen_viol.add(key)

                # #5 confidence 기준 분리
                high_conf = [
                    v for v in merged_violations
                    if float(v.get("confidence_score") or 1.0) >= _CONFIDENCE_THRESHOLD
                ]
                low_conf = [
                    v for v in merged_violations
                    if float(v.get("confidence_score") or 1.0) < _CONFIDENCE_THRESHOLD
                ]
                logging.info(
                    "[WorkflowHandler] violations total=%d (high=%d low=%d det=%d qa=%d)",
                    len(merged_violations), len(high_conf), len(low_conf), len(det_violations), len(qa_issues),
                )

                # 5. 리포트 생성 (high confidence 기준)
                t0 = time.time()
                drawing_id = (
                    context.get("current_drawing_id")
                    or _drawing_data.get("drawing_number")
                    or ""
                )
                report = self.report_agent.generate(high_conf, drawing_id=drawing_id)
                t_report = time.time() - t0
                await _emit_review_progress(
                    context,
                    "pipe_review_report",
                    f"검토 리포트 생성 완료 — 표시 항목 {len((report or {}).get('items') or [])}건",
                )

                # 6. 수정 대안 계산
                t0 = time.time()
                current_layout = {
                    el.get("id") or el.get("handle", ""): el.get("position") or {}
                    for el in elements
                }
                fixes = self.revision_agent.calculate_fix(high_conf, current_layout)
                t_revision = time.time() - t0
                await _emit_review_progress(
                    context,
                    "pipe_review_revision",
                    f"수정안 계산 완료 — 수정 후보 {len(fixes)}건",
                )

                t_total = time.time() - t_start
                logging.info(
                    "[PipingTracker] Parser=%.2fs Parallel=%.2fs Compliance=%.2fs Det=%.2fs QA=%.2fs Report=%.2fs Revision=%.2fs | Total=%.2fs",
                    t_parser, t_parallel, t_compliance, t_det, t_qa, t_report, t_revision, t_total,
                )
                logging.info(
                    "[PipingDebug] call_review_agent report_items=%s fixes=%s low_conf=%s",
                    len((report or {}).get("items") or []),
                    len(fixes or []),
                    len(low_conf),
                )
                final_actions.append({
                    "agent": "review",
                    "result": {
                        "report":  report,
                        "fixes":   fixes,
                        "rag_references": rag_results,
                        # #5 confidence 분리 — UI에서 탭 분리 가능
                        "low_confidence_violations": low_conf,
                        # #1 확정적 위반 — 별도 표시용
                        "deterministic_violations": det_violations,
                        # 도면 품질검사 — 법규/시방과 별도 표시용
                        "drawing_quality_issues": qa_issues,
                        "meta": {
                            "unit_factor":        _unit_factor,
                            "rag_domain_count":   len(rag_queries),
                            "rag_query_count":    len(rag_queries),
                            "rag_result_count":   len(rag_results),
                            "rag_classified_domains": classified_domains,
                            "rag_domain_evidence": domain_evidence,
                            "rag_doc_assessment": rag_doc_assessment,
                            "rag_supplemental_queries": supplemental_queries,
                            "total_violations":   len(merged_violations),
                            "high_conf_count":    len(high_conf),
                            "low_conf_count":     len(low_conf),
                            "deterministic_count": len(det_violations),
                            "drawing_quality_count": len(qa_issues),
                            "candidate_drawing_quality_count": candidate_drawing_quality_count,
                            "visible_drawing_quality_count": len(qa_issues),
                            "candidate_drawing_quality_suppressed_count": len(candidate_drawing_quality_diagnostics),
                            "candidate_drawing_quality_diagnostics": candidate_drawing_quality_diagnostics[:50],
                            "continuity_filter_suppressed_count": len(continuity_filter_diagnostics),
                            "continuity_filter_diagnostics": continuity_filter_diagnostics[:50],
                            "rag_fallback_spec_used": fallback_spec_used,
                        },
                    },
                })

            # ── call_action_agent ─────────────────────────────────────────
            # 결과 fixes[].handle + auto_fix → state.pending_fixes → (별도) REVIEW_RESULT UI/C# RevCloud, 적용은 APPROVE_FIX
            elif func_name == "call_action_agent":
                result = await self.action_agent.analyze_and_fix(context, domain="pipe")
                fixes = (result or {}).get("fixes") or []
                logging.info(
                    "[PipingDebug] call_action_agent fixes=%d sample_handles=%s",
                    len(fixes),
                    [f.get("handle") for f in fixes[:5]],
                )
                final_actions.append({"agent": "action", "result": result})

            # ── get_cad_entity_info ───────────────────────────────────────
            elif func_name == "get_cad_entity_info":
                from backend.services.agents.common.tools.common_tools import get_cad_entity_info_tool
                import json as _json
                handle = args.get("handle", "")
                # 건축/배관 분리 후 raw_layout은 mep subset일 수 있음 — 핸들 조회는 전체 drawing_data 우선
                full_dd = context.get("drawing_data")
                if isinstance(full_dd, dict) and (full_dd.get("entities") or full_dd.get("elements")):
                    drawing_data_str = _json.dumps(
                        {"entities": full_dd.get("entities") or full_dd.get("elements") or []},
                        ensure_ascii=False,
                    )
                else:
                    raw_layout = context.get("raw_layout_data", "{}")
                    if isinstance(raw_layout, dict):
                        drawing_data_str = _json.dumps(raw_layout, ensure_ascii=False)
                    else:
                        drawing_data_str = raw_layout or "{}"
                # get_cad_entity_info_tool 은 LangChain @tool 이므로 .invoke 로 호출
                result_str = get_cad_entity_info_tool.invoke({"handle": handle, "drawing_data": drawing_data_str})
                final_actions.append({"agent": "cad_info", "result": result_str})

        return final_actions
