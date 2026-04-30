"""
File    : backend/services/agents/common/multi_object_mapper.py
Author  : 송주엽
Create  : 2026-04-16
Description : 도면 내 다중 CAD 객체 간 최적 매핑을 결정하는 공통 유틸리티.
              배관·건축·전기·소방 등 모든 도메인 에이전트에서 재사용 가능합니다.

              ── 주요 함수 ─────────────────────────────────────────────────
              calculate_mapping_score()   — 가중치 기반 휴리스틱 점수 계산
              find_best_match()           — 후보 목록에서 최적 객체 선택
              llm_fallback_resolver()     — 점수가 모호할 때 sLLM 최종 판단
              ────────────────────────────────────────────────────────────

              ── 사용법 ──────────────────────────────────────────────────
              from backend.services.agents.common.multi_object_mapper import (
                  calculate_mapping_score,
                  find_best_match,
                  llm_fallback_resolver,
              )

              # 1. 점수 계산 (레이어 보너스는 도메인별로 다르게 설정)
              score = calculate_mapping_score(
                  text_entity, block_entity,
                  layer_bonus_config={"block_layer": "L4", "text_layer": "TEX", "bonus": 20.0},
              )

              # 2. 최적 매핑 자동 선택
              best, is_ambiguous, second = find_best_match(text_entity, candidates)

              # 3. 모호할 때 LLM 판단
              if is_ambiguous:
                  winner_handle = await llm_fallback_resolver(
                      text_entity, best, second, domain_hint="배관"
                  )
              ────────────────────────────────────────────────────────────

Modification History :
    - 2026-04-16 (송주엽) : 공통 다중객체 매핑 모듈 최초 작성
                            calculate_mapping_score / find_best_match /
                            llm_fallback_resolver 구현
    - 2026-04-19 (김지우) : OpenAI API 사용으로 수정
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ── 전역 상수: 함수 정의보다 먼저 선언해야 기본 파라미터 값으로 사용 가능 ────────────
_SPATIAL_FILTER_THRESHOLD_MM: float = 50_000.0
"""텍스트·블록 쌍 비교 시 이 거리 초과는 매핑 후보에서 제외.
평면도(단선계통도 포함)는 텍스트-블록이 수 미터 떨어져 있을 수 있으므로
50,000mm(50m)를 기본값으로 설정한다. score_kwargs의 max_distance_mm와
동기화하여 실수로 후보를 너무 좁게 자르지 않도록 주의한다."""

@dataclass
class LayerBonusConfig:
    """
    도메인별 레이어 보너스 설정.
    """
    block_layer: str = ""
    text_layer: str = ""
    bonus: float = 20.0

def _get_pos(entity: dict[str, Any]) -> tuple[float, float] | None:
    """엔티티의 위치 좌표를 추출합니다."""
    pos = entity.get("insert_point") or entity.get("position") or entity.get("center") or {}
    try:
        return float(pos.get("x", 0)), float(pos.get("y", 0))
    except (TypeError, ValueError):
        return None


def _bbox_overlap(a: dict, b: dict) -> bool:
    """두 엔티티의 bbox가 겹치거나 근접한지 확인합니다.
    CadBBox 직렬화 규격 (x1, y1, x2, y2) 기반.
    """
    a_bbox = a.get("bbox") or {}
    b_bbox = b.get("bbox") or {}
    if not a_bbox or not b_bbox:
        return False
    try:
        margin = 50.0
        # CadBBox JSON 필드: x1(min_x), x2(max_x), y1(min_y), y2(max_y)
        return not (
            float(a_bbox.get("x2", 0)) + margin < float(b_bbox.get("x1", 0)) or
            float(b_bbox.get("x2", 0)) + margin < float(a_bbox.get("x1", 0)) or
            float(a_bbox.get("y2", 0)) + margin < float(b_bbox.get("y1", 0)) or
            float(b_bbox.get("y2", 0)) + margin < float(a_bbox.get("y1", 0))
        )
    except (TypeError, ValueError):
        return False


def calculate_mapping_score(
    text_entity: dict[str, Any],
    block_entity: dict[str, Any],
    *,
    max_distance_mm: float = _SPATIAL_FILTER_THRESHOLD_MM,  # 기본값을 전역 상수와 일치
    distance_weight: float = 0.05,
    layer_bonus_config: LayerBonusConfig | None = None,
) -> float:
    score = 0.0

    t_pos = _get_pos(text_entity)
    b_pos = _get_pos(block_entity)

    dist = float("inf")
    if t_pos and b_pos:
        dist = math.hypot(t_pos[0] - b_pos[0], t_pos[1] - b_pos[1])

        if dist < 100:
            score += 1500.0   # 초근접 (단선도/결선도 — 확정적 매칭)
        elif dist < 300:
            score += 800.0    # 근접 (단선도 일반)
        elif dist < 1000:
            score += 400.0    # 유효 범위 (단선도 원거리)
        elif dist < max_distance_mm:
            # ── 연속 지수 감쇠 (평면도·대형 도면 대응) ──────────────────────
            # 1000mm=400점 → max_distance_mm≈0점으로 부드럽게 감소
            # 이 방식은 5000mm vs 8000mm 같은 미세한 거리 차이도 점수 차를 만들어
            # 익명 블록(A$C072071E9)이 많은 도면에서 LLM fallback을 최소화한다.
            t = (dist - 1000.0) / (max_distance_mm - 1000.0)  # 0~1 정규화
            score += 400.0 * math.exp(-3.0 * t)  # 1000mm=400, 점진 감소

    if _bbox_overlap(text_entity, block_entity):
        score += 200.0

    try:
        t_rot = float(text_entity.get("rotation", 0))
        b_rot = float(block_entity.get("rotation", 0))
        angle_diff = abs(t_rot - b_rot) % 180

        if angle_diff < 5 or angle_diff > 175:
            score += 50.0
        elif 85 < angle_diff < 95:
            score -= 30.0
    except (TypeError, ValueError):
        pass

    if layer_bonus_config:
        b_layer = str(block_entity.get("layer", ""))
        t_layer = str(text_entity.get("layer", ""))
        if (
            layer_bonus_config.block_layer and b_layer == layer_bonus_config.block_layer
            and layer_bonus_config.text_layer and t_layer == layer_bonus_config.text_layer
        ):
            score += layer_bonus_config.bonus

    return score

@dataclass
class MappingResult:
    best: dict[str, Any] | None        = None
    best_score: float                  = 0.0
    second: dict[str, Any] | None      = None
    second_score: float                = 0.0
    is_ambiguous: bool                 = False
    all_scores: list[tuple[dict, float]] = field(default_factory=list)

def find_best_match(
    text_entity: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    ambiguity_threshold: float = 10.0,
    score_kwargs: dict | None = None,
) -> MappingResult:
    if not candidates:
        return MappingResult()

    kwargs = score_kwargs or {}
    scored: list[tuple[dict, float]] = [
        (cand, calculate_mapping_score(text_entity, cand, **kwargs))
        for cand in candidates
    ]
    scored.sort(key=lambda x: x[1], reverse=True)

    best_entity, best_score = scored[0]
    result = MappingResult(
        best=best_entity,
        best_score=best_score,
        all_scores=scored,
    )

    if len(scored) >= 2:
        second_entity, second_score = scored[1]
        result.second = second_entity
        result.second_score = second_score
        result.is_ambiguous = (best_score - second_score) <= ambiguity_threshold

    return result

async def llm_fallback_resolver(
    text_entity: dict[str, Any],
    candidate_a: dict[str, Any],
    candidate_b: dict[str, Any],
    domain_hint: str = "",
) -> str:
    """
    두 후보 간 점수 차이가 작아 자동 판단이 어려울 때 OpenAI를 통해 최종 결정을 위임합니다.
    """
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_openai import ChatOpenAI

    from backend.core.config import settings

    domain_str = f" ({domain_hint} 도메인)" if domain_hint else ""
    handle_a = str(candidate_a.get("handle", "A"))
    handle_b = str(candidate_b.get("handle", "B"))

    system_prompt = (
        f"너는 CAD 도면 분석{domain_str} 전문가다.\n"
        "주어진 텍스트 엔티티가 어느 블록 엔티티와 연결되는지 판단한다.\n"
        "레이어명·색상 표준은 사용자/프로젝트마다 다를 수 있으므로, 색상이나 레이어명 하나만으로 "
        "결정하지 말고 위치, bbox, 방향, 텍스트 의미, 블록명/속성을 함께 판단한다.\n"
        "반드시 두 handle 중 하나만 출력하라. 설명 없이 handle 값만 출력하라."
    )

    user_prompt = (
        f"[텍스트 엔티티]\n{json.dumps(text_entity, ensure_ascii=False, indent=2)}\n\n"
        f"[후보 A — handle: {handle_a}]\n{json.dumps(candidate_a, ensure_ascii=False, indent=2)}\n\n"
        f"[후보 B — handle: {handle_b}]\n{json.dumps(candidate_b, ensure_ascii=False, indent=2)}\n\n"
        f"이 텍스트와 올바르게 연결되는 엔티티의 handle 을 하나만 출력하라.\n"
        f"선택지: {handle_a} 또는 {handle_b}"
    )

    try:
        # OpenAI 환경으로 수정
        llm = ChatOpenAI(
            model=settings.OPENAI_MODEL_NAME,
            temperature=0,
            api_key=settings.OPENAI_API_KEY,
        )
        response = await llm.ainvoke(
            [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
        )
        answer = response.content.strip()

        if handle_b in answer and handle_a not in answer:
            return handle_b
        return handle_a

    except Exception as exc:  # noqa: BLE001
        logger.error("[llm_fallback_resolver] LLM 호출 실패, 후보 A 반환: %s", exc)
        return handle_a


def _preparse_positions(
    entities: list[dict[str, Any]],
) -> list[tuple[float, float] | None]:
    """
    엔티티 리스트의 좌표를 (x, y) 튜플로 사전 파싱합니다.
    루프 내 반복 파싱을 피해 매핑 성능을 개선합니다.
    좌표가 없으면 None을 반환합니다.
    """
    result: list[tuple[float, float] | None] = []
    for ent in entities:
        pos = ent.get("insert_point") or ent.get("position") or ent.get("center") or {}
        try:
            result.append((float(pos.get("x", 0)), float(pos.get("y", 0))))
        except (TypeError, ValueError, AttributeError):
            result.append(None)
    return result


async def auto_map_entities(
    text_entities: list[dict[str, Any]],
    block_entities: list[dict[str, Any]],
    *,
    ambiguity_threshold: float = 10.0,
    domain_hint: str = "",
    score_kwargs: dict | None = None,
    llm_concurrency: int = 8,
    spatial_threshold_mm: float = _SPATIAL_FILTER_THRESHOLD_MM,
) -> list[dict[str, Any]]:
    """
    텍스트-블록 간 최적 매핑을 계산합니다.

    매핑 전략 (Greedy Assignment):
    - 1단계: 모든 텍스트의 후보 쌍과 점수를 사전 계산합니다.
    - 2단계: 전체 후보 쌍을 점수 내림차순으로 정렬합니다.
    - 3단계: 점수가 높은 쌍부터 순서대로 "이미 매핑된 텍스트"는 건너뜁니다.
             → 하나의 텍스트는 반드시 하나의 블록에만 매핑됩니다.

    최적화:
    - 좌표 사전 파싱(pre-parse): 루프 진입 전 모든 엔티티 좌표를 float 튜플로 변환
    - 공간 필터링(spatial filter): spatial_threshold_mm 초과 블록은 후보 제외
      → O(N×M) 전수 비교에서 O(N×k) (k ≪ M)로 복잡도 감소
    """
    import asyncio

    # ── 좌표 사전 파싱 ───────────────────────────────────────────────────────
    text_positions  = _preparse_positions(text_entities)
    block_positions = _preparse_positions(block_entities)

    # ── 공간 필터 임계값: score_kwargs의 max_distance_mm와 동기화 ──────────────
    _max_dist = float((score_kwargs or {}).get("max_distance_mm", _SPATIAL_FILTER_THRESHOLD_MM))
    _effective_spatial = max(spatial_threshold_mm, _max_dist)  # 더 넓은 쪽 사용

    # ── Adaptive Ambiguity Threshold ──────────────────────────────────────────
    # 단선도 근거리(score>=400): 점수 차 100 이상이어야 auto 판정 (기본)
    # 평면도 원거리(score<400) : 연속 감쇠로 실제 차이가 생기므로 20점으로도 충분
    # → 익명 블록 도면에서 LLM fallback 비율을 대폭 낮춤
    _HIGH_CONF_THRESHOLD = max(ambiguity_threshold, 100.0)  # 근거리용 엄격 기준
    _LOW_CONF_THRESHOLD  = 20.0                              # 원거리용 완화 기준

    # ── 1단계: 모든 후보 쌍의 점수 사전 계산 ─────────────────────────────────
    all_candidate_pairs: list[dict[str, Any]] = []

    for t_idx, text_ent in enumerate(text_entities):
        t_pos = text_positions[t_idx]

        # ── 공간 필터링: _effective_spatial 이내 블록만 후보로 구성 ────────────
        if t_pos is not None:
            tx, ty = t_pos
            candidates = [
                block_entities[b_idx]
                for b_idx, b_pos in enumerate(block_positions)
                if b_pos is None or math.hypot(tx - b_pos[0], ty - b_pos[1]) <= _effective_spatial
            ]
        else:
            candidates = block_entities

        if not candidates:
            continue

        m_res = find_best_match(
            text_ent,
            candidates,
            # 근거리(고신뢰) vs 원거리(저신뢰) adaptive threshold
            ambiguity_threshold=_HIGH_CONF_THRESHOLD,
            score_kwargs=score_kwargs,
        )

        if m_res.best is None:
            continue

        # ── Adaptive: 원거리 저신뢰 쌍은 더 낮은 threshold로 재판정 ──────────
        # 둘 다 400점 미만(원거리)이면, 20점 이상 차이면 auto로 확정
        if m_res.is_ambiguous and m_res.best_score < 400.0:
            tight_gap = (m_res.best_score - m_res.second_score) if m_res.second else m_res.best_score
            if tight_gap > _LOW_CONF_THRESHOLD:
                m_res.is_ambiguous = False  # 원거리에서는 20점 차이면 충분

        all_candidate_pairs.append({
            "text_ent":     text_ent,
            "best":         m_res.best,
            "second":       m_res.second,
            "score":        m_res.best_score,
            "is_ambiguous": m_res.is_ambiguous,
        })

    # ── 2단계: 점수 내림차순 정렬 (확실한 짝부터 선점) ──────────────────────
    all_candidate_pairs.sort(key=lambda x: x["score"], reverse=True)

    # ── 3단계: Greedy Assignment — 중복 텍스트 방지 ──────────────────────────
    auto_results:      list[dict[str, Any]]          = []
    ambiguous_tasks:   list[tuple[dict, dict, dict]] = []
    used_text_handles: set[str]                      = set()

    for pair in all_candidate_pairs:
        t_h = str(pair["text_ent"].get("handle", ""))
        b_h = str(pair["best"].get("handle", ""))

        if t_h in used_text_handles:
            continue  # 이미 더 높은 점수로 매핑된 텍스트는 건너뜀

        if pair["is_ambiguous"] and pair["second"] is not None:
            ambiguous_tasks.append((pair["text_ent"], pair["best"], pair["second"]))
        else:
            auto_results.append({
                "text_handle":  t_h,
                "block_handle": b_h,
                "score":        pair["score"],
                "method":       "auto",
            })

        used_text_handles.add(t_h)

    # ── LLM Fallback: 모호한 쌍만 AI에게 위임 ───────────────────────────────
    llm_results: list[dict[str, Any]] = []
    if ambiguous_tasks:
        sem = asyncio.Semaphore(llm_concurrency)

        async def _resolve(t: dict, a: dict, b: dict) -> dict | None:
            async with sem:
                try:
                    winner = await llm_fallback_resolver(t, a, b, domain_hint=domain_hint)
                    score  = find_best_match(t, [a, b], score_kwargs=score_kwargs).best_score
                    return {
                        "text_handle":  str(t.get("handle", "")),
                        "block_handle": winner,
                        "score":        score,
                        "method":       "llm_fallback",
                    }
                except Exception as exc:
                    logger.error("[auto_map_entities] LLM fallback 실패: %s", exc)
                    return None

        resolved = await asyncio.gather(
            *(_resolve(t, a, b) for t, a, b in ambiguous_tasks)
        )
        llm_results = [r for r in resolved if r is not None]

    logger.debug(
        "[auto_map_entities] 완료: text=%d blocks=%d → 자동=%d LLM=%d (공간필터 threshold=%.0fmm)",
        len(text_entities), len(block_entities),
        len(auto_results), len(llm_results), spatial_threshold_mm,
    )
    return auto_results + llm_results
