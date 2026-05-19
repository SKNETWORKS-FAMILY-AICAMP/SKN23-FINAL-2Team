"""
File    : backend/services/agents/elec/sub/candidate_generator.py
Description : ElecSignalItem 목록을 전기 검토 후보로 변환합니다.

후보는 LLM에게 "어디를 봐야 하는지" 알려주는 중간 산출물입니다.
법규 수치가 필요한 항목은 soft 후보로 남기고, topology가 이미 확정한
단선 같은 항목만 hard 후보로 표시합니다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any

from backend.services.agents.elec.sub.signal_extractor import ElecSignalItem

log = logging.getLogger(__name__)


@dataclass
class ElecCandidateItem:
    candidate_id: str
    candidate_type: str
    elec_category: str
    evidence: ElecSignalItem
    rag_queries: list[str] = field(default_factory=list)
    numeric_violation: bool = False
    candidate_strength: str = "soft"  # hard | soft
    severity_hint: str = "UNKNOWN"
    enabled: bool = True
    reference_topic: str = ""


_SIGNAL_TO_CANDIDATE: dict[str, tuple[str, str, str]] = {
    "open_circuit": ("continuity", "hard", "CRITICAL"),
    "grounding_plan": ("grounding", "soft", "INFO"),
    "grounding_attribute": ("grounding", "soft", "INFO"),
    "grounding_evidence": ("grounding", "soft", "INFO"),
    "high_voltage_plan": ("high_voltage", "soft", "WARNING"),
    "high_voltage_evidence": ("high_voltage", "soft", "WARNING"),
    "conduit_evidence": ("conduit", "soft", "INFO"),
    "conduit_clearance_observed": ("conduit_clearance", "soft", "WARNING"),
    "panel_evidence": ("panel", "soft", "INFO"),
    "panel_wall_clearance_observed": ("panel_clearance", "soft", "WARNING"),
    "cable_evidence": ("cable", "soft", "INFO"),
    "cable_size_observed": ("cable_ampacity", "soft", "INFO"),
    "breaker_cable_pair": ("breaker_coordination", "soft", "WARNING"),
}


class ElecCandidateGenerator:
    """전기 신호를 RAG 및 LLM 검토 후보로 변환합니다."""

    def generate(self, signals: list[ElecSignalItem]) -> list[ElecCandidateItem]:
        candidates: list[ElecCandidateItem] = []
        for signal in signals or []:
            candidate = self._process_signal(signal)
            if candidate is not None:
                candidates.append(candidate)
        deduped = self._dedupe(candidates)
        log.debug("[ElecCandidateGenerator] candidates=%d deduped=%d", len(candidates), len(deduped))
        return deduped

    def _process_signal(self, signal: ElecSignalItem) -> ElecCandidateItem | None:
        mapping = _SIGNAL_TO_CANDIDATE.get(signal.signal_type)
        if mapping is None:
            return None
        candidate_type, strength, severity = mapping
        is_hard = strength == "hard"
        return ElecCandidateItem(
            candidate_id=f"{candidate_type}-{signal.equipment_id}",
            candidate_type=candidate_type,
            elec_category=signal.elec_category,
            evidence=signal,
            numeric_violation=is_hard,
            candidate_strength=strength,
            severity_hint=severity,
            reference_topic=self._topic(candidate_type, signal),
        )

    @staticmethod
    def _topic(candidate_type: str, signal: ElecSignalItem) -> str:
        return {
            "continuity": "전선 접속 및 회로 연속성",
            "grounding": "접지선, 접지저항, 접지극, 피뢰 접지",
            "high_voltage": "고압 수변전설비 이격 및 보호",
            "conduit": "전선관 및 트레이 설치",
            "conduit_clearance": "전선관/트레이 간 이격",
            "panel": "분전반 및 배전반",
            "panel_clearance": "분전반 전면 유지관리 공간",
            "cable": "전선 규격",
            "cable_ampacity": "전선 허용전류와 전압강하",
            "breaker_coordination": "차단기 정격과 전선 굵기 협조",
        }.get(candidate_type, signal.elec_category)

    @staticmethod
    def _dedupe(candidates: list[ElecCandidateItem]) -> list[ElecCandidateItem]:
        out: list[ElecCandidateItem] = []
        seen: set[tuple[str, str]] = set()
        for candidate in candidates:
            key = (candidate.candidate_type, candidate.elec_category)
            # evidence별 hard 후보는 보존하고, soft 후보는 유형 단위로 압축한다.
            if candidate.numeric_violation:
                key = (candidate.candidate_type, candidate.evidence.equipment_id)
            if key in seen:
                continue
            seen.add(key)
            out.append(candidate)
        return out


def candidate_to_dict(candidate: ElecCandidateItem) -> dict[str, Any]:
    signal = candidate.evidence
    return {
        "candidate_id": candidate.candidate_id,
        "candidate_type": candidate.candidate_type,
        "elec_category": candidate.elec_category,
        "equipment_id": signal.equipment_id,
        "signal_type": signal.signal_type,
        "observed_value": signal.observed_value,
        "numeric_violation": candidate.numeric_violation,
        "candidate_strength": candidate.candidate_strength,
        "severity_hint": candidate.severity_hint,
        "reference_topic": candidate.reference_topic,
        "context": signal.context,
    }
