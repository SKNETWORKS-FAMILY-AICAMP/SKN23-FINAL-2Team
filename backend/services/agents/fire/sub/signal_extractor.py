"""
File    : backend/services/agents/fire/sub/signal_extractor.py
Description : fire_topology 데이터에서 SignalItem 추출.

Primary  경로: fire_topology.<cat>.nearest_distances → spacing_observed 신호
Fallback 경로: fire_topology.<cat>.violation_candidates → spacing_exceeded 신호 (하위 호환)

nearest_distances가 비어 있으면 violation_candidates 폴백을 사용한다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

_SPACING_CATEGORIES: tuple[str, ...] = (
    "sprinkler", "detector", "hydrant"
)


@dataclass
class SignalItem:
    equipment_id:   str
    fire_category:  str                 # sprinkler | detector | hydrant | extinguisher
    signal_type:    str                 # spacing_observed | spacing_exceeded | ...
    observed_value: float | str | None  # 실측값 (mm 단위가 기본)
    threshold:      float | None        # spacing_observed는 None, spacing_exceeded는 파서 기준값
    context:        dict = field(default_factory=dict)


class SignalExtractor:
    """
    파서 출력(parsed dict)에서 SignalItem 목록을 추출한다.

    nearest_distances(primary) → spacing_observed:
        raw 측정값 신호. threshold=None. CandidateGenerator가 RuleSlot으로 위반 판단.

    violation_candidates(fallback) → spacing_exceeded:
        parser가 기준값으로 필터링한 신호. nearest_distances가 비어 있을 때만 사용.
    """

    def extract(self, parsed: dict) -> list[SignalItem]:
        if not isinstance(parsed, dict):
            return []
        fire_topology = parsed.get("fire_topology") or {}
        signals: list[SignalItem] = []

        for cat in _SPACING_CATEGORIES:
            topo = fire_topology.get(cat) or {}
            nearest = topo.get("nearest_distances") or []
            if nearest:
                for item in nearest:
                    signal = self._from_nearest_distance(item, cat)
                    if signal is not None:
                        signals.append(signal)
            else:
                for candidate in topo.get("violation_candidates") or []:
                    signal = self._from_spacing_candidate(candidate, cat)
                    if signal is not None:
                        signals.append(signal)

        # extinguisher coverage_gap signals
        ext_topo = fire_topology.get("extinguisher") or {}
        for gap in ext_topo.get("coverage_gaps") or []:
            signal = self._from_coverage_gap(gap)
            if signal is not None:
                signals.append(signal)

        log.debug("[SignalExtractor] 추출된 신호 %d건", len(signals))
        return signals

    @staticmethod
    def _from_nearest_distance(item: dict, fire_category: str) -> SignalItem | None:
        """nearest_distances 항목 → spacing_observed 신호."""
        head_id = str(item.get("head") or "").strip()
        if not head_id:
            return None
        try:
            distance_mm = float(item.get("distance_mm") or 0)
        except (TypeError, ValueError):
            return None
        if distance_mm <= 0:
            return None
        return SignalItem(
            equipment_id=head_id,
            fire_category=fire_category,
            signal_type="spacing_observed",
            observed_value=distance_mm,
            threshold=None,
            context={"nearest_head": str(item.get("nearest_head") or "")},
        )

    @staticmethod
    def _from_spacing_candidate(candidate: dict, fire_category: str) -> SignalItem | None:
        """violation_candidates 항목 → spacing_exceeded 신호 (fallback)."""
        head_id = str(candidate.get("head") or "").strip()
        if not head_id:
            return None
        try:
            distance_mm = float(candidate.get("distance_mm") or 0)
            limit_mm    = float(candidate.get("limit_mm") or 0)
        except (TypeError, ValueError):
            return None
        if distance_mm <= 0 or limit_mm <= 0:
            log.debug(
                "[SignalExtractor] distance_mm=%s or limit_mm=%s가 0 이하 — 신호 생성 건너뜀",
                distance_mm, limit_mm,
            )
            return None
        return SignalItem(
            equipment_id=head_id,
            fire_category=fire_category,
            signal_type="spacing_exceeded",
            observed_value=distance_mm,
            threshold=limit_mm,
            context={
                "nearest_head": str(candidate.get("nearest_head") or ""),
                "candidate":    candidate.copy(),
            },
        )

    @staticmethod
    def _from_coverage_gap(gap: dict) -> "SignalItem | None":
        """coverage_gaps 항목 → coverage_gap 신호."""
        sample_id = str(gap.get("sample_id") or "").strip()
        if not sample_id:
            return None
        try:
            dist_mm = float(gap.get("nearest_distance_mm") or 0)
        except (TypeError, ValueError):
            return None
        if dist_mm <= 0:
            return None
        return SignalItem(
            equipment_id=sample_id,
            fire_category="extinguisher",
            signal_type="coverage_gap",
            observed_value=dist_mm,
            threshold=20_000.0,
            context={
                "x":                       gap.get("x"),
                "y":                       gap.get("y"),
                "nearest_extinguisher_id": gap.get("nearest_extinguisher_id", ""),
            },
        )
