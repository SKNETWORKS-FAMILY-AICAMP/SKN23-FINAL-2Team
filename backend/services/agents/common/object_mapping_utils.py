"""
File    : backend/services/agents/common/object_mapping_utils.py
Description : TEXT↔BLOCK 객체 매핑 공통 유틸리티
              - 4개 도메인(배관/전기/건축/소방) 공용
              - OOM 방지: 동시 매핑 작업 수를 전역 세마포어로 제한
              - run_object_mapping  : drawing_data 에서 직접 추출·매핑 (node용)
              - map_texts_to_blocks : 사전 추출된 엔티티 매핑 (parser용)
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from backend.services.agents.common.multi_object_mapper import (
    LayerBonusConfig,
    auto_map_entities,
)

_log = logging.getLogger(__name__)

_REPORT_ENTITY_TYPES = (
    "LINE",
    "POLYLINE",
    "LWPOLYLINE",
    "ARC",
    "CIRCLE",
    "TEXT",
    "MTEXT",
    "DIMENSION",
)

_REPORT_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "LINE": ("start", "end"),
    "POLYLINE": ("vertices",),
    "LWPOLYLINE": ("vertices",),
    "ARC": ("center", "radius"),
    "CIRCLE": ("center", "radius"),
    "TEXT": ("text",),
    "MTEXT": ("text",),
    "DIMENSION": ("measurement",),
}

# ── 도메인별 레이어 보너스 (공용 상수) ────────────────────────────────────────
# (과거 특정 레이어 가산점이 있었으나, 모호성 제거를 위해 보수적으로 운영)
PIPE_LAYER_BONUS = None
ELEC_LAYER_BONUS = None
ARCH_LAYER_BONUS = LayerBonusConfig(block_layer="A-AREA", text_layer="A-ANNO", bonus=15.0)
# 소방(fire): 레이어 보너스 없음 → None 전달

# ── OOM 방지: 전역 동시 매핑 세마포어 (lazy init) ─────────────────────────────
# asyncio.Semaphore 는 이벤트 루프 내에서 생성해야 하므로 첫 호출 시 초기화
_MAPPING_SEM: asyncio.Semaphore | None = None
_MAPPING_MAX_CONCURRENT = 4  # 다중 사용자 동시 요청 시 메모리 초과 방지


def _get_mapping_sem() -> asyncio.Semaphore:
    global _MAPPING_SEM
    if _MAPPING_SEM is None:
        _MAPPING_SEM = asyncio.Semaphore(_MAPPING_MAX_CONCURRENT)
    return _MAPPING_SEM


# ── 공통 텍스트 정제 ──────────────────────────────────────────────────────────
def _clean_text(raw: str) -> str:
    """MTEXT RTF escape 제거 후 실제 텍스트 반환."""
    return re.sub(r"\\[A-Za-z0-9]+;", "", raw).strip()


def build_drawing_test_report(
    drawing_data: dict | list[dict],
    *,
    max_samples: int = 20,
) -> dict[str, Any]:
    from backend.services.agents.elec.sub.elec_attr_extractor import extract_elec_attrs

    elements = _report_elements(drawing_data)
    entity_counts = {key: 0 for key in _REPORT_ENTITY_TYPES}
    dimension_measurements: list[float | int] = []
    electrical_annotations = {
        "wire_size": [],
        "cable_sqmm": [],
        "pole_options": [],
        "poles": [],
        "bolt_size": [],
        "label_keys": [],
    }
    parser_missing_fields: list[dict[str, Any]] = []
    sample_entities: list[dict[str, Any]] = []

    for el in elements:
        raw_type = str(el.get("raw_type") or el.get("type") or "").upper()
        if raw_type in entity_counts:
            entity_counts[raw_type] += 1

        missing = _missing_report_fields(el, raw_type)
        if missing:
            parser_missing_fields.append({
                "handle": str(el.get("handle") or el.get("id") or ""),
                "type": raw_type,
                "missing": missing,
            })

        if raw_type == "DIMENSION":
            measurement = _report_float(el.get("measurement"))
            if measurement is None:
                measurement = _first_number(str(el.get("text") or el.get("content") or ""))
            if measurement is not None:
                dimension_measurements.append(_compact_number(measurement))

        text = str(el.get("text") or el.get("content") or "").strip()
        if text:
            _merge_annotation_attrs(electrical_annotations, extract_elec_attrs(text))

        if isinstance(el.get("elec_attrs"), dict):
            _merge_annotation_attrs(electrical_annotations, el["elec_attrs"])

        if len(sample_entities) < max_samples and raw_type in _REPORT_ENTITY_TYPES:
            sample_entities.append(_sample_entity(el, raw_type))

    topology = None
    if isinstance(drawing_data, dict):
        topology = drawing_data.get("elec_topology") or drawing_data.get("topology")
    terminal_candidates = []
    if isinstance(topology, dict):
        terminal_candidates = topology.get("terminal_candidates") or []
    if not terminal_candidates:
        try:
            from backend.services.agents.elec.sub.topology import detect_terminal_candidates

            terminal_candidates = detect_terminal_candidates(elements)
        except Exception as exc:  # noqa: BLE001
            _log.warning("[DrawingTestReport] terminal candidate fallback failed: %s", exc)
    total_entity_count = sum(entity_counts.values())
    critical_flags = [
        flag
        for flag in _qa_flags(entity_counts, dimension_measurements, electrical_annotations)
        if flag in {"dimension_without_measurement", "text_without_electrical_annotation"}
    ]

    return {
        "entity_counts": entity_counts,
        "dimension_measurements": _unique_keep_order(dimension_measurements),
        "electrical_annotations": electrical_annotations,
        "parser_missing_fields": parser_missing_fields,
        "terminal_candidates": terminal_candidates,
        "summary": {
            "total_entity_count": total_entity_count,
            "terminal_candidate_count": len(terminal_candidates),
            "parser_missing_field_count": len(parser_missing_fields),
            "critical_qa_flag_count": len(critical_flags),
        },
        "qa_flags": _qa_flags(entity_counts, dimension_measurements, electrical_annotations),
        "sample_entities": sample_entities,
    }


def _report_elements(drawing_data: dict | list[dict]) -> list[dict]:
    if isinstance(drawing_data, list):
        return [e for e in drawing_data if isinstance(e, dict)]
    if not isinstance(drawing_data, dict):
        return []
    mep_review = drawing_data.get("mep_review") if isinstance(drawing_data.get("mep_review"), dict) else {}
    raw = (
        drawing_data.get("entities")
        or drawing_data.get("elements")
        or drawing_data.get("clean_entities")
        or mep_review.get("entities")
        or []
    )
    return [e for e in raw if isinstance(e, dict)]


def _missing_report_fields(el: dict, raw_type: str) -> list[str]:
    missing = []
    for field in _REPORT_REQUIRED_FIELDS.get(raw_type, ()):
        if field == "vertices" and (el.get("vertices") or el.get("points")):
            continue
        if field == "text" and (el.get("text") or el.get("content")):
            continue
        if field == "measurement" and (
            el.get("measurement") is not None
            or _first_number(str(el.get("text") or el.get("content") or "")) is not None
        ):
            continue
        if el.get(field) is None:
            missing.append(field)
    return missing


def _merge_annotation_attrs(target: dict[str, list], attrs: dict[str, Any]) -> None:
    if attrs.get("wire_size"):
        _append_unique(target["wire_size"], str(attrs["wire_size"]))
    if attrs.get("cable_sqmm") is not None:
        _append_unique(target["cable_sqmm"], _compact_number(float(attrs["cable_sqmm"])))
    for pole in attrs.get("pole_options") or []:
        _append_unique(target["pole_options"], str(pole))
        _append_unique(target["poles"], str(pole))
    if attrs.get("bolt_size"):
        _append_unique(target["bolt_size"], str(attrs["bolt_size"]))
    for key in attrs.get("label_keys") or []:
        _append_unique(target["label_keys"], str(key))


def _sample_entity(el: dict, raw_type: str) -> dict[str, Any]:
    keys = (
        "handle", "id", "type", "raw_type", "layer", "position", "start", "end",
        "vertices", "center", "radius", "measurement", "text", "insert_point",
    )
    sample = {k: el[k] for k in keys if k in el}
    sample.setdefault("type", raw_type)
    return sample


def _qa_flags(
    entity_counts: dict[str, int],
    measurements: list[float | int],
    annotations: dict[str, list],
) -> list[str]:
    flags: list[str] = []
    if entity_counts.get("DIMENSION", 0) and not measurements:
        flags.append("dimension_without_measurement")
    if (entity_counts.get("TEXT", 0) or entity_counts.get("MTEXT", 0)) and not any(
        annotations[k] for k in ("wire_size", "pole_options", "bolt_size", "label_keys")
    ):
        flags.append("text_without_electrical_annotation")
    if entity_counts.get("CIRCLE", 0) and entity_counts["CIRCLE"] < 4:
        flags.append("circle_count_below_terminal_candidate_min")
    return flags


def _report_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_number(text: str) -> float | None:
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text or "")
    return float(match.group(0)) if match else None


def _compact_number(value: float) -> float | int:
    return int(value) if float(value).is_integer() else value


def _append_unique(target: list, value: Any) -> None:
    if value not in target:
        target.append(value)


def _unique_keep_order(values: list[Any]) -> list[Any]:
    result: list[Any] = []
    for value in values:
        _append_unique(result, value)
    return result


# ── node 파일용: drawing_data → 엔티티 추출 → 매핑 ───────────────────────────
async def run_object_mapping(
    drawing_data: dict,
    *,
    domain_hint: str,
    log_prefix: str,
    layer_bonus_config: LayerBonusConfig | None = None,
    filter_arch_layers: bool = False,
    ambiguity_threshold: float = 30.0,
    max_distance_mm: float = 10_000.0,
) -> list[dict[str, Any]]:
    """
    drawing_data 에서 TEXT/MTEXT·BLOCK 엔티티를 추출하고
    거리·레이어 점수 기반 자동 매핑 + 모호 케이스 LLM fallback 을 수행합니다.

    Parameters
    ----------
    drawing_data       : CAD 파싱 결과 (entities 또는 elements 키 포함)
    domain_hint        : LLM fallback 힌트 ("배관" | "전기" | "건축" | "소방")
    log_prefix         : 로그 접두사 ("[PipingNode]" 등)
    layer_bonus_config : 도메인별 레이어 가산점 설정 (없으면 None)
    filter_arch_layers : True 면 entity_layer_role=="arch" 엔티티 제거 (배관 전용)
    ambiguity_threshold: 모호 판정 점수 차 임계값
    max_distance_mm    : 매핑 최대 거리 (mm)

    Returns
    -------
    [{"text_handle", "block_handle", "label", "score", "method"}, ...]
    """
    from backend.services.agents.common.mapping import _is_ignored_layer

    raw: list = drawing_data.get("entities") or drawing_data.get("elements") or []

    # 건축 레이어 필터 (배관 전용: 건축 엔티티와 섞이지 않도록)
    arch_handles: set[str] = set()
    if filter_arch_layers:
        from backend.services.arch_pipe_layer_split import split_entities_by_layer_role
        # 고도화된 통계 기반 분류기 사용 (프로젝트별 혼합 레이어 대응)
        arch_list, _, _, _, _ = split_entities_by_layer_role(
            raw, drawing_data=drawing_data
        )
        arch_handles = {str(e.get("handle", "")) for e in arch_list}

    text_ents = [
        e for e in raw
        if e.get("type") in ("TEXT", "MTEXT")
        and not _is_ignored_layer(str(e.get("layer", "")))
        and str(e.get("handle", "")) not in arch_handles
    ]
    block_ents = [
        e for e in raw
        if e.get("type") == "BLOCK"
        and not _is_ignored_layer(str(e.get("layer", "")))
        and str(e.get("handle", "")) not in arch_handles
    ]

    if not text_ents or not block_ents:
        _log.info("%s 4b 스킵 — 설비 텍스트 또는 블록 없음 (보조 레이어 제외 후)", log_prefix)
        return []

    _log.info(
        "%s 4b 매핑 시작 — TEXT:%d BLOCK:%d (보조 레이어 제외)",
        log_prefix, len(text_ents), len(block_ents),
    )

    score_kwargs: dict[str, Any] = {"max_distance_mm": max_distance_mm}
    if layer_bonus_config is not None:
        score_kwargs["layer_bonus_config"] = layer_bonus_config

    text_lookup = {str(e.get("handle", "")): e for e in text_ents}

    async with _get_mapping_sem():
        results = await auto_map_entities(
            text_ents,
            block_ents,
            ambiguity_threshold=ambiguity_threshold,
            domain_hint=domain_hint,
            score_kwargs=score_kwargs,
        )

    # auto_map_entities 결과에 label 필드 추가
    for r in results:
        t = text_lookup.get(r.get("text_handle", ""), {})
        r["label"] = _clean_text(t.get("text", ""))

    auto_n = sum(1 for r in results if r.get("method") == "auto")
    llm_n  = len(results) - auto_n
    print(f"{log_prefix} 4b 완료 — 총 {len(results)}쌍 (자동={auto_n}, LLM={llm_n})")
    return results


# ── parser 파일용: 사전 추출 엔티티 → 매핑 ───────────────────────────────────
async def map_texts_to_blocks(
    text_entities: list[dict],
    block_entities: list[dict],
    *,
    domain_hint: str,
    layer_bonus_config: LayerBonusConfig | None = None,
    ambiguity_threshold: float = 10.0,
) -> list[dict[str, Any]]:
    """
    사전 추출된 text/block 엔티티 목록을 매핑합니다 (arch·fire 파서용).
    자동 매핑 + 모호 케이스 LLM fallback, label 필드 포함.
    """
    score_kwargs: dict[str, Any] = {}
    if layer_bonus_config is not None:
        score_kwargs["layer_bonus_config"] = layer_bonus_config

    text_lookup = {str(e.get("handle", "")): e for e in text_entities}

    async with _get_mapping_sem():
        results = await auto_map_entities(
            text_entities,
            block_entities,
            ambiguity_threshold=ambiguity_threshold,
            domain_hint=domain_hint,
            score_kwargs=score_kwargs,
        )

    for r in results:
        t = text_lookup.get(r.get("text_handle", ""), {})
        r["label"] = _clean_text(t.get("text", ""))

    return results
