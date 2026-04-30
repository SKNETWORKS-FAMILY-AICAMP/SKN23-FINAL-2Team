"""
File    : backend/services/agents/elec/sub/elec_attr_extractor.py
Author  : AI Assistant
Create  : 2026-04-28
Description :
  매핑된 텍스트 라벨에서 전기 속성(전압, 위상, 주파수, 전선 굵기, 전류 등)을
  정규식으로 추출하여 해당 블록 엔티티에 주입하는 유틸리티.

  도면의 블록에 ATTDEF(속성 정의)가 없고, 전기 정보가 근처 TEXT로만 표기된
  경우에 사용된다. 텍스트→블록 매핑(object_mapping)이 선행되어야 함.

  지원 추출 패턴:
    - 전압 : "380V", "220V", "AC110V", "DC24V" → voltage_v
    - 위상 : "3∅", "3Φ", "1∅", "단상", "3상"  → phase
    - 주파수: "60HZ", "50Hz"                     → frequency_hz
    - 전선  : "2.5SQ", "10SQ", "16SQ"            → cable_sqmm
    - 전류  : "20A", "100A"                       → current_a
    - 접지  : "GND", "GROUND", "접지"             → is_ground
    - 동력  : "MOTOR", "모터", "모타"              → is_motor
"""
from __future__ import annotations

import logging
import re
from typing import Any

_log = logging.getLogger(__name__)

# ── 정규식 패턴 (컴파일) ──────────────────────────────────────────────────────

# 전압: "380V", "AC220V", "DC24V", "110~220V"
_VOLTAGE_RE = re.compile(
    r"(?:AC|DC)?\s*(\d{2,4})\s*V(?!\w)",
    re.IGNORECASE,
)

# 위상: "3∅", "3Φ", "3φ", "3상", "단상", "1∅"
_PHASE_RE = re.compile(
    r"(\d)\s*[∅Φφ상]|단상",
    re.IGNORECASE,
)

# 주파수: "60HZ", "50Hz"
_FREQ_RE = re.compile(
    r"(\d{2})\s*HZ",
    re.IGNORECASE,
)

# 전선 굵기: "2.5SQ", "10SQ", "16sq"
_SQ_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*SQ",
    re.IGNORECASE,
)

# 전류: "20A", "100A"  (V 바로 뒤의 A는 VA 단위이므로 제외)
_AMP_RE = re.compile(
    r"(?<!V)(?<!\d)(\d+(?:\.\d+)?)\s*A(?!\w)",
    re.IGNORECASE,
)

# 접지 키워드
_GND_RE = re.compile(
    r"\bGND\b|\bGROUND\b|\bEARTH\b|접지",
    re.IGNORECASE,
)

# 모터 키워드
_MOTOR_RE = re.compile(
    r"\bMOTOR\b|모터|모타",
    re.IGNORECASE,
)

# 차단기 키워드
_BREAKER_RE = re.compile(
    r"\bMCCB\b|\bMCB\b|\bELB\b|\bNFB\b|\bACB\b|차단기",
    re.IGNORECASE,
)


def extract_elec_attrs(text: str) -> dict[str, Any]:
    """
    텍스트 문자열에서 전기 속성을 정규식으로 추출한다.

    Returns:
        추출된 속성 딕셔너리. 해당 패턴이 없으면 키 자체가 없음.
        예: {"voltage_v": 380, "phase": 3, "frequency_hz": 60}
    """
    attrs: dict[str, Any] = {}

    # 전압 (여러 전압이 있을 수 있음: "380V/440V" → 첫 번째 사용)
    vm = _VOLTAGE_RE.findall(text)
    if vm:
        attrs["voltage_v"] = int(vm[0])
        if len(vm) > 1:
            attrs["voltage_alt_v"] = int(vm[1])

    # 위상
    pm = _PHASE_RE.search(text)
    if pm:
        if "단상" in text:
            attrs["phase"] = 1
        elif pm.group(1):
            attrs["phase"] = int(pm.group(1))

    # 주파수
    fm = _FREQ_RE.search(text)
    if fm:
        attrs["frequency_hz"] = int(fm.group(1))

    # 전선 굵기
    sm = _SQ_RE.findall(text)
    if sm:
        attrs["cable_sqmm"] = float(sm[0])

    # 전류
    am = _AMP_RE.findall(text)
    if am:
        attrs["current_a"] = float(am[0])

    # 설비 타입 플래그
    if _GND_RE.search(text):
        attrs["equip_type"] = "ground"
    elif _MOTOR_RE.search(text):
        attrs["equip_type"] = "motor"
    elif _BREAKER_RE.search(text):
        attrs["equip_type"] = "breaker"

    return attrs


def inject_elec_attrs_from_mapping(
    elements: list[dict],
    object_mapping: list[dict],
) -> dict[str, dict]:
    """
    object_mapping의 label 텍스트에서 전기 속성을 추출하여
    해당 block_handle의 element에 주입한다.

    Args:
        elements: parsed["elements"] — 파싱된 엔티티 리스트 (in-place 수정)
        object_mapping: 텍스트→블록 매핑 리스트

    Returns:
        block_handle → 추출된 속성 딕셔너리 (디버그/로깅용)
    """
    # 1. block_handle별 모든 라벨 수집
    handle_labels: dict[str, list[str]] = {}
    for m in object_mapping:
        bh = str(m.get("block_handle", ""))
        label = str(m.get("label", "")).strip()
        if bh and label:
            handle_labels.setdefault(bh, []).append(label)

    # 2. 각 블록의 라벨들에서 전기 속성 추출
    handle_attrs: dict[str, dict] = {}
    for bh, labels in handle_labels.items():
        merged: dict[str, Any] = {}
        for label in labels:
            extracted = extract_elec_attrs(label)
            for k, v in extracted.items():
                if k not in merged:  # 첫 번째 추출값 우선
                    merged[k] = v
        if merged:
            handle_attrs[bh] = merged

    # 3. elements에 주입
    injected_count = 0
    for el in elements:
        el_handle = str(el.get("handle") or el.get("id") or "")
        attrs = handle_attrs.get(el_handle)
        if attrs:
            el.setdefault("elec_attrs", {}).update(attrs)
            injected_count += 1

    _log.info(
        "[ElecAttrExtractor] %d개 블록에서 전기 속성 추출, %d개 element에 주입 완료",
        len(handle_attrs), injected_count,
    )

    return handle_attrs
