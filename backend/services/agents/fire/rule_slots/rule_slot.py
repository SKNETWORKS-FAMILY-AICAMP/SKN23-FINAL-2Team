"""
File    : backend/services/agents/fire/rule_slots/rule_slot.py
Description : RuleSlot 데이터 구조, comparator 판정 함수, YAML 레지스트리 로더
"""
from __future__ import annotations

import logging
import pathlib
from dataclasses import dataclass
from typing import Optional

import yaml

_YAML_PATH = pathlib.Path(__file__).parent / "nfsc_defaults.yaml"

log = logging.getLogger(__name__)


@dataclass
class RuleSlot:
    rule_id:            str
    rule_topic:         str
    equipment_category: str   # sprinkler | detector | hydrant | extinguisher
    candidate_type:     str   # spacing | height | missing | attribute | coverage
    measure_type:       str   # distance | height | count | pressure | diameter
    unit:               str   # mm | m | ea | MPa
    comparator:         str   # lte | gte | eq
    threshold:          float
    condition_text:     str
    source_type:        str   # nfsc | project_spec | site_rule
    priority:           int   # nfsc=10, project_spec=20, site_rule=30


def is_violated(slot: RuleSlot, observed_value: float) -> bool:
    """
    comparator 방향 주의:
      lte: observed <= threshold가 정상 → observed > threshold면 위반
      gte: observed >= threshold가 정상 → observed < threshold면 위반

    주의: observed_value는 호출 전에 float로 변환되어야 한다.
    None이 전달되면 False(위반 아님)를 반환한다.
    """
    if observed_value is None:
        return False
    if slot.comparator == "lte":
        return observed_value > slot.threshold
    if slot.comparator == "gte":
        return observed_value < slot.threshold
    if slot.comparator == "eq":
        # 부동소수점 정밀도 주의: mm 단위 실측값과 YAML 기준값이 bit-identical하지 않을 수 있다.
        # eq 슬롯 사용 시 threshold를 정수 단위로 정의하거나 허용 오차를 추가로 검토할 것.
        return observed_value != slot.threshold
    log.warning("[RuleSlot] 알 수 없는 comparator: %s", slot.comparator)
    return False


_DEFAULT_SLOTS: list[RuleSlot] | None = None


def load_rule_slots(yaml_path: pathlib.Path = _YAML_PATH) -> list[RuleSlot]:
    """YAML 파일에서 RuleSlot 목록을 로드한다. 기본 경로는 모듈 레벨 캐시를 사용한다."""
    global _DEFAULT_SLOTS
    if yaml_path == _YAML_PATH and _DEFAULT_SLOTS is not None:
        return _DEFAULT_SLOTS
    try:
        with open(yaml_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or []
    except FileNotFoundError:
        log.error("[RuleSlot] YAML 파일 없음: %s", yaml_path)
        return []
    slots = []
    for item in raw:
        try:
            slots.append(RuleSlot(
                rule_id=str(item["rule_id"]),
                rule_topic=str(item.get("rule_topic", "")),
                equipment_category=str(item["equipment_category"]),
                candidate_type=str(item["candidate_type"]),
                measure_type=str(item.get("measure_type", "distance")),
                unit=str(item.get("unit", "mm")),
                comparator=str(item["comparator"]),
                threshold=float(item["threshold"]),
                condition_text=str(item.get("condition_text", "")),
                source_type=str(item.get("source_type", "nfsc")),
                priority=int(item.get("priority", 10)),
            ))
        except (KeyError, ValueError, TypeError) as e:
            log.warning("[RuleSlot] 항목 로드 실패 (건너뜀): %s — %s", item, e)
    if yaml_path == _YAML_PATH:
        _DEFAULT_SLOTS = slots
    return slots


def find_slot(
    slots: list[RuleSlot],
    equipment_category: str,
    candidate_type: str,
) -> Optional[RuleSlot]:
    """
    동일 equipment_category + candidate_type 중 priority 가장 높은 슬롯을 반환.
    """
    matched = [
        s for s in slots
        if s.equipment_category == equipment_category
        and s.candidate_type == candidate_type
    ]
    if not matched:
        return None
    return max(matched, key=lambda s: s.priority)
