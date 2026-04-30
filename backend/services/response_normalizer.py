"""
File    : backend/services/response_normalizer.py
Author  : 김다빈
Create  : 2026-04-13
Description : 도메인별 에이전트 출력을 REVIEW_SPEC.md annotated_entities 표준 포맷으로 정규화
              각 도메인 에이전트는 LawAgent 출력 포맷이 달라,
              Langfuse 평가 및 C# 연동 시 통일된 포맷이 필요하다.
              이 모듈이 raw violations dict를 받아 REVIEW_SPEC.md 스키마로 변환한다.

              지원 입력 포맷:
                - arch (건축): handle + severity + reference_rule + description + suggestion
                                + auto_fix_type + auto_fix_value — 이미 표준에 가까움
                - elec (전기): equipment_id + violation_type + reference_rule
                                + current_value + required_value + reason
                - 기타 도메인: 가능한 필드만 매핑, 나머지는 기본값

              출력: REVIEW_SPEC.md annotated_entity dict 리스트
                [
                  {
                    "handle": "1A2B",
                    "type": "BLOCK",
                    "layer": "E-CABLE",
                    "bbox": {"x1": 0, "y1": 0, "x2": 0, "y2": 0},
                    "violation": {
                      "id": "V001",
                      "severity": "Critical",
                      "rule": "KEC 142.6",
                      "description": "...",
                      "suggestion": "...",
                      "auto_fix": {"type": "ATTRIBUTE", "new_value": "4.0"} | null
                    }
                  }
                ]

Modification History :
    - 2026-04-13 (김다빈) : 초기 구현 — 도메인 전 도메인 표준화 레이어
"""

from __future__ import annotations


# severity 정규화 — 다양한 표현 → Critical/Major/Minor
_SEVERITY_MAP: dict[str, str] = {
    "critical": "Critical",
    "high": "Critical",
    "major": "Major",
    "medium": "Major",
    "minor": "Minor",
    "low": "Minor",
    "warning": "Minor",
}

# auto_fix type 정규화
_AUTOFIX_TYPE_MAP: dict[str, str] = {
    "attribute": "ATTRIBUTE",
    "layer": "LAYER",
    "text": "TEXT",
}


def _normalize_severity(raw: str | None) -> str:
    """severity 문자열을 Critical/Major/Minor 중 하나로 정규화"""
    if not raw:
        return "Major"
    return _SEVERITY_MAP.get(raw.lower(), "Major")


def _normalize_auto_fix(fix_type: str | None, fix_value: str | None) -> dict | None:
    """auto_fix_type + auto_fix_value → auto_fix dict 또는 None"""
    if not fix_type or fix_type.lower() in ("null", "none", ""):
        return None
    if not fix_value or fix_value.lower() in ("null", "none", ""):
        return None
    normalized_type = _AUTOFIX_TYPE_MAP.get(fix_type.lower(), fix_type.upper())
    return {"type": normalized_type, "new_value": str(fix_value)}


def normalize_arch_violations(
    entities: list[dict], raw_violations: list[dict]
) -> list[dict]:
    """건축 LawAgent 출력 → REVIEW_SPEC.md 포맷

    건축 에이전트는 이미 arch_agent.py에서 _build_annotated_entities()로
    변환하므로 이 함수는 이미 변환된 annotated_entities를 검증/재정규화할 때 사용.
    raw_violations가 직접 들어올 경우를 위해 handle 매핑 로직 포함.
    """
    handle_map = {e.get("handle", ""): e for e in entities}
    result = []

    for i, v in enumerate(raw_violations):
        handle = v.get("handle", "")
        entity = handle_map.get(handle) or (entities[0] if entities else {})
        if not entity:
            continue

        handle = handle or entity.get("handle", f"UNKNOWN_{i}")
        violation_id = f"V{str(i + 1).zfill(3)}"

        auto_fix = _normalize_auto_fix(
            v.get("auto_fix_type"), v.get("auto_fix_value")
        )

        result.append({
            "handle": handle,
            "type": entity.get("type", ""),
            "layer": entity.get("layer", ""),
            "bbox": entity.get("bbox", {"x1": 0, "y1": 0, "x2": 0, "y2": 0}),
            "violation": {
                "id": violation_id,
                "severity": _normalize_severity(v.get("severity")),
                "rule": v.get("reference_rule", ""),
                "description": v.get("description", ""),
                "suggestion": v.get("suggestion", ""),
                "auto_fix": auto_fix,
            },
        })

    return result


def normalize_elec_violations(
    entities: list[dict], raw_violations: list[dict]
) -> list[dict]:
    """전기 LawAgent 출력(equipment_id 포맷) → REVIEW_SPEC.md 포맷

    전기 포맷:
      equipment_id, violation_type, reference_rule,
      current_value, required_value, reason

    handle 매핑:
      equipment_id → handle (entity handle과 일치하면 매핑, 없으면 그대로 사용)
    """
    # equipment_id → entity handle 역매핑 시도
    # 전기 엔티티에 equipment_id 필드가 있으면 매핑
    equip_to_entity: dict[str, dict] = {}
    for e in entities:
        eid = e.get("equipment_id") or e.get("handle", "")
        if eid:
            equip_to_entity[eid] = e

    handle_map = {e.get("handle", ""): e for e in entities}
    result = []

    for i, v in enumerate(raw_violations):
        equip_id = v.get("equipment_id", "")
        # equipment_id로 엔티티 찾기 → 없으면 handle로 시도 → 폴백
        entity = (
            equip_to_entity.get(equip_id)
            or handle_map.get(equip_id)
            or (entities[0] if entities else {})
        )

        handle = entity.get("handle", equip_id) if entity else equip_id
        violation_id = f"V{str(i + 1).zfill(3)}"

        # 전기 포맷에서 description 조합 (reason + current/required value)
        reason = v.get("reason", "")
        current_val = v.get("current_value", "")
        required_val = v.get("required_value", "")
        if current_val and required_val:
            description = f"{reason} (현재: {current_val}, 기준: {required_val})"
        else:
            description = reason

        auto_fix = _normalize_auto_fix(
            v.get("auto_fix_type"), v.get("auto_fix_value")
        )
        # LLM이 auto_fix 객체를 직접 반환한 경우도 수용
        if auto_fix is None and isinstance(v.get("auto_fix"), dict):
            auto_fix = v["auto_fix"]

        result.append({
            "handle": handle,
            "type": entity.get("type", "") if entity else "",
            "layer": entity.get("layer", "") if entity else "",
            "bbox": entity.get("bbox", {"x1": 0, "y1": 0, "x2": 0, "y2": 0}) if entity else {"x1": 0, "y1": 0, "x2": 0, "y2": 0},
            "violation": {
                "id": violation_id,
                # 전기 포맷엔 severity 없음 → violation_type으로 추론
                "severity": _infer_severity_from_type(v.get("violation_type", "")),
                "rule": v.get("reference_rule", ""),
                "description": description,
                "suggestion": v.get("suggestion") or v.get("required_value") or "",
                "auto_fix": auto_fix,
            },
        })

    return result


def normalize_generic_violations(
    entities: list[dict], raw_violations: list[dict], domain: str = ""
) -> list[dict]:
    """소방/배관 등 미구현 도메인의 범용 변환기

    가능한 필드만 매핑, 나머지는 기본값으로 채움.
    각 도메인 담당자가 구현 완료 후 도메인 전용 normalizer로 교체 예정.
    """
    handle_map = {e.get("handle", ""): e for e in entities}
    result = []

    for i, v in enumerate(raw_violations):
        # handle 후보 필드를 순서대로 탐색
        handle = (
            v.get("handle")
            or v.get("equipment_id")
            or v.get("entity_id")
            or v.get("id")
            or ""
        )
        entity = handle_map.get(handle) or (entities[0] if entities else {})
        handle = handle or (entity.get("handle", f"UNKNOWN_{i}") if entity else f"UNKNOWN_{i}")

        # description 후보 필드
        description = (
            v.get("description")
            or v.get("reason")
            or v.get("detail")
            or ""
        )
        # current/required value가 있으면 description에 추가
        current_val = v.get("current_value", "")
        required_val = v.get("required_value", "")
        if current_val and required_val and current_val not in description:
            description = f"{description} (현재: {current_val}, 기준: {required_val})"

        violation_id = f"V{str(i + 1).zfill(3)}"
        auto_fix = _normalize_auto_fix(
            v.get("auto_fix_type"), v.get("auto_fix_value")
        )

        result.append({
            "handle": handle,
            "type": entity.get("type", "") if entity else "",
            "layer": entity.get("layer", "") if entity else "",
            "bbox": entity.get("bbox", {"x1": 0, "y1": 0, "x2": 0, "y2": 0}) if entity else {"x1": 0, "y1": 0, "x2": 0, "y2": 0},
            "violation": {
                "id": violation_id,
                "severity": _normalize_severity(v.get("severity")),
                "rule": v.get("reference_rule") or v.get("rule") or "",
                "description": description,
                "suggestion": v.get("suggestion") or "",
                "auto_fix": auto_fix,
            },
        })

    return result


def normalize_agent_output(
    domain: str,
    entities: list[dict],
    raw_violations: list[dict],
) -> list[dict]:
    """도메인 이름을 받아 적절한 normalizer를 자동 선택

    사용법:
        from backend.services.response_normalizer import normalize_agent_output

        annotated = normalize_agent_output(
            domain="elec",
            entities=payload["entities"],
            raw_violations=law_agent.check_compliance(...)
        )
    """
    if domain == "arch":
        return normalize_arch_violations(entities, raw_violations)
    elif domain == "elec":
        return normalize_elec_violations(entities, raw_violations)
    else:
        # fire, pipe 등 — 범용 변환기 사용
        return normalize_generic_violations(entities, raw_violations, domain)


# ── 내부 헬퍼 ────────────────────────────────────────────────────────────────

def _infer_severity_from_type(violation_type: str) -> str:
    """전기 위반 타입에서 severity 추론 (전기 LawAgent가 severity를 반환하지 않으므로)"""
    vt = violation_type.upper()
    if "POWER" in vt or "OVERLOAD" in vt or "SHORT" in vt:
        return "Critical"
    if "DISTANCE" in vt or "HEIGHT" in vt or "VOLTAGE" in vt:
        return "Major"
    return "Minor"
