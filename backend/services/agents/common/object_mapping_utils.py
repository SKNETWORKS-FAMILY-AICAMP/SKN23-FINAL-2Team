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

# ── 도메인별 레이어 보너스 (공용 상수) ────────────────────────────────────────
# (과거 L4 등 특정 레이어 가산점이 있었으나, 모호성 제거를 위해 보수적으로 운영)
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
        # 고도화된 통계 기반 분류기 사용 (L4, L3 등 대응)
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
