"""
File    : backend/services/agents/elec/sub/deterministic_checker.py
Author  : AI Assistant
Create  : 2026-04-28
Description :
  LLM에 의존하지 않는 확정적(코드 기반) 전기 위반 검출기.

  ★ 핵심 원칙: "없음"을 근거로 위반을 보고하지 않는다.
     검출 능력이 불완전한 상태에서 "X가 없다 → 위반"은 환각과 같다.
     반드시 "X가 있고, 그 값이 규정 기준 Y를 초과/위반한다"는 형태로만 보고.

  검출 항목 (양성 증거 기반만):
    1. topology broken_segments → 단선 (양 끝 handle + gap 수치 확인)
    2. 추출된 전기 속성 간 모순 → 동일 블록 내 380V/220V 충돌 등
"""
from __future__ import annotations

import logging
from typing import Any

_log = logging.getLogger(__name__)


def run_deterministic_checks(
    elements: list[dict],
    extracted_attrs: dict[str, dict],
    topology: dict,
) -> list[dict]:
    """
    확정적 위반 검출을 실행한다.

    ★ "없다 → 위반" 패턴 금지.
       "있다 + 값이 위반 → 위반" 패턴만 허용.
    """
    violations: list[dict] = []

    # ── 1. topology 단선 (broken_segments) — 양성 증거 확정 ───────────────
    # broken_segments는 두 전선 사이에 실제로 gap_mm > 0인 끊김이 감지된 것.
    # 이는 "없다"가 아니라 "끊어진 간격이 있다"는 양성 증거.
    for seg in topology.get("broken_segments", []):
        violations.append({
            "object_id": seg.get("handle_a", ""),
            "violation_type": "open_circuit_error",
            "reason": (
                f"전선 단선 감지: {seg['handle_a']}와 {seg['handle_b']} 사이 "
                f"{seg['gap_mm']}mm 간격으로 끊어짐"
            ),
            "legal_reference": "KEC 232 배선의 시설",
            "suggestion": "끊어진 전선을 연결하십시오",
            "current_value": f"끊어진 간격 {seg['gap_mm']}mm",
            "required_value": "0mm (연속 연결)",
            "midpoint": seg.get("midpoint"),
        })

    # ── 2. 동일 블록 내 전기 속성 모순 — 양성 증거 확정 ────────────────────
    # 하나의 블록에 매핑된 텍스트에서 추출된 속성이 서로 모순되는 경우.
    # 예: "380V" 라벨과 "220V" 라벨이 같은 블록에 매핑 → 설계 오류 가능성
    # (단, voltage_alt_v가 있으면 듀얼 전압이므로 정상)
    for bh, attrs in extracted_attrs.items():
        v = attrs.get("voltage_v")
        v_alt = attrs.get("voltage_alt_v")
        if v and v_alt:
            # 380V/440V 같은 듀얼 전압은 정상
            # 하지만 380V/220V처럼 상(phase)이 다른 전압이 동시에 있으면 경고
            if abs(v - v_alt) > 100 and min(v, v_alt) < 300 and max(v, v_alt) >= 380:
                violations.append({
                    "object_id": bh,
                    "violation_type": "voltage_mismatch",
                    "reason": (
                        f"블록 {bh}에 단상({min(v, v_alt)}V)과 "
                        f"3상({max(v, v_alt)}V) 전압이 동시 표기됨"
                    ),
                    "legal_reference": "KEC 232.29 전압강하",
                    "suggestion": "해당 설비의 정격 전압을 확인하십시오",
                    "current_value": f"{v}V / {v_alt}V",
                    "required_value": "단일 정격 전압 또는 호환 전압(380V/440V 등)",
                })

    det_count = len(violations)
    broken_count = len(topology.get("broken_segments", []))

    _log.info(
        "[DeterministicChecker] 확정적 위반 %d건 (broken=%d, 속성모순=%d)",
        det_count, broken_count, det_count - broken_count,
    )
    return violations
