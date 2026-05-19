"""
File    : backend/services/agents/fire/sub/candidate_generator.py
Description : SignalItem[] → CandidateItem[] 변환기.

spacing_observed (primary): nearest_distances 기반 raw 측정값 신호.
    RuleSlot이 없거나 위반이 아니면 candidate 생성 안 함 (정책 B).
    RuleSlot 위반 확정이면 hard candidate 생성.

spacing_exceeded (legacy fallback): violation_candidates 기반.
    RuleSlot 위반 확정이면 hard, RuleSlot 없으면 soft (하위 호환).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from backend.services.agents.fire.sub.signal_extractor import SignalItem
from backend.services.agents.fire.rule_slots.rule_slot import (
    RuleSlot, find_slot, is_violated,
)

log = logging.getLogger(__name__)


@dataclass
class CandidateItem:
    candidate_id:       str
    candidate_type:     str       # spacing | height | missing | attribute | coverage
    equipment_category: str
    evidence:           SignalItem
    rag_queries:        list[str] = field(default_factory=list)
    numeric_violation:  bool = False
    candidate_strength: str = "soft"  # hard | soft
    severity_hint:      str = "UNKNOWN"
    enabled:            bool = True
    # 적용된 RuleSlot 정보 (hard candidate 시 채워짐)
    rule_id:             str        = ""
    rule_topic:          str        = ""
    applied_threshold:   float|None = None
    applied_unit:        str        = "mm"
    applied_comparator:  str        = ""
    applied_source_type: str        = ""
    applied_priority:    int        = 0


class CandidateGenerator:
    """
    SignalItem 목록을 CandidateItem 목록으로 변환한다.

    spacing_observed (primary): nearest_distances 기반 raw 측정값 신호.
        RuleSlot이 없거나 위반이 아니면 candidate 생성 안 함 (정책 B).
        RuleSlot 위반 확정이면 hard candidate 생성.

    spacing_exceeded (legacy fallback): violation_candidates 기반.
        RuleSlot 위반 확정이면 hard, RuleSlot 없으면 soft (하위 호환).

    '정상' 판정(위반 없음)은 후보에 포함하지 않는다.
    """

    def __init__(self, rule_slots: list[RuleSlot]):
        self._rule_slots = rule_slots

    def generate(self, signals: list[SignalItem]) -> list[CandidateItem]:
        candidates: list[CandidateItem] = []
        for signal in signals:
            candidate = self._process_signal(signal)
            if candidate is not None:
                candidates.append(candidate)
        log.debug("[CandidateGenerator] 생성된 후보 %d건", len(candidates))
        return candidates

    def _process_signal(self, signal: SignalItem) -> CandidateItem | None:
        if signal.signal_type == "spacing_observed":
            return self._spacing_observed_candidate(signal)
        if signal.signal_type == "spacing_exceeded":
            return self._spacing_exceeded_candidate(signal)
        if signal.signal_type == "coverage_gap":
            return self._coverage_gap_candidate(signal)
        log.debug("[CandidateGenerator] 미처리 signal_type=%s (Phase 2+)", signal.signal_type)
        return None

    def _build_hard_spacing_candidate(
        self, signal: SignalItem, slot: RuleSlot
    ) -> CandidateItem:
        return CandidateItem(
            candidate_id=f"spacing-{signal.fire_category}-{signal.equipment_id}",
            candidate_type="spacing",
            equipment_category=signal.fire_category,
            evidence=signal,
            numeric_violation=True,
            candidate_strength="hard",
            severity_hint="CRITICAL",
            rule_id=slot.rule_id,
            rule_topic=slot.rule_topic,
            applied_threshold=slot.threshold,
            applied_unit=slot.unit,
            applied_comparator=slot.comparator,
            applied_source_type=slot.source_type,
            applied_priority=slot.priority,
        )

    def _spacing_observed_candidate(self, signal: SignalItem) -> CandidateItem | None:
        """nearest_distances 기반 spacing_observed 처리. RuleSlot 없거나 정상이면 None."""
        try:
            observed = float(signal.observed_value) if signal.observed_value is not None else 0.0
        except (ValueError, TypeError):
            log.warning("[CandidateGenerator] observed_value 변환 실패, 신호 건너뜀: %s", signal.observed_value)
            return None
        if observed <= 0:
            return None

        slot = find_slot(self._rule_slots, signal.fire_category, "spacing")
        if slot is None:
            return None
        if not is_violated(slot, observed):
            return None

        return self._build_hard_spacing_candidate(signal, slot)

    def _spacing_exceeded_candidate(self, signal: SignalItem) -> CandidateItem | None:
        """violation_candidates 기반 spacing_exceeded 처리 (legacy fallback)."""
        try:
            observed = float(signal.observed_value) if signal.observed_value is not None else 0.0
        except (ValueError, TypeError):
            log.warning("[CandidateGenerator] observed_value 변환 실패, 신호 건너뜀: %s", signal.observed_value)
            return None

        if observed <= 0:
            return None

        slot = find_slot(self._rule_slots, signal.fire_category, "spacing")

        if slot is not None:
            if not is_violated(slot, observed):
                return None
            return self._build_hard_spacing_candidate(signal, slot)
        else:
            return CandidateItem(
                candidate_id=f"spacing-{signal.fire_category}-{signal.equipment_id}",
                candidate_type="spacing",
                equipment_category=signal.fire_category,
                evidence=signal,
                numeric_violation=False,
                candidate_strength="soft",
                severity_hint="WARNING",
            )

    def _coverage_gap_candidate(self, signal: SignalItem) -> CandidateItem | None:
        """coverage_gap 신호 → coverage type CandidateItem (hard, numeric_violation=True)."""
        try:
            observed = float(signal.observed_value) if signal.observed_value is not None else 0.0
        except (ValueError, TypeError):
            return None
        if observed <= 0:
            return None
        threshold = float(signal.threshold) if signal.threshold is not None else 20_000.0
        return CandidateItem(
            candidate_id=f"coverage-extinguisher-{signal.equipment_id}",
            candidate_type="coverage",
            equipment_category="extinguisher",
            evidence=signal,
            numeric_violation=True,
            candidate_strength="hard",
            severity_hint="CRITICAL",
            rule_id="NFSC-extinguisher-walking",
            rule_topic="소화기 커버리지",
            applied_threshold=threshold,
            applied_unit="mm",
            applied_comparator="lte",
            applied_source_type="nfsc_default",
            applied_priority=3,
        )
