"""
File    : backend/services/agents/fire/sub/review/parser.py
Author  : 김민정
Create  : 2026-04-15
Modified: 2026-04-24 (Phase 8 — parse() 표준 인터페이스 추가, 소방 도메인 속성 추출 강화)
Description : 도면 데이터에서 소방 설비 객체들의 좌표 및 메타데이터를 추출합니다.

Modification History:
    - 2026-04-15 (김민정) : 도면 엔티티 기하 정보 및 속성 데이터 파싱 로직 구현
    - 2026-04-24 (Phase 8) : parse() 표준 인터페이스 추가 (mapping_table 지원, elements[] 반환),
                             fire_category 자동 추론, 소방 도메인 속성(coverage_area, height 등) 추출
"""

import json
import logging
import math
import re
from typing import Any

from backend.services.agents.common.object_mapping_utils import map_texts_to_blocks as _map_texts_to_blocks_util

_HEAD_DEDUP_TOL_MM = 10.0

# ── 소방 카테고리 추론 패턴 ──────────────────────────────────────────────────
_FIRE_CAT_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"SPK|SPRIN|스프링|헤드|HEAD",        re.IGNORECASE), "sprinkler"),
    (re.compile(r"FDH|DET|감지|DETECT",               re.IGNORECASE), "detector"),
    (re.compile(r"HYD|소화전|HYDRANT",                re.IGNORECASE), "hydrant"),
    (re.compile(r"PUMP|펌프|FP-",                     re.IGNORECASE), "pump"),
    (re.compile(r"ALARM|경보|BELL|SIREN",             re.IGNORECASE), "alarm"),
    (re.compile(r"PANEL|패널|제어반|CTRL|CONTROL",    re.IGNORECASE), "panel"),
    (re.compile(r"PIPE|배관|MAIN|BRANCH|RISER",       re.IGNORECASE), "pipe"),
]
_ARCH_LAYER_RE = re.compile(
    r"건축|ARCH|A[-_]|WALL|DOOR|WINDOW|ROOM|GRID|치수|벽|문|창",
    re.IGNORECASE,
)
_FIRE_SIGNAL_RE = re.compile(
    r"소방|FIRE|SP[-_]?|SPK|SPRIN|스프링|헤드|HEAD|"
    r"FDH|DET|감지|HYD|소화전|PUMP|펌프|FP[-_]|ALARM|경보|"
    r"PANEL|제어반|PIPE|배관|MAIN|BRANCH|RISER",
    re.IGNORECASE,
)


def _infer_fire_category(layer: str, raw_type: str, block_name: str = "") -> str:
    text = f"{layer} {raw_type} {block_name}"
    for pattern, category in _FIRE_CAT_MAP:
        if pattern.search(text):
            return category
    return "unknown"


def _has_fire_signal(*values: Any) -> bool:
    text = " ".join(str(v or "") for v in values)
    return bool(_FIRE_SIGNAL_RE.search(text))


def _is_arch_only_layer(layer: str, *values: Any) -> bool:
    return bool(_ARCH_LAYER_RE.search(layer or "")) and not _has_fire_signal(layer, *values)


def _dedupe_heads_by_position(heads: list[dict]) -> list[dict]:
    """
    같은 물리 헤드가 BLOCK/심볼/속성 등으로 중복 추출되는 경우가 있다.
    중복을 제거하지 않으면 최근접 거리가 0mm가 되어 실제 간격 위반을 놓치므로,
    좌표가 거의 같은 헤드는 하나의 헤드로 병합한다.
    """
    unique: list[dict] = []
    for head in heads:
        pos = head.get("position") or {}
        try:
            x = float(pos.get("x") or 0)
            y = float(pos.get("y") or 0)
        except (TypeError, ValueError):
            unique.append(head)
            continue

        merged = False
        for existing in unique:
            epos = existing.get("position") or {}
            try:
                ex = float(epos.get("x") or 0)
                ey = float(epos.get("y") or 0)
            except (TypeError, ValueError):
                continue
            if math.hypot(x - ex, y - ey) <= _HEAD_DEDUP_TOL_MM:
                aliases = existing.setdefault("duplicate_handles", [])
                h = str(head.get("handle") or head.get("id") or "")
                if h:
                    aliases.append(h)
                merged = True
                break
        if not merged:
            unique.append(dict(head))
    return unique


def _compute_nearest_distances(elements: list, fire_category: str, limit_mm: float) -> dict:
    """
    지정 카테고리 설비들의 최근접 거리를 계산한다.
    각 설비의 가장 가까운 동종 설비 거리만 사용하여 위반 후보를 판정한다.
    결과는 compliance 프롬프트의 fire_topology.<category> 필드로 전달된다.
    """
    targets = [
        el for el in elements
        if isinstance(el, dict)
        and el.get("fire_category") == fire_category
        and isinstance(el.get("position"), dict)
    ]
    raw_count = len(targets)
    targets = _dedupe_heads_by_position(targets)
    if len(targets) < 2:
        return {
            "nearest_distances": [],
            "violation_candidates": [],
            "unique_count": len(targets),
            "raw_count": raw_count,
            "limit_mm": limit_mm,
        }

    nearest_by_id: dict[str, dict] = {}
    for i in range(len(targets)):
        for j in range(i + 1, len(targets)):
            pa = targets[i]["position"]
            pb = targets[j]["position"]
            dx = float(pa.get("x") or 0) - float(pb.get("x") or 0)
            dy = float(pa.get("y") or 0) - float(pb.get("y") or 0)
            dist = round(math.sqrt(dx * dx + dy * dy), 1)
            id_a = str(targets[i].get("handle") or targets[i].get("id") or "")
            id_b = str(targets[j].get("handle") or targets[j].get("id") or "")
            for eid, nearest_eid in ((id_a, id_b), (id_b, id_a)):
                current = nearest_by_id.get(eid)
                if current is None or dist < current["distance_mm"]:
                    nearest_by_id[eid] = {"head": eid, "nearest_head": nearest_eid, "distance_mm": dist}

    nearest_distances = sorted(
        nearest_by_id.values(),
        key=lambda x: x["distance_mm"],
        reverse=True,
    )
    violation_candidates = [
        {**item, "limit_mm": limit_mm}
        for item in nearest_distances
        if item["distance_mm"] > limit_mm
    ]
    return {
        "nearest_distances": nearest_distances,
        "violation_candidates": violation_candidates,
        "unique_count": len(targets),
        "raw_count": raw_count,
        "limit_mm": limit_mm,
    }


class ParserAgent:
    """
    도면 JSON → 소방 검토용 구조체 변환기.

    parse()   : 표준 인터페이스 (mapping_table 지원, {elements:[]} 반환)
    execute() : 레거시 인터페이스 (backward-compat 유지)
    """

    # ── 표준 인터페이스 ───────────────────────────────────────────────────────

    def parse(self, raw_layout: dict | str, mapping_table: dict | None = None) -> dict:
        """
        Parameters
        ----------
        raw_layout    : C# 형식 도면 JSON (str 또는 dict)
                        {entities: [{handle, type, layer, bbox, ...}, ...]}
        mapping_table : MappingAgent 출력 (term_map, entity_type_map)

        Returns
        -------
        {
          "elements": [
            {
              "id":                    str,   # TAG_NAME or handle
              "handle":                str,
              "type":                  str,   # 매핑 후 전문 용어
              "raw_type":              str,   # 원본 CAD 타입
              "layer":                 str,
              "fire_category":         str,   # sprinkler|detector|hydrant|pump|alarm|panel|pipe|unknown
              "position":              {"x": float, "y": float},
              "bbox":                  dict | None,
              "coverage_area_m2":      float,
              "installation_height_mm": float,
              "standard_type":         str | None,
              "attributes":            dict,
            },
            ...
          ]
        }
        """
        if isinstance(raw_layout, str):
            try:
                raw_layout = json.loads(raw_layout)
            except json.JSONDecodeError:
                logging.error("[FireParserAgent] JSON 파싱 실패 — 빈 elements 반환")
                return {"elements": []}

        if not isinstance(raw_layout, dict):
            return {"elements": []}

        entities = (
            raw_layout.get("entities")
            or raw_layout.get("elements")
            or []
        )
        term_map: dict[str, str]        = (mapping_table or {}).get("term_map", {})
        entity_type_map: dict[str, str] = (mapping_table or {}).get("entity_type_map", {})

        elements: list[dict[str, Any]] = []
        skipped_arch = 0
        for ent in entities:
            handle     = str(ent.get("handle") or ent.get("object_id") or "")
            raw_type   = str(ent.get("type") or "")
            layer      = str(ent.get("layer") or "")
            block_name = str(ent.get("block_name") or "")
            attrs      = ent.get("attributes") or ent.get("metadata") or {}

            resolved_type = (
                term_map.get(block_name)
                or term_map.get(layer)
                or entity_type_map.get(raw_type.upper())
                or raw_type
            )
            fire_category = _infer_fire_category(layer, raw_type, block_name)
            attr_text = " ".join(str(v or "") for v in attrs.values()) if isinstance(attrs, dict) else ""
            if fire_category == "unknown" and _is_arch_only_layer(
                layer,
                raw_type,
                block_name,
                resolved_type,
                ent.get("standard_type"),
                attr_text,
            ):
                skipped_arch += 1
                continue

            position = (
                ent.get("center")
                or ent.get("insert_point")
                or ent.get("position")
                or {"x": self._to_float(ent.get("x")), "y": self._to_float(ent.get("y"))}
            )

            elements.append({
                "id":                     str(attrs.get("TAG_NAME") or handle),
                "handle":                 handle,
                "type":                   resolved_type,
                "raw_type":               raw_type,
                "layer":                  layer,
                "fire_category":          fire_category,
                "position":               position,
                "bbox":                   ent.get("bbox"),
                "coverage_area_m2":       self._to_float(
                                              attrs.get("COVERAGE") or attrs.get("AREA")
                                          ),
                "installation_height_mm": self._to_float(
                                              attrs.get("HEIGHT") or attrs.get("INSTALL_HEIGHT")
                                          ),
                "standard_type":          ent.get("standard_type") or attrs.get("STANDARD_TYPE"),
                "attributes":             attrs,
            })

        if skipped_arch:
            logging.info(
                "[FireParserAgent] 건축 레이어 비소방 객체 %d건 제외: parsed_elements=%d",
                skipped_arch,
                len(elements),
            )
        logging.debug("[FireParserAgent] parse() elements=%d", len(elements))
        return {
            "elements": elements,
            "fire_topology": {
                "sprinkler": _compute_nearest_distances(elements, "sprinkler", 2300.0),
                "detector":  _compute_nearest_distances(elements, "detector",  4500.0),
                "hydrant":   _compute_nearest_distances(elements, "hydrant",  25000.0),
            },
        }

    # ── 레거시 인터페이스 (hasattr 어댑터 호환) ──────────────────────────────

    def execute(self, drawing_data: dict | str) -> dict:
        """
        레거시 호출 경로 — workflow_handler의 hasattr 어댑터가 parse()를 우선 사용하므로
        직접 호출되는 경우는 없지만 하위 호환을 위해 유지합니다.
        """
        if isinstance(drawing_data, str):
            try:
                drawing_data = json.loads(drawing_data)
            except json.JSONDecodeError:
                logging.error("[ParserAgent] 도면 데이터 파싱 실패: 유효하지 않은 JSON 형식")
                return {"error": "Invalid JSON", "parsed_entities": [], "total_count": 0}

        entities = drawing_data.get("entities", [])
        parsed_entities = []

        for entity in entities:
            parsed_entities.append({
                "id":            entity.get("handle") or entity.get("object_id"),
                "standard_type": entity.get("standard_type"),
                "x":             entity.get("x", 0.0),
                "y":             entity.get("y", 0.0),
                "layer":         entity.get("layer"),
                "metadata":      entity.get("metadata", {}),
            })

        return {
            "parsed_entities": parsed_entities,
            "total_count":     len(parsed_entities),
            "status":          "success",
        }

    # ── 다중 객체 매핑 (공통 유틸 사용) ─────────────────────────────────────────

    async def async_map_texts_to_blocks(
        self,
        text_entities: list[dict],
        block_entities: list[dict],
        *,
        ambiguity_threshold: float = 10.0,
    ) -> list[dict]:
        """
        (비동기) 텍스트 → 소방 블록 매핑 + 모호 케이스 LLM fallback.
        OOM 방지 전역 세마포어 및 label 필드 포함.

        Returns
        -------
        [{"text_handle", "block_handle", "label", "score", "method"}, ...]
        """
        return await _map_texts_to_blocks_util(
            text_entities,
            block_entities,
            domain_hint="소방",
            layer_bonus_config=None,   # 소방은 레이어 보너스 없음
            ambiguity_threshold=ambiguity_threshold,
        )

    # ── 헬퍼 ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _to_float(value: Any) -> float:
        try:
            return float(
                str(value)
                .replace("mm", "").replace("m²", "").replace("m2", "")
                .replace("m", "").strip()
            )
        except (ValueError, TypeError):
            return 0.0
