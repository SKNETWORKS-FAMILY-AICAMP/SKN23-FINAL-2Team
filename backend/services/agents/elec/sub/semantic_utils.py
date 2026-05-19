from __future__ import annotations


def infer_table_cell_semantic(text: str) -> str:
    upper = str(text or "").upper()
    if "TEST" in upper:
        return "test_annotation"
    if "E1" in upper or "E2" in upper or "GROUND" in upper or "GND" in upper or "접지" in str(text):
        return "grounding_spec"
    if "CABLE" in upper or "SQ" in upper or "MM2" in upper or "㎟" in str(text):
        return "cable_spec"
    if "QTY" in upper or "수량" in str(text):
        return "quantity"
    return "equipment_annotation"


def build_table_mapping_debug(elements: list[dict]) -> dict:
    text_items = [
        e for e in elements
        if str(e.get("type") or e.get("raw_type") or "").upper() in {"TEXT", "MTEXT"}
        and str(e.get("text") or e.get("content") or "").strip()
    ]
    table_keywords = ("CABLE", "GROUND", "GND", "TEST", "E1", "E2", "SPEC", "QTY", "SQ", "MM2", "접지", "수량")
    cells = []
    for e in text_items:
        text = str(e.get("text") or e.get("content") or "").strip()
        if any(k.upper() in text.upper() for k in table_keywords):
            cells.append({
                "handle": str(e.get("handle") or ""),
                "text": text,
                "layer": str(e.get("layer") or ""),
            })
    return {
        "table_count": 1 if len(cells) >= 3 else 0,
        "table_cells": cells[:200],
        "mapped_table_annotations": [
            {"text_handle": c["handle"], "semantic": infer_table_cell_semantic(c["text"])}
            for c in cells[:200]
        ],
    }
