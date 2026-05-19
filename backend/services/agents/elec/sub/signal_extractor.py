"""
File    : backend/services/agents/elec/sub/signal_extractor.py
Description : ELEC parsed/topology/geometry 결과에서 전기 검토 신호를 추출합니다.

LLM이 도면 전체를 막연히 해석하지 않도록, 결정론적 엔진이 계산한
topology/geometry/속성 결과를 작은 SignalItem 목록으로 정리합니다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

_GROUND_RE = re.compile(
    r"GND|GROUND|GRD|EARTH|접지|접지봉|접지선|접지도체|접지저항|외함\s*접지|"
    r"피뢰|L\.?\s*A|E1|E2|1종|2종|3종|특별\s*3종|FGV",
    re.IGNORECASE,
)
_HV_RE = re.compile(r"22\.?9\s*KV|22\.?9KV|HIGH\s*VOLT|HV|고압|수변전|CUBICLE|TR\b", re.IGNORECASE)
_CONDUIT_RE = re.compile(r"CONDUIT|RACEWAY|전선관|관로|덕트|DUCT|TRAY|트레이", re.IGNORECASE)
_PANEL_RE = re.compile(r"PANEL|PNL|MCC|MCCB|MCB|ELB|ACB|분전|배전|차단기", re.IGNORECASE)
_CABLE_RE = re.compile(r"\b(?:CV|HFIX|HIV|IV|FGV|SQ|MM2|AWG)\b|㎟|mm²", re.IGNORECASE)


@dataclass
class ElecSignalItem:
    equipment_id: str
    signal_type: str
    elec_category: str
    observed_value: float | str | None = None
    threshold: float | None = None
    context: dict[str, Any] = field(default_factory=dict)


def _handle(row: dict) -> str:
    return str(row.get("handle") or row.get("id") or row.get("object_id") or row.get("equipment_id") or "").strip()


def _text_blob(row: dict) -> str:
    attrs = row.get("attributes") or {}
    pieces = [
        row.get("layer"),
        row.get("type"),
        row.get("raw_type"),
        row.get("name"),
        row.get("text"),
        row.get("content"),
        row.get("block_name"),
        row.get("effective_name"),
        row.get("category"),
        row.get("elec_category"),
    ]
    if isinstance(attrs, dict):
        pieces.extend(attrs.values())
    return " ".join(str(p or "") for p in pieces)


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"[-+]?\d+(?:\.\d+)?", str(value))
    return float(m.group(0)) if m else None


class ElecSignalExtractor:
    """전기 parsed dict에서 RAG/검토 후보 생성을 위한 신호를 추출합니다."""

    def extract(self, parsed: dict) -> list[ElecSignalItem]:
        if not isinstance(parsed, dict):
            return []

        elements = [e for e in (parsed.get("elements") or []) if isinstance(e, dict)]
        topology = parsed.get("elec_topology") or {}
        signals: list[ElecSignalItem] = []

        signals.extend(self._from_topology(topology))
        signals.extend(self._from_geometry(parsed))
        signals.extend(self._from_extracted_attrs(parsed.get("elec_extracted_attrs") or {}))
        signals.extend(self._from_element_keywords(elements))

        deduped = self._dedupe(signals)
        log.debug("[ElecSignalExtractor] signals=%d deduped=%d", len(signals), len(deduped))
        return deduped

    def _from_topology(self, topology: dict) -> list[ElecSignalItem]:
        signals: list[ElecSignalItem] = []
        for seg in topology.get("broken_segments") or []:
            if not isinstance(seg, dict):
                continue
            handle = str(seg.get("handle_a") or "").strip()
            if not handle:
                continue
            signals.append(ElecSignalItem(
                equipment_id=handle,
                signal_type="open_circuit",
                elec_category="continuity",
                observed_value=seg.get("gap_mm"),
                context={"handle_b": seg.get("handle_b"), "midpoint": seg.get("midpoint")},
            ))

        summary = topology.get("summary") or {}
        intent = str(summary.get("drawing_intent") or topology.get("drawing_intent") or "")
        if intent == "GROUNDING_PLAN":
            signals.append(ElecSignalItem(
                equipment_id="DRAWING",
                signal_type="grounding_plan",
                elec_category="grounding",
                context={"intent_confidence": summary.get("intent_confidence")},
            ))
        if intent == "HIGH_VOLTAGE_PLAN":
            signals.append(ElecSignalItem(
                equipment_id="DRAWING",
                signal_type="high_voltage_plan",
                elec_category="high_voltage",
                context={"intent_confidence": summary.get("intent_confidence")},
            ))

        for run in topology.get("circuit_runs") or []:
            if not isinstance(run, dict):
                continue
            run_id = str(run.get("run_id") or (run.get("handles") or [""])[0] or "").strip()
            if not run_id:
                continue
            cable_sqmm = _to_float(run.get("cable_sqmm"))
            if cable_sqmm and cable_sqmm > 0:
                signals.append(ElecSignalItem(
                    equipment_id=run_id,
                    signal_type="cable_size_observed",
                    elec_category="cable",
                    observed_value=cable_sqmm,
                    context={
                        "voltage": run.get("voltage"),
                        "total_length_mm": run.get("total_length_mm"),
                        "connected_panels": run.get("connected_panels") or [],
                    },
                ))
        return signals

    def _from_geometry(self, parsed: dict) -> list[ElecSignalItem]:
        signals: list[ElecSignalItem] = []
        for row in parsed.get("conduit_clearances") or []:
            if not isinstance(row, dict):
                continue
            handle = str(row.get("handle_a") or "").strip()
            if not handle:
                continue
            signals.append(ElecSignalItem(
                equipment_id=handle,
                signal_type="conduit_clearance_observed",
                elec_category="conduit",
                observed_value=row.get("separation_mm") or row.get("separation_drawing"),
                context=row,
            ))
        for row in parsed.get("panel_clearances") or []:
            if not isinstance(row, dict):
                continue
            handle = str(row.get("mep_handle") or "").strip()
            if not handle:
                continue
            signals.append(ElecSignalItem(
                equipment_id=handle,
                signal_type="panel_wall_clearance_observed",
                elec_category="panel",
                observed_value=row.get("separation_mm") or row.get("separation_drawing"),
                context=row,
            ))
        return signals

    def _from_extracted_attrs(self, attrs_by_handle: dict) -> list[ElecSignalItem]:
        signals: list[ElecSignalItem] = []
        if not isinstance(attrs_by_handle, dict):
            return signals
        for handle, attrs in attrs_by_handle.items():
            if not isinstance(attrs, dict):
                continue
            sq = _to_float(attrs.get("cable_sqmm"))
            breaker = _to_float(attrs.get("circuit_breaker_a"))
            if sq and breaker:
                signals.append(ElecSignalItem(
                    equipment_id=str(handle),
                    signal_type="breaker_cable_pair",
                    elec_category="breaker",
                    observed_value=sq,
                    context={"circuit_breaker_a": breaker, "attrs": attrs},
                ))
            if attrs.get("grounding") or attrs.get("ground_type"):
                signals.append(ElecSignalItem(
                    equipment_id=str(handle),
                    signal_type="grounding_attribute",
                    elec_category="grounding",
                    context={"attrs": attrs},
                ))
        return signals

    def _from_element_keywords(self, elements: list[dict]) -> list[ElecSignalItem]:
        signals: list[ElecSignalItem] = []
        seen_category: set[str] = set()
        for el in elements[:500]:
            blob = _text_blob(el)
            handle = _handle(el) or "DRAWING"
            matches: list[tuple[str, str]] = []
            if _GROUND_RE.search(blob):
                matches.append(("grounding_evidence", "grounding"))
            if _HV_RE.search(blob):
                matches.append(("high_voltage_evidence", "high_voltage"))
            if _CONDUIT_RE.search(blob):
                matches.append(("conduit_evidence", "conduit"))
            if _PANEL_RE.search(blob):
                matches.append(("panel_evidence", "panel"))
            if _CABLE_RE.search(blob):
                matches.append(("cable_evidence", "cable"))
            for signal_type, category in matches:
                key = f"{signal_type}:{handle}"
                if key in seen_category:
                    continue
                seen_category.add(key)
                signals.append(ElecSignalItem(
                    equipment_id=handle,
                    signal_type=signal_type,
                    elec_category=category,
                    context={"layer": el.get("layer"), "text": blob[:200]},
                ))
        return signals

    @staticmethod
    def _dedupe(signals: list[ElecSignalItem]) -> list[ElecSignalItem]:
        out: list[ElecSignalItem] = []
        seen: set[tuple[str, str, str]] = set()
        for signal in signals:
            key = (signal.equipment_id, signal.signal_type, signal.elec_category)
            if key in seen:
                continue
            seen.add(key)
            out.append(signal)
        return out
