"""
File    : backend/services/agents/elec/sub/review/parser.py
Author  : 김지우
Description : C# 클라이언트가 전송한 전기 도면 JSON을 정규화합니다.
"""

import json
import re
import logging
from typing import Any
from backend.services.agents.common.object_mapping_utils import (
    map_texts_to_blocks as _map_texts_to_blocks_util,
    ELEC_LAYER_BONUS,
)

# 레이어명에서 전선 굵기(SQ) 추출 — "Cable_2.5SQ", "W-4SQ", "E-CABLE-6SQ", "2.5sq" 등
_SQ_PATTERN = re.compile(r'(\d+(?:\.\d+)?)\s*[Ss][Qq]')

# 레이어명에서 전압(V) 추출 — "220V", "380V", "E-LINE-110V"
_VOLTAGE_PATTERN = re.compile(r'(\d+(?:\.\d+)?)\s*[Vv](?:\b|$)')


def _sq_from_layer(layer: str) -> float:
    """레이어명에서 전선 굵기(SQ)를 추출. 예: 'Cable_2.5SQ' → 2.5"""
    m = _SQ_PATTERN.search(layer)
    return float(m.group(1)) if m else 0.0


def _voltage_from_layer(layer: str) -> float:
    """레이어명에서 전압(V)을 추출. 예: 'E-220V' → 220.0"""
    m = _VOLTAGE_PATTERN.search(layer)
    return float(m.group(1)) if m else 0.0


class ParserAgent:

    def parse(self, layout_data: str | dict, mapping_table: dict | None = None) -> dict:
        try:
            data = json.loads(layout_data) if isinstance(layout_data, str) else layout_data
        except (json.JSONDecodeError, TypeError):
            return {"elements": [], "error": "layout_data 파싱 실패"}

        term_map = (mapping_table or {}).get("term_map", {})
        entity_type_map = (mapping_table or {}).get("entity_type_map", {})
        raw_entities = data.get("entities") or data.get("elements") or []

        parsed = []
        skipped = 0

        for idx, item in enumerate(raw_entities):
            if not isinstance(item, dict):
                continue

            # handle 없으면 layer+index 기반 합성 ID 생성
            raw_handle = item.get("handle")
            if raw_handle:
                handle = str(raw_handle)
            else:
                layer_hint = str(item.get("layer") or "")[:8]
                handle = f"__gen_{idx}_{layer_hint}"

            # type 없으면 block_name/effective_name 기준으로 INSERT 추론
            raw_type = item.get("type")
            if not raw_type:
                block_name_hint = item.get("effective_name") or item.get("block_name") or ""
                if block_name_hint:
                    raw_type = "INSERT"
                elif item.get("layer"):
                    raw_type = "LINE"
                else:
                    skipped += 1
                    continue

            raw_type = str(raw_type)
            layer = str(item.get("layer", ""))
            attrs = item.get("attributes") or {}

            # 동적 블록 대응: effective_name 우선 사용
            effective_name = str(item.get("effective_name") or "")
            raw_block_name = str(item.get("block_name") or "")
            block_name = effective_name or raw_block_name

            # 기구번호/장비 ID 추출 보강
            equipment_id = str(
                attrs.get("TAG_NAME")
                or attrs.get("TAG")
                or attrs.get("ID")
                or attrs.get("DEVICE_ID")
                or attrs.get("EQUIPMENT_ID")
                or attrs.get("MARK")
                or attrs.get("NAME")
                or handle
            )

            resolved_type = (
                term_map.get(block_name)
                or term_map.get(raw_block_name)
                or term_map.get(effective_name)
                or term_map.get(layer)
                or entity_type_map.get(raw_type.upper())
                or raw_type
            )

            position = (
                item.get("center")
                or item.get("insert_point")
                or item.get("start")
                or self._bbox_center(item.get("bbox"))
                or {"x": 0.0, "y": 0.0}
            )

            # 전압: 속성 → 아이템 필드 → 레이어명 순으로 폴백
            voltage = self._to_float(
                attrs.get("VOLTAGE")
                or attrs.get("V")
                or item.get("voltage", 0)
            )
            if not voltage:
                voltage = _voltage_from_layer(layer)

            # 전선 굵기(SQ): 속성 → 아이템 필드 → 레이어명 순으로 폴백
            cable_sqmm = self._to_float(
                attrs.get("CABLE_SQ")
                or attrs.get("SQ")
                or attrs.get("SQMM")
                or attrs.get("WIRE_SIZE")
                or item.get("sqmm", 0)
            )
            if not cable_sqmm:
                cable_sqmm = _sq_from_layer(layer)

            # 레이어명에서 파싱한 값을 attributes에도 주입
            enriched_attrs = dict(attrs)
            if cable_sqmm and not enriched_attrs.get("SQ"):
                enriched_attrs["SQ"] = str(cable_sqmm)
            if voltage and not enriched_attrs.get("VOLTAGE"):
                enriched_attrs["VOLTAGE"] = str(voltage)

            el_dict: dict = {
                "id": handle,
                "handle": handle,
                "block_name": raw_block_name,
                "effective_name": effective_name,
                "tag_name": equipment_id,
                "type": resolved_type,
                "raw_type": raw_type,
                "layer": layer,
                "position": position,
                "voltage": voltage,
                "cable_sqmm": cable_sqmm,
                "attributes": enriched_attrs,
            }
            for meta_key in (
                "electric_review_scope",
                "analysis_role",
                "drawing_role",
                "analysis_scope",
                "topology_candidate",
                "compliance_candidate",
                "cad_delete_candidate",
                "domain",
            ):
                if meta_key in item:
                    el_dict[meta_key] = item.get(meta_key)

            # LINE/ARC/POLYLINE 좌표는 topology builder가 사용할 수 있도록 보존
            raw_upper = raw_type.upper()
            if raw_upper == "LINE":
                if item.get("start"):
                    el_dict["start"] = item["start"]
                if item.get("end"):
                    el_dict["end"] = item["end"]
                if item.get("length") is not None:
                    el_dict["length"] = item["length"]
                if item.get("vertices"):
                    el_dict["vertices"] = item["vertices"]
                if item.get("angle") is not None:
                    el_dict["angle"] = self._to_float(item.get("angle"))

            elif raw_upper in ("POLYLINE", "LWPOLYLINE"):
                if item.get("vertices"):
                    el_dict["vertices"] = item["vertices"]
                if item.get("points"):
                    el_dict["points"] = item["points"]
                if item.get("start"):
                    el_dict["start"] = item["start"]
                if item.get("end"):
                    el_dict["end"] = item["end"]
                if item.get("length") is not None:
                    el_dict["length"] = item["length"]
                if item.get("is_closed") is not None:
                    el_dict["is_closed"] = bool(item.get("is_closed"))
                if item.get("area") is not None:
                    el_dict["area"] = self._to_float(item.get("area"))

            elif raw_upper == "ARC":
                if item.get("center"):
                    el_dict["center"] = item["center"]
                if item.get("start"):
                    el_dict["start"] = item["start"]
                if item.get("end"):
                    el_dict["end"] = item["end"]
                radius = self._to_float(item.get("radius", 0))
                if radius:
                    el_dict["radius"] = radius
                if item.get("start_angle") is not None:
                    el_dict["start_angle"] = self._to_float(item.get("start_angle"))
                if item.get("end_angle") is not None:
                    el_dict["end_angle"] = self._to_float(item.get("end_angle"))
                if item.get("length") is not None:
                    el_dict["length"] = item["length"]

            elif raw_upper == "CIRCLE":
                if item.get("center"):
                    el_dict["center"] = item["center"]
                radius = self._to_float(item.get("radius", 0))
                if radius:
                    el_dict["radius"] = radius

            elif raw_upper == "DIMENSION":
                text = item.get("text") or item.get("content")
                raw_measurement = item.get("measurement")
                measurement = self._to_float(raw_measurement)
                if not measurement and raw_measurement is not None:
                    measurement = self._dimension_value_from_text(str(raw_measurement))
                if not measurement and text is not None:
                    measurement = self._dimension_value_from_text(str(text))
                if measurement:
                    el_dict["measurement"] = measurement
                start = item.get("start") or item.get("xline1_point")
                end = item.get("end") or item.get("xline2_point")
                if start:
                    el_dict["start"] = start
                if end:
                    el_dict["end"] = end
                if text is not None:
                    el_dict["text"] = str(text)

            elif raw_upper in ("TEXT", "MTEXT"):
                text = item.get("text") or item.get("content")
                if text is not None:
                    el_dict["text"] = str(text)
                insert_point = item.get("insert_point") or item.get("insert")
                if insert_point:
                    el_dict["insert_point"] = insert_point
                if item.get("text_height") is not None:
                    el_dict["text_height"] = self._to_float(item.get("text_height"))
                if item.get("rotation") is not None:
                    el_dict["rotation"] = self._to_float(item.get("rotation"))

            parsed.append(el_dict)

        return {"elements": parsed}

    @staticmethod
    def _bbox_center(bbox: dict | None) -> dict | None:
        if not isinstance(bbox, dict):
            return None

        try:
            return {
                "x": (float(bbox["x1"]) + float(bbox["x2"])) / 2,
                "y": (float(bbox["y1"]) + float(bbox["y2"])) / 2,
            }
        except (KeyError, TypeError, ValueError):
            return None

    async def async_map_texts_to_blocks(
        self,
        text_entities: list[dict],
        block_entities: list[dict],
        *,
        ambiguity_threshold: float = 10.0,
    ) -> list[dict]:
        """
        텍스트와 전기 블록 매핑 + 모호 케이스 LLM fallback.
        """
        return await _map_texts_to_blocks_util(
            text_entities,
            block_entities,
            domain_hint="전기",
            layer_bonus_config=ELEC_LAYER_BONUS,
            ambiguity_threshold=ambiguity_threshold,
        )

    @staticmethod
    def _to_float(value: Any) -> float:
        try:
            return float(
                str(value)
                .replace("kV", "")
                .replace("KV", "")
                .replace("V", "")
                .replace("v", "")
                .replace("SQMM", "")
                .replace("sqmm", "")
                .replace("SQ", "")
                .replace("sq", "")
                .replace("A", "")
                .replace("a", "")
                .strip()
            )
        except (ValueError, TypeError):
            return 0.0

    @staticmethod
    def _dimension_value_from_text(text: str) -> float:
        match = re.search(r"[-+]?\d+(?:\.\d+)?", text or "")
        return float(match.group(0)) if match else 0.0
