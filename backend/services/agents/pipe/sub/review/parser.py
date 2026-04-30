"""
File    : backend/services/agents/piping/sub/review/parser.py
Author  : 송주엽
Create  : 2026-04-09
Description : C# 클라이언트가 전송한 도면 JSON을 정규화하여
              ComplianceAgent가 소비할 수 있는 구조체로 변환합니다.

Modification History :
    - 2026-04-09 (송주엽) : 도면 JSON 정규화 파서 로직 작성
    - 2026-04-15 (송주엽) : C# 엔티티 형식(handle/bbox/center/insert_point) 완전 지원,
                            "entities"·"elements" 키 모두 허용,
                            diameter·position 다중 소스 파생,
                            equipment_id = TAG_NAME 우선 → handle 폴백
    - 2026-04-16 (송주엽) : 공통 다중객체 매핑 모듈(common.multi_object_mapper) 연동.
                            map_texts_to_blocks() / async_map_texts_to_blocks() 메서드 추가.
                            배관 도메인 LayerBonusConfig(L4/TEX) 적용.
    - 2026-04-23 : CAD_JSON_DEBUG 시 최상위 키·요소 샘플러(handle/type/attributes 키) INFO 로그
    - 2026-04-28 (송주엽) : TextAttributeExtractor 추가 (TEXT 엔티티에서 DN/재질/압력 추출)
                            도면층 기반 매핑 (LayerBasedScoringEngine) 통합.
                            map_texts_to_blocks_layer_enhanced() 메서드 추가.
"""

import json
import logging
import re
from typing import Any

from backend.core.config import settings

from backend.services.agents.common.multi_object_mapper import (
    MappingResult,
    find_best_match,
)
from backend.services.agents.common.object_mapping_utils import (
    map_texts_to_blocks as _map_texts_to_blocks_util,
    PIPE_LAYER_BONUS,
)
from backend.services.payload_service import extract_layers_json
from backend.services.agents.pipe.sub.mapping import LayerBasedScoringEngine

_PIPING_LAYER_BONUS = PIPE_LAYER_BONUS

# ── 배관 규정 검증과 무관한 엔티티 타입 / 레이어 / 블록명 제외 ────────────────────
# ① CAD 엔티티 타입 기준: DIMENSION, HATCH 등은 도면 주석이므로 배관 설비 아님
_SKIP_RAW_TYPES: frozenset[str] = frozenset({
    "DIMENSION", "HATCH", "VIEWPORT", "OLE2FRAME",
})

# ② 레이어 이름 기준: 치수·주석 전용 레이어 (레이어 이름이 "치수", "DIM" 등일 때)
#    ※ 같은 레이어에 치수/설비가 혼재하는 도면에서는 효과 없음 → ③④ 보완
_DIM_LAYER_RE = re.compile(
    r"치수|^DIM|DIMS?$|^ANNO|ANNOTATION|^TXT[-_]|^TEXT[-_]",
    re.IGNORECASE,
)

# ③ 블록 이름 기준: 치수 기호 블록, 화살표, 중심마크 등 비설비 블록
#    AutoCAD 표준 치수 심볼·사용자 치수 마크 블록 이름 패턴
_DIM_BLOCK_NAME_RE = re.compile(
    r"치수|DIMTICK|DIM[-_]|^TICK|^CROSS|^ARROW|^CENTER[-_]MARK|"
    r"^_OPEN|^_CLOSED|^_OBLIQUE|^_DOTSMALL|^_DOT|^_INTEGRAL|"
    r"^_ARCH|^_SMALL|ACAD_DIMSTYLE",
    re.IGNORECASE,
)


# ── TEXT 속성 추출기 ─────────────────────────────────────────────────────────

class TextAttributeExtractor:
    """
    TEXT / MTEXT / MLEADER 엔티티 문자열에서 배관 속성을 추출합니다.

    지원 패턴:
      DN50 / φ50 / Ø50    → diameter_mm
      SUS / ST / PVC 등   → material
      0.4MPa / 2atm       → pressure_mpa
      Q=150m³/h / 120LPM  → flow_rate_m3h
      85℃ / T=60          → temp_c
      v=2.0m/s            → velocity_ms
      @1500 / @2000mm     → hanger_spacing_mm
      FL+150 / EL.1200    → elevation_mm
    """

    DN_PATTERN = re.compile(
        r"DN\s*(\d+(?:\.\d+)?)"
        r"|φ\s*(\d+(?:\.\d+)?)"
        r"|Ø\s*(\d+(?:\.\d+)?)"
        r"|(?<!\w)(\d+(?:\.\d+)?)\s*[Aa]",
        re.IGNORECASE,
    )
    MATERIAL_PATTERN = re.compile(
        r"\b(SUS\s*\d*|ST(?:PG|PY|PW|K)?|PVC|CPVC|PP[-_]R|PE|동관|강관|"
        r"아연도금|스테인리스|비닐|흑관|백관|동관|HDPE|UPVC)\b",
        re.IGNORECASE,
    )
    PRESSURE_PATTERN = re.compile(
        r"(\d+(?:\.\d+)?)\s*MPa"
        r"|(\d+(?:\.\d+)?)\s*kPa"
        r"|(\d+(?:\.\d+)?)\s*atm",
        re.IGNORECASE,
    )
    FLOW_PATTERN = re.compile(
        r"[Qq]\s*[=:]?\s*(\d+(?:\.\d+)?)\s*m.?\/h"
        r"|(\d+(?:\.\d+)?)\s*(?:LPM|CMH)",
        re.IGNORECASE,
    )
    TEMP_PATTERN = re.compile(
        r"[Tt]\s*[=:]?\s*(\d+(?:\.\d+)?)[~\-]?\d*\s*(?:℃|°C)"
        r"|(\d+(?:\.\d+)?)\s*(?:℃|°C)",
        re.IGNORECASE,
    )
    VELOCITY_PATTERN = re.compile(r"[Vv]\s*[=:]?\s*(\d+(?:\.\d+)?)\s*m/s", re.IGNORECASE)
    HANGER_PATTERN   = re.compile(r"@\s*(\d+(?:\.\d+)?)\s*(?:mm)?(?!\d)", re.IGNORECASE)
    ELEVATION_PATTERN = re.compile(r"(?:FL|EL|GL)\s*[+.\-]\s*(\d+(?:\.\d+)?)", re.IGNORECASE)

    @classmethod
    def extract_from_text(cls, text: str) -> dict[str, Any]:
        """TEXT 문자열에서 배관 속성 추출."""
        if not text:
            return {}
        attrs: dict[str, Any] = {}

        dn_match = cls.DN_PATTERN.search(text)
        if dn_match:
            raw = next(g for g in dn_match.groups() if g is not None)
            attrs["diameter_mm"] = float(raw)

        mat_match = cls.MATERIAL_PATTERN.search(text)
        if mat_match:
            attrs["material"] = mat_match.group(1).strip()

        press_match = cls.PRESSURE_PATTERN.search(text)
        if press_match:
            if press_match.group(1):
                attrs["pressure_mpa"] = float(press_match.group(1))
            elif press_match.group(2):
                attrs["pressure_mpa"] = round(float(press_match.group(2)) / 1000.0, 6)
            elif press_match.group(3):
                attrs["pressure_mpa"] = round(float(press_match.group(3)) * 0.101325, 6)

        flow_match = cls.FLOW_PATTERN.search(text)
        if flow_match:
            raw_f = next(g for g in flow_match.groups() if g is not None)
            attrs["flow_rate_m3h"] = float(raw_f)

        temp_match = cls.TEMP_PATTERN.search(text)
        if temp_match:
            raw_t = next(g for g in temp_match.groups() if g is not None)
            attrs["temp_c"] = float(raw_t)

        vel_match = cls.VELOCITY_PATTERN.search(text)
        if vel_match:
            attrs["velocity_ms"] = float(vel_match.group(1))

        hanger_match = cls.HANGER_PATTERN.search(text)
        if hanger_match:
            attrs["hanger_spacing_mm"] = float(hanger_match.group(1))

        elev_match = cls.ELEVATION_PATTERN.search(text)
        if elev_match:
            attrs["elevation_mm"] = float(elev_match.group(1))

        return attrs

    @classmethod
    def enrich_element(cls, element: dict, extracted: dict[str, Any]) -> dict:
        """
        추출된 속성을 element에 병합. 기존 값이 있는 필드(overwrite 대상)는 덮어쓰지 않음.
        신규 확장 필드(additive)는 element에 없으면 그냥 추가.
        """
        changed = False
        _OVERWRITE = {"diameter_mm", "material", "pressure_mpa"}
        _ADDITIVE  = {"flow_rate_m3h", "temp_c", "velocity_ms",
                      "hanger_spacing_mm", "elevation_mm"}
        for key in _OVERWRITE:
            existing = element.get(key)
            if key in extracted and (not existing or existing == 0 or existing == "UNKNOWN"):
                element[key] = extracted[key]
                changed = True
        for key in _ADDITIVE:
            if key in extracted and key not in element:
                element[key] = extracted[key]
                changed = True
        if changed:
            element["source_attributes"] = "text_extracted"
        return element


# ── 메인 파서 클래스 ─────────────────────────────────────────────────────────

class ParserAgent:
    """
    C# → Python 방향으로 전달되는 도면 JSON을 ComplianceAgent 입력 형식으로 정규화합니다.

    C# 엔티티 공통 필드: handle, type, layer, bbox, color, linetype, lineweight
    타입별 추가 필드:
      LINE     : start, end, length, angle
      CIRCLE   : center, radius, diameter
      ARC      : center, radius, start_angle, end_angle
      POLYLINE : vertices[], is_closed, perimeter, area
      BLOCK    : block_name, insert_point, rotation, scale_x/y/z, attributes{}
      MTEXT    : text, insert_point, text_height
      TEXT     : text, insert_point, text_height
      DIMENSION: measurement, dim_type, xline1_point, xline2_point
      HATCH    : pattern_name, hatch_area, boundary_count
      SOLID    : corner1..4
      MLEADER  : text, start, end
      SPLINE   : fit_points[], is_closed
      ELLIPSE  : center, major_axis, minor_ratio
    """

    # C# 엔티티 최소 필수 필드 (handle + type 만 요구)
    _REQUIRED = {"handle", "type"}

    def _item_to_parsed_element(
        self,
        item: Any,
        term_map: dict[str, str],
        entity_type_map: dict[str, str],
        layer_resolved_roles: dict[str, str] | None = None,
        *,
        arch_context: bool = False,
    ) -> dict | None:
        if not isinstance(item, dict) or not self._REQUIRED.issubset(item.keys()):
            return None
        handle = str(item["handle"])
        raw_type = str(item["type"])
        layer = str(item.get("layer", ""))

        promoted_for_pipe = bool(item.get("flag_for_piping_agent")) or str(item.get("layer_role") or "").lower() == "mep"

        # ⓪ 통계 기반 레이어 역할 필터링 (arch인 경우 즉시 제외)
        # 단, L3/L4처럼 레이어 전체는 arch/unknown이어도 유색 배관선·배관 주석으로 승격된 객체는 보존한다.
        if (
            not arch_context
            and layer_resolved_roles
            and layer_resolved_roles.get(layer) == "arch"
            and not promoted_for_pipe
        ):
            if settings.CAD_JSON_DEBUG:
                logging.info("[Parser Debug] Skip arch layer entity: handle=%s layer=%s", handle, layer)
            return None

        # ① 엔티티 타입 기준 제외: DIMENSION, HATCH 등
        if raw_type.upper() in _SKIP_RAW_TYPES:
            return None
            
        # ② 치수/주석 전용 레이어 제외 (TEX는 배관 주석이므로 제외 대상에서 명시적 보호)
        if layer:
            if layer.upper() == "TEX":
                # TEX 레이어는 무조건 보존
                pass
            elif _DIM_LAYER_RE.search(layer):
                return None

        attrs = item.get("attributes") or {}
        equipment_id = str(attrs.get("TAG_NAME") or handle)
        block_name = str(item.get("block_name") or "")

        # ③ 블록 이름 기준 제외: 치수 기호·화살표·중심마크 등 비설비 블록
        if block_name and _DIM_BLOCK_NAME_RE.search(block_name):
            return None

        # ④ 데이터 내용 기준 제외: 같은 레이어에 설비·치수가 혼재하는 도면 대응
        #    BLOCK/INSERT 인데 배관 속성이 전혀 없고, MappingAgent term_map에도 없으면
        #    도면 심볼(치수기준점, 방위표, 범례기호 등)으로 판단하여 제외한다.
        #
        #    ※ 이 판정은 term_map이 로드된 이후에만 신뢰할 수 있다.
        #      term_map이 비어 있으면 block_name 자체가 없는 경우만 제외(보수적).
        if not arch_context and raw_type.upper() in {"BLOCK", "INSERT", "POINT"}:
            has_pipe_attr = bool(
                attrs.get("TAG_NAME")
                or attrs.get("SIZE")
                or attrs.get("DIAMETER")
                or attrs.get("MATERIAL")
                or attrs.get("PRESSURE")
                or attrs.get("SLOPE")
                or item.get("diameter")
                or item.get("pressure")
            )
            if not has_pipe_attr:
                # term_map이 채워진 경우: 블록 이름이 매핑 테이블에 있어야 설비로 인정
                if term_map:
                    if not term_map.get(block_name):
                        return None   # 알 수 없는 블록 + 배관 속성 없음 → 심볼
                else:
                    # term_map 없음(매핑 미완): 블록 이름 자체가 없을 때만 제외(보수적)
                    if not block_name:
                        return None
        resolved_type = (
            term_map.get(block_name)
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
        diameter_mm = (
            self._to_float(item.get("diameter"))
            or self._to_float(item.get("radius", 0)) * 2
            or self._parse_dn_size(attrs.get("SIZE", ""))
        )
        # CIRCLE 단면: radius >= 2.5mm 이면 배관 단면으로 간주
        if raw_type.upper() == "CIRCLE" and not diameter_mm:
            r = self._to_float(item.get("radius", 0))
            if r >= 2.5:
                diameter_mm = r * 2

        pressure_mpa = self._to_float(attrs.get("PRESSURE") or item.get("pressure", 0))
        slope_pct    = self._to_float(attrs.get("SLOPE") or item.get("slope", 0))
        material     = str(
            item.get("material")
            or attrs.get("MATERIAL")
            or attrs.get("MAT")
            or "UNKNOWN"
        )

        # ── 신규: 도면 표현 필드 ──────────────────────────────────────────────
        color      = item.get("color")                  # ACI int or RGB dict
        linetype   = str(item.get("linetype") or "")   # CONTINUOUS/HIDDEN/DASHDOT 등
        lineweight = item.get("lineweight")             # 100 = 1.0mm

        out_el: dict[str, Any] = {
            "id":           equipment_id,
            "handle":       handle,
            "type":         resolved_type,
            "raw_type":     raw_type,
            "layer":        layer,
            "position":     position,
            "diameter_mm":  diameter_mm,
            "pressure_mpa": pressure_mpa,
            "slope_pct":    slope_pct,
            "material":     material,
            "attributes":   attrs,
            # 신규 필드
            "color":        color,
            "linetype":     linetype if linetype else None,
            "lineweight":   lineweight,
        }

        # ── 타입별 추가 필드 ──────────────────────────────────────────────────
        raw_upper = raw_type.upper()

        # LINE / ARC: 끝점 + 길이 + 각도(방향)
        if raw_upper in ("LINE", "ARC"):
            if item.get("start"):  out_el["start"] = item["start"]
            if item.get("end"):    out_el["end"]   = item["end"]
            if item.get("length") is not None:
                out_el["length"] = item["length"]
            angle = item.get("angle")
            if angle is not None:
                out_el["angle_deg"] = round(float(angle), 2)  # 0°=수평

        # POLYLINE / LWPOLYLINE: 꼭짓점 + 폐합 여부 + 면적
        elif raw_upper in ("POLYLINE", "LWPOLYLINE"):
            if item.get("vertices"):   out_el["vertices"] = item["vertices"]
            if item.get("length") is not None:
                out_el["length"] = item["length"]
            is_closed = item.get("is_closed")
            if is_closed is not None:
                out_el["is_closed"] = bool(is_closed)
            area = item.get("area")
            if area is not None:
                out_el["area"] = round(float(area), 4)

        # CIRCLE: 중심 + 반지름 (배관 단면)
        elif raw_upper == "CIRCLE":
            if item.get("center"): out_el["center"] = item["center"]
            r_val = self._to_float(item.get("radius", 0))
            if r_val:  out_el["radius"] = r_val

        # BLOCK / INSERT: 회전 + 스케일
        elif raw_upper in ("BLOCK", "INSERT"):
            rotation = item.get("rotation")
            if rotation is not None:
                out_el["rotation_deg"] = round(float(rotation), 2)
            sx = item.get("scale_x"); sy = item.get("scale_y"); sz = item.get("scale_z")
            if sx is not None or sy is not None:
                out_el["scale"] = {
                    "x": round(float(sx or 1.0), 4),
                    "y": round(float(sy or 1.0), 4),
                    "z": round(float(sz or 1.0), 4),
                }

        # SPLINE: 피트포인트 수 (곡선 배관)
        elif raw_upper == "SPLINE":
            fps = item.get("fit_points") or []
            out_el["fit_points_count"] = len(fps)
            if item.get("vertices"):   out_el["vertices"] = item["vertices"]
            is_closed = item.get("is_closed")
            if is_closed is not None:  out_el["is_closed"] = bool(is_closed)

        # TEXT / MTEXT: keep annotation content for topology and attribute hints.
        elif raw_upper in ("TEXT", "MTEXT"):
            text_value = str(item.get("text") or item.get("content") or "")
            if text_value:
                out_el["text"] = text_value
                extracted = TextAttributeExtractor.extract_from_text(text_value)
                if extracted:
                    TextAttributeExtractor.enrich_element(out_el, extracted)
            if item.get("text_height") is not None:
                out_el["text_height"] = self._to_float(item.get("text_height"))
            rotation = item.get("rotation")
            if rotation is not None:
                out_el["rotation_deg"] = round(float(rotation), 2)

        # MLEADER: 인출선 주석 → TextAttributeExtractor 직접 적용
        elif raw_upper == "MLEADER":
            ml_text = str(item.get("text") or item.get("content") or "")
            if ml_text:
                out_el["text"] = ml_text
                extracted = TextAttributeExtractor.extract_from_text(ml_text)
                if extracted:
                    TextAttributeExtractor.enrich_element(out_el, extracted)

        lr = item.get("layer_role")
        if lr is not None:
            out_el["layer_role"] = lr
        if item.get("flag_for_piping_agent") is not None:
            out_el["flag_for_piping_agent"] = bool(item.get("flag_for_piping_agent"))
        if item.get("source_layer_role") is not None:
            out_el["source_layer_role"] = str(item.get("source_layer_role") or "")
        if item.get("entity_mep_score") is not None:
            out_el["entity_mep_score"] = self._to_float(item.get("entity_mep_score"))
        if item.get("entity_mep_indicator") is not None:
            out_el["entity_mep_indicator"] = str(item.get("entity_mep_indicator") or "")
        if item.get("metadata_role") is not None:
            out_el["metadata_role"] = str(item.get("metadata_role") or "")
        if arch_context:
            out_el["layer_role"] = "arch"
        return out_el

    def parse(
        self,
        layout_data: str | dict,
        mapping_table: dict | None = None,
        layer_resolved_roles: dict | None = None,
    ) -> dict:
        """
        원시 도면 데이터를 받아 정규화된 요소 목록을 반환합니다.

        입력:
          layout_data  : C# 형식 도면 JSON (str 또는 dict)
          mapping_table: MappingAgent 출력 — term_map, style_map, entity_type_map 포함
          layer_resolved_roles: (신규) 통계 기반 레이어 역할 맵 (L4, L3 등 모든 레이어 대응)

        반환:
          {
            "elements": [...],
            "layers_json": {...},
            "arch_elements": [...],
            "layer_role_stats": {...},
            ...
          }
        """
        try:
            data = json.loads(layout_data) if isinstance(layout_data, str) else layout_data
        except (json.JSONDecodeError, TypeError) as e:
            if settings.CAD_JSON_DEBUG:
                logging.getLogger(__name__).info(
                    "[ParserAgent] JSON decode/type error: %s input_type=%s",
                    e,
                    type(layout_data).__name__,
                )
            return {"elements": [], "error": "layout_data 파싱 실패"}

        term_map: dict[str, str] = (mapping_table or {}).get("term_map", {})
        entity_type_map: dict[str, str] = (mapping_table or {}).get("entity_type_map", {})

        # 건축/배관 분리 스키마(pipe_split_v1): mep_review·arch_reference
        arch_raw: list = []
        layer_role_stats = None
        if isinstance(data, dict):
            layer_role_stats = data.get("layer_role_stats")
            if isinstance(data.get("mep_review"), dict):
                raw_entities = data["mep_review"].get("entities") or []
            else:
                raw_entities = data.get("entities") or data.get("elements") or []
            if isinstance(data.get("arch_reference"), dict):
                arch_raw = data["arch_reference"].get("entities") or []
        else:
            raw_entities = []

        if not raw_entities:
            raw_entities = data.get("entities") or data.get("elements") or [] if isinstance(data, dict) else []

        _log = logging.getLogger(__name__)
        if settings.CAD_JSON_DEBUG:
            _log.info(
                "[ParserAgent] layout top-level keys=%s mep_entities=%s arch_entities=%s",
                list(data.keys()) if isinstance(data, dict) else type(data).__name__,
                len(raw_entities),
                len(arch_raw),
            )

        parsed: list[dict] = []
        skipped_no_required = 0
        skipped_dim_or_hatch = 0
        for item in raw_entities:
            # 치수/해칭 여부 미리 확인 (로그용)
            _raw_t  = str(item.get("type", "")).upper()       if isinstance(item, dict) else ""
            _lyr    = str(item.get("layer", ""))              if isinstance(item, dict) else ""
            _blk    = str(item.get("block_name") or "")       if isinstance(item, dict) else ""
            _is_dim = (
                _raw_t in _SKIP_RAW_TYPES
                or bool(_lyr and _DIM_LAYER_RE.search(_lyr))
                or bool(_blk and _DIM_BLOCK_NAME_RE.search(_blk))
            )

            el = self._item_to_parsed_element(
                item, term_map, entity_type_map, layer_resolved_roles=layer_resolved_roles
            )
            if el is None:
                if _is_dim:
                    skipped_dim_or_hatch += 1
                else:
                    skipped_no_required += 1
                if (
                    settings.CAD_JSON_DEBUG
                    and isinstance(item, dict)
                    and not self._REQUIRED.issubset(item.keys())
                    and not _is_dim
                ):
                    _log.info(
                        "[ParserAgent] skip entity (need handle+type) keys=%s",
                        list(item.keys())[:24],
                    )
                continue
            parsed.append(el)

        arch_parsed: list[dict] = []
        for item in arch_raw:
            el = self._item_to_parsed_element(
                item,
                term_map,
                entity_type_map,
                layer_resolved_roles=layer_resolved_roles,
                arch_context=True,
            )
            if el is not None:
                arch_parsed.append(el)

        _log.info(
            "[ParserAgent] parsed=%s arch_parsed=%s skipped_missing=%s skipped_dim/hatch=%s",
            len(parsed),
            len(arch_parsed),
            skipped_no_required,
            skipped_dim_or_hatch,
        )
        if settings.CAD_JSON_DEBUG:
            for i, el in enumerate(parsed[:3]):
                ak = list((el.get("attributes") or {}).keys())[:16]
                _log.info(
                    "[ParserAgent] element[%s] id=%r handle=%r type=%r layer=%r pos=%s mat=%r attr_keys=%s",
                    i,
                    el.get("id"),
                    el.get("handle"),
                    el.get("type"),
                    el.get("layer"),
                    el.get("position"),
                    el.get("material"),
                    ak,
                )

        # ── 도면층 구조화 (extract_layers_json) ─────────────────────────────
        layers_json: dict[str, Any] = {}
        if isinstance(data, dict) and data.get("layers"):
            layers_json = extract_layers_json(data)

        out: dict[str, Any] = {"elements": parsed}
        if layers_json:
            out["layers_json"] = layers_json
        if arch_parsed:
            out["arch_elements"] = arch_parsed
        if layer_role_stats is not None:
            out["layer_role_stats"] = layer_role_stats
        if isinstance(data, dict):
            lr = data.get("layer_roles")
            if isinstance(lr, dict) and lr:
                out["layer_roles"] = lr
            li = data.get("layers_indexed")
            if isinstance(li, list) and li:
                out["layers_indexed"] = li
            sp = data.get("spatial_hints")
            if isinstance(sp, dict) and sp:
                out["spatial_hints"] = sp
        return out

    # ── 다중 객체 매핑 (공통 모듈 활용) ─────────────────────────────────────

    def map_texts_to_blocks(
        self,
        text_entities: list[dict],
        block_entities: list[dict],
        *,
        ambiguity_threshold: float = 10.0,
    ) -> list[MappingResult]:
        """
        (동기) 텍스트 엔티티 목록과 블록 엔티티 목록을 1:1 매핑합니다.

        모호한 경우(점수 차 ≤ ambiguity_threshold)는 result.is_ambiguous=True 로
        표시됩니다. 즉시 LLM fallback 이 필요하면 async_map_texts_to_blocks 를
        사용하세요.

        Returns:
            list[MappingResult]  — 텍스트 엔티티별 매핑 결과
        """
        results: list[MappingResult] = []
        for text_ent in text_entities:
            result = find_best_match(
                text_ent,
                block_entities,
                ambiguity_threshold=ambiguity_threshold,
                score_kwargs={"layer_bonus_config": _PIPING_LAYER_BONUS},
            )
            results.append(result)
        return results

    async def async_map_texts_to_blocks(
        self,
        text_entities: list[dict],
        block_entities: list[dict],
        *,
        ambiguity_threshold: float = 10.0,
    ) -> list[dict]:
        """
        (비동기) 텍스트-블록 자동 매핑 + 모호 케이스 LLM fallback.
        OOM 방지 전역 세마포어 및 label 필드 포함.

        Returns:
            [{"text_handle", "block_handle", "label", "score", "method"}, ...]
        """
        return await _map_texts_to_blocks_util(
            text_entities,
            block_entities,
            domain_hint="배관",
            layer_bonus_config=_PIPING_LAYER_BONUS,
            ambiguity_threshold=ambiguity_threshold,
        )

    async def map_texts_to_blocks_layer_enhanced(
        self,
        text_entities: list[dict],
        block_entities: list[dict],
        layers_json: dict[str, Any],
        elements_by_handle: dict[str, dict],
        *,
        ambiguity_threshold: float = 10.0,
    ) -> dict[str, Any]:
        """
        (비동기) 도면층 기반 가중치 스코어링 + TEXT 속성 추론 통합 매핑.

        1. async_map_texts_to_blocks()로 기본 매핑 수행
        2. LayerBasedScoringEngine으로 도면층 내 점수 보정
        3. TextAttributeExtractor로 TEXT에서 속성 추출 → 블록 element 보강
        4. compute_inter_layer_constraints()로 레이어 간 관계 분석

        Returns:
            {
              "mappings": [...],                  # 보강된 매핑 목록
              "inter_layer_constraints": [...],   # 레이어 간 관계
              "enriched_count": int,              # 속성 보강된 element 수
            }
        """
        _log = logging.getLogger(__name__)

        # [1] 기본 매핑
        base_mappings: list[dict] = await self.async_map_texts_to_blocks(
            text_entities,
            block_entities,
            ambiguity_threshold=ambiguity_threshold,
        )

        # 레이어 인덱스 (이름 → layer_info)
        layer_index: dict[str, dict] = {
            l["name"]: l for l in (layers_json.get("layers") or [])
        }
        scorer = LayerBasedScoringEngine()

        enhanced_mappings: list[dict] = []
        enriched_count = 0

        for mapping in base_mappings:
            text_ent  = mapping.get("text_entity") or {}
            block_ent = mapping.get("block_entity") or {}

            # [2] 도면층 기반 점수 보정
            text_layer = str(text_ent.get("layer") or "")
            layer_info = layer_index.get(text_layer, {})

            if layer_info:
                intra = scorer.compute_intra_layer_mapping_score(
                    text_ent,
                    block_ent,
                    layer_info,
                    base_score=float(mapping.get("score") or 0.0),
                )
                mapping = dict(mapping)
                mapping["layer_based_score"]  = intra["score"]
                mapping["layer_score_reason"] = intra["reason"]

            # [3] TEXT 속성 추출 → 블록 element 보강
            text_str = str(text_ent.get("text") or "")
            if text_str:
                extracted = TextAttributeExtractor.extract_from_text(text_str)
                if extracted:
                    block_handle = str(block_ent.get("handle") or "")
                    if block_handle and block_handle in elements_by_handle:
                        before = dict(elements_by_handle[block_handle])
                        TextAttributeExtractor.enrich_element(
                            elements_by_handle[block_handle], extracted
                        )
                        if elements_by_handle[block_handle] != before:
                            enriched_count += 1

            enhanced_mappings.append(mapping)

        # [4] 레이어 간 관계 분석
        inter_layer_constraints = scorer.compute_inter_layer_constraints(
            layers_json.get("layers") or [],
            enhanced_mappings,
        )

        _log.info(
            "[ParserAgent] layer_enhanced_mapping mappings=%s enriched=%s inter_constraints=%s",
            len(enhanced_mappings),
            enriched_count,
            len(inter_layer_constraints),
        )

        return {
            "mappings":               enhanced_mappings,
            "inter_layer_constraints": inter_layer_constraints,
            "enriched_count":         enriched_count,
        }

    # ── 정적 헬퍼 ────────────────────────────────────────────────────────────

    @staticmethod
    def _bbox_center(bbox: dict | None) -> dict | None:
        """bbox {x1, y1, x2, y2} → 중심 좌표"""
        if not isinstance(bbox, dict):
            return None
        try:
            return {
                "x": (float(bbox["x1"]) + float(bbox["x2"])) / 2,
                "y": (float(bbox["y1"]) + float(bbox["y2"])) / 2,
            }
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _to_float(value: Any) -> float:
        try:
            return float(
                str(value)
                .replace("mm", "").replace("MPa", "").replace("%", "")
                .replace("DN", "").replace("A", "").strip()
            )
        except (ValueError, TypeError):
            return 0.0

    @staticmethod
    def _parse_dn_size(size_str: str) -> float:
        """'DN50' → 50.0 / '50A' → 50.0 / '50' → 50.0"""
        m = re.search(r"(\d+(?:\.\d+)?)", str(size_str))
        return float(m.group(1)) if m else 0.0
