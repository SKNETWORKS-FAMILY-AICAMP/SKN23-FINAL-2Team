"""
File    : backend/services/agents/elec/sub/elec_attr_extractor.py
Description :
  텍스트 기반 전기 속성 추출 + 전기 설비 분류
"""

from __future__ import annotations

import logging
import re
from typing import Any

_log = logging.getLogger(__name__)

# ── 정규식 패턴 ─────────────────────────────────────────

_VOLTAGE_RE = re.compile(r"(?:AC|DC)?\s*(\d{2,4})\s*V", re.IGNORECASE)
_PHASE_RE   = re.compile(r"(\d)\s*[∅Φφ상]|단상", re.IGNORECASE)
_FREQ_RE    = re.compile(r"(\d{2})\s*HZ", re.IGNORECASE)
_SQ_RE      = re.compile(r"(\d+(?:\.\d+)?)\s*SQ", re.IGNORECASE)
_WIRE_SIZE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:mm\s*(?:2|²)?|㎟|SQ)",
    re.IGNORECASE,
)
_POLE_RE = re.compile(r"\b(\d+)\s*P\b", re.IGNORECASE)
_BOLT_RE = re.compile(r"\bM\s*(\d+(?:\.\d+)?)\b", re.IGNORECASE)
_AMP_RE     = re.compile(r"(?<!V)(\d+(?:\.\d+)?)\s*A", re.IGNORECASE)
_LABEL_KEY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("suitable_wire", re.compile(r"\uc801\ud569\s*\uc804\uc120")),
    ("standard_product", re.compile(r"\ud45c\uc900\s*\ud488")),
)

_GND_RE     = re.compile(r"\bGND\b|\bGROUND\b|\bEARTH\b|접지", re.IGNORECASE)
_MOTOR_RE   = re.compile(r"\bMOTOR\b|모터|모타", re.IGNORECASE)
_BREAKER_RE = re.compile(r"\bMCCB\b|\bMCB\b|\bELB\b|\bACB\b|차단기", re.IGNORECASE)


# ── 1. 텍스트 → 전기 속성 추출 ─────────────────────────

def extract_elec_attrs(text: str) -> dict[str, Any]:
    attrs: dict[str, Any] = {}

    vm = _VOLTAGE_RE.findall(text)
    if vm:
        attrs["voltage_v"] = int(vm[0])

    pm = _PHASE_RE.search(text)
    if pm:
        if "단상" in text:
            attrs["phase"] = 1
        else:
            attrs["phase"] = int(pm.group(1))

    fm = _FREQ_RE.search(text)
    if fm:
        attrs["frequency_hz"] = int(fm.group(1))

    wire_matches = _WIRE_SIZE_RE.findall(text)
    if wire_matches:
        wire_size = _normalize_wire_size(wire_matches[0])
        attrs["wire_size"] = wire_size
        attrs["cable_sqmm"] = float(wire_matches[0])
    else:
        sm = _SQ_RE.findall(text)
        if sm:
            attrs["cable_sqmm"] = float(sm[0])

    pole_options = _unique_keep_order(f"{p.upper()}P" for p in _POLE_RE.findall(text))
    if pole_options:
        attrs["pole_options"] = pole_options

    bm = _BOLT_RE.search(text)
    if bm:
        attrs["bolt_size"] = f"M{_normalize_number_text(bm.group(1))}"

    label_keys = [
        key
        for key, pattern in _LABEL_KEY_PATTERNS
        if pattern.search(text)
    ]
    if label_keys:
        attrs["label_keys"] = label_keys

    am = _AMP_RE.findall(text)
    if am:
        attrs["current_a"] = float(am[0])

    if _GND_RE.search(text):
        attrs["equip_type"] = "ground"
    elif _MOTOR_RE.search(text):
        attrs["equip_type"] = "motor"
    elif _BREAKER_RE.search(text):
        attrs["equip_type"] = "breaker"

    return attrs


# ── 2. BLOCK/레이어 기반 설비 분류 ───────────────────────

def _normalize_wire_size(raw_size: str) -> str:
    return f"{_normalize_number_text(raw_size)}mm2"


def _normalize_number_text(raw_number: str) -> str:
    number = float(raw_number)
    return str(int(number)) if number.is_integer() else str(number)


def _unique_keep_order(values) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _merge_attr(target: dict[str, Any], key: str, value: Any) -> None:
    if key in {"pole_options", "label_keys"}:
        existing = target.get(key) or []
        target[key] = _unique_keep_order([*existing, *value])
    elif key not in target:
        target[key] = value


def classify_elec_entity(el: dict) -> dict[str, Any]:
    name = " ".join([
        str(el.get("effective_name") or ""),
        str(el.get("block_name") or ""),
        str(el.get("layer") or ""),
        str(el.get("type") or ""),
    ]).upper()

    result: dict[str, Any] = {}

    if "EXIT" in name and "LIGHT" in name:
        result["category"] = "LIGHT"
        result["subtype"] = "EXIT_LIGHT"

    elif "LIGHT" in name or "LAMP" in name:
        result["category"] = "LIGHT"

    elif "SWITCH" in name or re.search(r"\bSW\b", name):
        result["category"] = "SWITCH"

    elif "SOCKET" in name or "OUTLET" in name:
        result["category"] = "SOCKET"

    elif "PNL" in name or "PANEL" in name:
        result["category"] = "PANEL"

    elif any(k in name for k in ["MCCB", "MCB", "ELB", "ACB", "BREAKER"]):
        result["category"] = "BREAKER"

    elif "CABLE" in name or "WIRE" in name or "SQ" in name:
        result["category"] = "CABLE"

    if result:
        result["domain"] = "ELEC"

    return result


# ── 3. 속성 주입 + 분류 주입 ───────────────────────────

def inject_elec_data(
    elements: list[dict],
    object_mapping: list[dict],
) -> dict[str, dict]:
    """
    1. 텍스트 → 전기 속성 추출
    2. BLOCK → 전기 설비 분류
    """

    # ── 텍스트 → 블록 매핑 기반 속성 수집 ─────────────
    handle_labels: dict[str, list[str]] = {}

    for m in object_mapping:
        bh = str(m.get("block_handle", ""))
        label = str(m.get("label", "")).strip()
        if bh and label:
            handle_labels.setdefault(bh, []).append(label)

    handle_attrs: dict[str, dict] = {}

    for bh, labels in handle_labels.items():
        merged: dict[str, Any] = {}

        for label in labels:
            extracted = extract_elec_attrs(label)
            for k, v in extracted.items():
                _merge_attr(merged, k, v)

        if merged:
            handle_attrs[bh] = merged

    # ── element에 주입 ─────────────────────────────
    injected_count = 0
    classified_count = 0

    for el in elements:
        handle = str(el.get("handle") or el.get("id") or "")

        # 1. 속성 주입
        attrs = handle_attrs.get(handle)
        if attrs:
            el.setdefault("elec_attrs", {}).update(attrs)
            injected_count += 1

        # 2. 설비 분류
        classified = classify_elec_entity(el)
        if classified:
            el.setdefault("elec_attrs", {}).update(classified)
            el.update(classified)
            classified_count += 1

    _log.info(
        "[ElecAttrExtractor] 속성주입=%d, 분류=%d",
        injected_count,
        classified_count,
    )

    return handle_attrs
