"""
File    : backend/services/review_service.py
Author  : 양창일
Create  : 2026-04-13
Description : drawing_data와 retrieved_laws를 기반으로 review_result를 생성하는 서비스

Modification History :
    - 2026-04-13 (양창일) : 기본 규칙 기반 review_result 생성 함수 추가
    - 2026-04-13 (양창일) : 도메인별 review 설정 분기 추가
"""

from typing import Any


DOMAIN_REVIEW_CONFIG: dict[str, dict[str, str]] = {
    "arch": {
        "violation_type": "ARCH_REVIEW_REQUIRED",
        "action_label": "space layout and egress rule",
    },
    "elec": {
        "violation_type": "ELEC_REVIEW_REQUIRED",
        "action_label": "electrical installation rule",
    },
    "fire": {
        "violation_type": "FIRE_REVIEW_REQUIRED",
        "action_label": "fire safety rule",
    },
    "pipe": {
        "violation_type": "PIPE_REVIEW_REQUIRED",
        "action_label": "piping rule",
    },
}


def build_review_result(
    drawing_data: dict[str, Any],
    retrieved_laws: list[dict[str, Any]],
    active_object_ids: list[str],
    review_label: str,
    domain: str,
) -> dict[str, Any]:
    config = DOMAIN_REVIEW_CONFIG.get(
        domain,
        {
            "violation_type": "REVIEW_REQUIRED",
            "action_label": "review rule",
        },
    )

    referenced_laws = [
        law.get("legal_reference", "")
        for law in retrieved_laws
        if law.get("legal_reference")
    ]

    violations: list[dict[str, str]] = []
    suggestions: list[str] = []
    objects = drawing_data.get("objects", [])

    if active_object_ids and referenced_laws:
        primary_object_id = active_object_ids[0]
        primary_law = referenced_laws[0]
        primary_snippet = _find_snippet(retrieved_laws, primary_law)
        suggestion = (
            f"Review object {primary_object_id} against the {config['action_label']} "
            f"reference {primary_law}."
        )

        violations.append(
            {
                "object_id": primary_object_id,
                "violation_type": config["violation_type"],
                "reason": primary_snippet or f"Rule review required for {primary_object_id}.",
                "legal_reference": primary_law,
                "suggestion": suggestion,
                "current_value": "pending_review",
                "required_value": primary_law,
            }
        )
        suggestions.append(suggestion)
    elif objects and referenced_laws:
        primary_object = objects[0]
        primary_object_id = str(primary_object.get("object_id", primary_object.get("handle", "unknown")))
        primary_law = referenced_laws[0]
        primary_snippet = _find_snippet(retrieved_laws, primary_law)
        suggestion = (
            f"Inspect object {primary_object_id} for compliance with {primary_law}."
        )

        violations.append(
            {
                "object_id": primary_object_id,
                "violation_type": config["violation_type"],
                "reason": primary_snippet or f"Rule review required for {primary_object_id}.",
                "legal_reference": primary_law,
                "suggestion": suggestion,
                "current_value": "drawing_check_required",
                "required_value": primary_law,
            }
        )
        suggestions.append(suggestion)

    is_violation = len(violations) > 0
    final_message = (
        f"{review_label} completed with {len(violations)} review item(s)."
        if is_violation
        else f"{review_label} completed with no immediate review item."
    )

    return {
        "is_violation": is_violation,
        "violations": violations,
        "suggestions": suggestions,
        "referenced_laws": referenced_laws,
        "final_message": final_message,
    }


def _find_snippet(retrieved_laws: list[dict[str, Any]], legal_reference: str) -> str:
    for law in retrieved_laws:
        if law.get("legal_reference") == legal_reference:
            return str(law.get("snippet", ""))
    return ""
