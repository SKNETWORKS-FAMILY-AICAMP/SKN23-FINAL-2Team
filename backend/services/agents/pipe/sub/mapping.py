"""
File    : backend/services/agents/pipe/sub/mapping.py
Author  : 송주엽
Create  : 2026-04-09
Description : 배관(pipe) 도메인 CAD 매핑 에이전트.
              공통 BaseMappingAgent(common/mapping.py)를 상속하며
              DOMAIN="pipe" 로 DB를 조회한다.
              layer_role 지원 헬퍼(get_layer_role_map, compute_unmapped_layer_names)는
              arch_pipe_layer_split.py, agent_api.py, cad_interop.py에서 사용한다.

Modification History :
    - 2026-04-09 (송주엽) : 초기 작성
    - 2026-04-24 (송주엽) : common/mapping.py 기반으로 리팩토링 (479줄 → 박피)
    - 2026-04-28 (송주엽) : LayerBasedScoringEngine 추가 — 도면층별 다중 요소 가중치 스코어링
"""

from typing import Any

from backend.services.agents.common.mapping import (
    BaseMappingAgent,
    _RuleEntry,               # re-export — 외부 참조 호환
    _is_ignored_layer,        # re-export — arch_pipe_layer_split:23, pipe_review_node:920
    _is_valid_uuid_str,
    _fetch_rules_cached,
    invalidate_mapping_cache,  # re-export — cad_interop:43
    get_mapping_cache_stats,
)

__all__ = [
    "MappingAgent",
    "_is_ignored_layer",
    "_RuleEntry",
    "get_layer_role_map",
    "compute_unmapped_layer_names",
    "invalidate_mapping_cache",
    "get_mapping_cache_stats",
    "LayerBasedScoringEngine",
]


class MappingAgent(BaseMappingAgent):
    """
    배관 도메인 매핑 에이전트 (DOMAIN="pipe").
    layer_role(arch/mep/aux) 컬럼을 DB에서 함께 로드한다.
    """
    DOMAIN = "pipe"


# ─── 배관 전용 헬퍼 ───────────────────────────────────────────────────────────

def get_layer_role_map(org_id: str | None) -> dict[str, str]:
    """
    DB에 명시된 레이어명 → 역할("arch"/"mep"/"aux") 맵을 반환한다.
    arch_pipe_layer_split.py, agent_api.py가 휴리스틱 분류보다 우선하여 사용한다.
    캐시된 _fetch_rules_cached 결과를 재사용하므로 추가 DB 쿼리 없음.
    """
    oid = (org_id or "").strip()
    if not (oid and _is_valid_uuid_str(oid)):
        return {}
    rules = _fetch_rules_cached("pipe", oid)
    role_map: dict[str, str] = {}
    for rule in rules:
        role = (rule.layer_role or "").strip().lower()
        if role in {"arch", "mep", "aux"} and rule.source_key:
            role_map[rule.source_key] = role
    return role_map


def compute_unmapped_layer_names(
    drawing_data: dict[str, Any],
    org_id: str | None,
) -> list[str]:
    """정규화된 도면 JSON에서 미매핑 레이어·블록명 목록을 반환한다. (순서 유지, 중복 제거)"""
    oid = (org_id or "").strip()
    mapper = MappingAgent(org_id=oid if _is_valid_uuid_str(oid) else None)
    mr = mapper.execute(drawing_data)
    raw = mr.get("unmapped") or []
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for x in raw:
        s = str(x).strip() if x is not None else ""
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


# ─── 도면층 기반 다중 요소 가중치 스코어링 ──────────────────────────────────

def _estimate_entity_size(entity: dict) -> float:
    """엔티티 bbox 기반 크기(넓이) 추정. bbox 없으면 0."""
    b = entity.get("bbox")
    if not isinstance(b, dict):
        return 0.0
    try:
        if "x1" in b:
            return abs(float(b["x2"]) - float(b["x1"])) * abs(float(b["y2"]) - float(b["y1"]))
        if "min_x" in b:
            return (
                abs(float(b["max_x"]) - float(b["min_x"]))
                * abs(float(b["max_y"]) - float(b["min_y"]))
            )
    except (TypeError, ValueError, KeyError):
        pass
    return 0.0


class LayerBasedScoringEngine:
    """
    도면층(layer) 기반 다중 요소 가중치 스코어링 엔진.

    - compute_intra_layer_mapping_score(): 같은 레이어 내 text-block 매핑 점수 보정
    - compute_inter_layer_constraints()  : 레이어 간 텍스트→블록 방향성 분석
    """

    # 기본 보너스 설정
    DEFAULT_LAYER_BONUS        = 20.0
    DOMINANT_TYPE_MATCH_BONUS  = 20.0
    COLOR_EXACT_MATCH_BONUS    = 15.0
    COLOR_LAYER_MATCH_BONUS    = 8.0
    SIZE_SIMILAR_BONUS         = 15.0
    SIZE_ACCEPTABLE_BONUS      = 5.0
    HIGH_CONCENTRATION_BONUS   = 10.0

    def compute_intra_layer_mapping_score(
        self,
        text_entity: dict,
        block_entity: dict,
        layer_info: dict,
        *,
        base_score: float = 0.0,
        layer_bonus: float = DEFAULT_LAYER_BONUS,
    ) -> dict[str, Any]:
        """
        도면층 내 text-block 매핑 점수 보정.

        추가 가중치:
        1. 같은 도면층 보너스        : +layer_bonus (기본 20점)
        2. 주도 엔티티 타입 일치     : +20점
        3. 색상 완전 일치           : +15점 / 레이어 주도 색상 일치: +8점
        4. 크기 분포 유사도         : +15점(30% 이내) / +5점(70% 이내)
        5. 레이어 블록 집중도       : +10점 (block_entity_ratio > 0.7)

        Returns:
            {score, reason, base_score, layer_bonus}
        """
        score = base_score + layer_bonus
        reasons: list[str] = [f"same_layer:{layer_bonus:.0f}"]

        # [L-layer 내 MEP 후보 감지]
        text_mep_score = text_entity.get("entity_mep_score", 0.0)
        block_mep_score = block_entity.get("entity_mep_score", 0.0)
        if text_mep_score >= 0.6 or block_mep_score >= 0.6:
            l_layer_bonus = 30.0
            score += l_layer_bonus
            reasons.append(f"l_layer_mep_candidate:text={text_mep_score:.2f},block={block_mep_score:.2f}:{l_layer_bonus:.0f}")

        characteristics = layer_info.get("characteristics") or {}

        # [1] 주도 엔티티 타입 일치
        text_type  = str(text_entity.get("raw_type") or text_entity.get("type") or "").upper()
        block_type = str(block_entity.get("raw_type") or block_entity.get("type") or "").upper()
        dominant   = str(characteristics.get("dominant_type") or "").upper()

        if dominant and (text_type == dominant or block_type == dominant):
            score += self.DOMINANT_TYPE_MATCH_BONUS
            reasons.append(f"dominant_type_match:{self.DOMINANT_TYPE_MATCH_BONUS:.0f}")

        # [2] 색상 일치
        text_color   = text_entity.get("color")
        block_color  = block_entity.get("color")
        layer_color  = characteristics.get("dominant_color")

        if text_color is not None and block_color is not None and text_color == block_color:
            score += self.COLOR_EXACT_MATCH_BONUS
            reasons.append(f"color_exact:{self.COLOR_EXACT_MATCH_BONUS:.0f}")
        elif layer_color is not None and (
            text_color == layer_color or block_color == layer_color
        ):
            score += self.COLOR_LAYER_MATCH_BONUS
            reasons.append(f"color_layer:{self.COLOR_LAYER_MATCH_BONUS:.0f}")

        # [3] 크기 유사도 (bbox 기반)
        text_size  = _estimate_entity_size(text_entity)
        block_size = _estimate_entity_size(block_entity)
        avg_size   = float(characteristics.get("avg_entity_size") or 0.0)

        if avg_size > 0:
            deviation = max(
                abs(text_size - avg_size),
                abs(block_size - avg_size),
            ) / avg_size

            if deviation < 0.30:
                score += self.SIZE_SIMILAR_BONUS
                reasons.append(f"size_similar:{self.SIZE_SIMILAR_BONUS:.0f}")
            elif deviation < 0.70:
                score += self.SIZE_ACCEPTABLE_BONUS
                reasons.append(f"size_acceptable:{self.SIZE_ACCEPTABLE_BONUS:.0f}")

        # [4] 레이어 블록 집중도
        block_ratio = float(characteristics.get("block_entity_ratio") or 0.0)
        if block_ratio > 0.7:
            score += self.HIGH_CONCENTRATION_BONUS
            reasons.append(f"high_block_concentration:{self.HIGH_CONCENTRATION_BONUS:.0f}")

        return {
            "score":       score,
            "reason":      " | ".join(reasons),
            "base_score":  base_score,
            "layer_bonus": layer_bonus,
        }

    def compute_inter_layer_constraints(
        self,
        layers_structured: list[dict],
        mapped_results: list[dict],
        *,
        min_probability: float = 0.3,
        min_score: float = 1000.0,
    ) -> list[dict[str, Any]]:
        """
        도면층 간 text→block 방향성 분석.

        text_entity_ratio > 0.5 인 레이어(텍스트 레이어)에서
        block_entity_ratio > 0.5 인 레이어(블록 레이어)로의 매핑 성공률을 집계한다.

        Returns:
            [{"from_layer", "to_layer", "relationship", "mapping_probability", "count"}, ...]
        """
        text_layers: set[str] = {
            l["name"] for l in layers_structured
            if (l.get("characteristics") or {}).get("text_entity_ratio", 0.0) > 0.5
        }
        block_layers: set[str] = {
            l["name"] for l in layers_structured
            if (l.get("characteristics") or {}).get("block_entity_ratio", 0.0) > 0.5
        }

        constraints: list[dict[str, Any]] = []

        for text_layer in text_layers:
            # 이 텍스트 레이어에서 시작된 매핑 전체
            from_mappings = [
                m for m in mapped_results
                if (m.get("text_entity") or {}).get("layer") == text_layer
            ]
            total = len(from_mappings)
            if total == 0:
                continue

            for block_layer in block_layers:
                if text_layer == block_layer:
                    continue

                matched = sum(
                    1 for m in from_mappings
                    if (m.get("block_entity") or {}).get("layer") == block_layer
                    and (m.get("mapping_score") or m.get("layer_based_score") or 0.0) >= min_score
                )
                probability = matched / total

                if probability >= min_probability:
                    constraints.append({
                        "from_layer":          text_layer,
                        "to_layer":            block_layer,
                        "relationship":        "likely_labels",
                        "mapping_probability": round(probability, 4),
                        "count":               matched,
                    })

        # 확률 내림차순 정렬
        constraints.sort(key=lambda x: x["mapping_probability"], reverse=True)
        return constraints
