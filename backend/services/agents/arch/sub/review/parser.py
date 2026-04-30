"""
File    : backend/services/agents/arch/sub/review/parser.py
Author  : 김다빈
WBS     : AI-05 (건축 도메인 에이전트)
Create  : 2026-04-15
Modified: 2026-04-24 (Phase 8 — elements 플랫 리스트 추가, multi_object_mapper 텍스트→지오메트리 연동)

Description :
    CadDrawingData JSON(entities 배열)을 건축 검토용 구조체로 변환.
    CadDataExtractor가 출력한 handle/type/layer/bbox 기반 필드를 그대로 사용.

    지원 엔티티 타입:
        walls       — LWPOLYLINE/POLYLINE (constant_width > 0 또는 WALL 레이어)
                      LINE (WALL 레이어 또는 길이 기반 추정)
        columns     — CIRCLE (소형 원 → 기둥 추정)
        curved_walls— ARC (곡선 벽체 또는 구조 요소)
        spaces      — HATCH (hatch_area 보유)
                      ELLIPSE (면적 보유 시 공간 윤곽)
        openings    — INSERT (door/window/stair 블록명 패턴)
                      ARC (도어스윙 패턴: 반원 또는 사분원)
        dimensions  — DIMENSION (measurement 보유)
        annotations — MTEXT / TEXT

    drawing_unit 변환:
        mm(기본) 기준으로 통일. cm×10, m×1000, inch×25.4, feet×304.8.

Modification History :
    - 2026-04-15 (김다빈) : 초기 구현
    - 2026-04-15 (김다빈) : LINE/ARC/CIRCLE/ELLIPSE 처리 추가, 도어스윙 감지
    - 2026-04-24 (Phase 8) : parse() 반환값에 elements 플랫 리스트 추가
                             (ArchTopologyBuilder/GeometryPreprocessor 호환),
                             map_texts_to_blocks() / async_map_texts_to_blocks() 추가
                             (건축 레이어 보너스 A-ANNO/A-AREA 적용)
"""

import math
import re
from typing import Any

from backend.services.agents.common.multi_object_mapper import (
    LayerBonusConfig,
    MappingResult,
    find_best_match,
)
from backend.services.agents.common.object_mapping_utils import (
    map_texts_to_blocks as _map_texts_to_blocks_util,
    ARCH_LAYER_BONUS,
)

# 건축 도메인 레이어 보너스 — object_mapping_utils.ARCH_LAYER_BONUS 와 동일 (호환용 alias)
_ARCH_TEXT_BONUS = ARCH_LAYER_BONUS

# ── 단위 변환 계수 ──────────────────────────────────────────────────────────
_UNIT_TO_MM: dict[str, float] = {
    "mm":      1.0,
    "cm":      10.0,
    "m":       1000.0,
    "inch":    25.4,
    "feet":    304.8,
    "unknown": 1.0,
}

# ── 레이어명 패턴 ───────────────────────────────────────────────────────────
_WALL_LAYER   = re.compile(r"(wall|벽|WALL|W[-_]|구조|structural|section)", re.IGNORECASE)
_DOOR_LAYER   = re.compile(r"(door|문|도어|dr[-_]?|opening)", re.IGNORECASE)
_WIN_LAYER    = re.compile(r"(window|창문|창호|win[-_]?|wd[-_]?|glass|글라스)", re.IGNORECASE)
_STAIR_LAYER  = re.compile(r"(stair|계단|st[-_]?)", re.IGNORECASE)
_COL_LAYER    = re.compile(r"(column|기둥|col[-_]?|pillar)", re.IGNORECASE)
_SPACE_LAYER  = re.compile(r"(hatch|해치|room|space|area|공간)", re.IGNORECASE)

# ── 블록명 패턴 (INSERT) ────────────────────────────────────────────────────
_DOOR_BLOCK   = re.compile(r"(door|문|도어|dr[-_]?)", re.IGNORECASE)
_WIN_BLOCK    = re.compile(r"(window|창문|창호|win[-_]?|wd[-_]?)", re.IGNORECASE)
_STAIR_BLOCK  = re.compile(r"(stair|계단|st[-_]?)", re.IGNORECASE)

# 도어스윙 판별: ARC 각도 범위가 ~90° 또는 ~180°인 경우
_DOOR_SWING_ANGLES = {90.0, 180.0}
_DOOR_SWING_TOL    = 15.0   # ±허용 오차(°)

# 기둥 판별: CIRCLE 반지름 상한 (mm 기준, 단위 변환 후)
_COLUMN_RADIUS_MAX_MM = 500.0   # 반지름 500mm 이하 → 기둥 추정


def _to_mm(value: float | None, factor: float) -> float | None:
    if value is None:
        return None
    return value * factor


def _arc_span(start_angle: float, end_angle: float) -> float:
    """ARC의 각도 범위(°) 계산. end < start이면 360° wrap-around 처리."""
    span = end_angle - start_angle
    if span <= 0:
        span += 360.0
    return span


def _is_door_swing(ent: dict, factor: float) -> bool:
    """ARC가 도어스윙 패턴인지 판별.
    판별 기준:
      1. 레이어명이 DOOR 패턴
      2. 각도 범위가 90° 또는 180° (±15° 허용)
      3. 반지름이 일반적인 문 폭 범위 (500~1500mm 변환 후)
    """
    layer = ent.get("layer", "")
    if _DOOR_LAYER.search(layer):
        return True

    radius_raw = ent.get("radius") or 0
    radius_mm  = radius_raw * factor
    start = ent.get("start_angle") or 0
    end   = ent.get("end_angle")   or 0
    if not end:
        return False

    span = _arc_span(start, end)
    for target in _DOOR_SWING_ANGLES:
        if abs(span - target) <= _DOOR_SWING_TOL:
            # 반지름이 문 폭 범위(500~1500mm)이면 도어스윙으로 확정
            if 400 <= radius_mm <= 1800:
                return True

    return False


class ArchParserAgent:
    """
    CadDrawingData JSON → ArchDrawingContext 변환기.
    ComplianceAgent가 소비할 수 있는 범주별 엔티티 목록을 반환합니다.

    처리 가능한 모든 엔티티를 최대한 분류하여 정보 손실을 최소화합니다.
    """

    def parse(self, drawing_data: dict[str, Any]) -> dict[str, Any]:
        """
        Parameters
        ----------
        drawing_data : CadDrawingData JSON dict
            {drawing_unit, layer_count, entity_count, layers, entities}

        Returns
        -------
        ArchDrawingContext dict:
            {drawing_unit, unit_factor,
             walls, columns, curved_walls,
             spaces, openings, dimensions, annotations,
             unclassified, raw_entity_count}
        """
        unit   = drawing_data.get("drawing_unit", "unknown")
        factor = _UNIT_TO_MM.get(unit, 1.0)
        entities: list[dict] = drawing_data.get("entities", [])

        walls:        list[dict] = []
        columns:      list[dict] = []
        curved_walls: list[dict] = []
        spaces:       list[dict] = []
        openings:     list[dict] = []
        dimensions:   list[dict] = []
        annotations:  list[dict] = []
        unclassified: list[dict] = []

        for ent in entities:
            etype  = (ent.get("type") or "").upper()
            layer  = ent.get("layer") or ""
            handle = ent.get("handle") or ""
            bbox   = ent.get("bbox")

            # ── LWPOLYLINE / POLYLINE ──────────────────────────────────────
            if etype in ("LWPOLYLINE", "POLYLINE", "2DPOLYLINE"):
                cw      = ent.get("constant_width") or 0.0
                area    = ent.get("area")
                is_wall = bool((cw and cw > 0) or _WALL_LAYER.search(layer))

                if is_wall:
                    walls.append({
                        "handle":       handle,
                        "layer":        layer,
                        "bbox":         bbox,
                        "thickness_mm": _to_mm(cw or None, factor),
                        "length_mm":    _to_mm(ent.get("perimeter") or ent.get("length"), factor),
                        "area_mm2":     _to_mm(area, factor) if area else None,
                        "is_closed":    ent.get("is_closed", False),
                        "source":       "polyline",
                    })
                elif ent.get("is_closed") and area:
                    spaces.append({
                        "handle":  handle,
                        "layer":   layer,
                        "bbox":    bbox,
                        "area_mm2": _to_mm(area, factor),
                        "source":  "polyline",
                    })
                else:
                    unclassified.append({"handle": handle, "type": etype, "layer": layer, "bbox": bbox})

            # ── LINE ───────────────────────────────────────────────────────
            elif etype == "LINE":
                length_mm = _to_mm(ent.get("length"), factor)
                is_wall   = bool(_WALL_LAYER.search(layer))

                entry = {
                    "handle":    handle,
                    "layer":     layer,
                    "bbox":      bbox,
                    "length_mm": length_mm,
                    "start":     ent.get("start"),
                    "end":       ent.get("end"),
                    "angle":     ent.get("angle"),
                    "source":    "line",
                }

                if is_wall:
                    walls.append({**entry, "thickness_mm": None})
                else:
                    # 레이어 불명 LINE → unclassified로 보내되
                    # ComplianceAgent가 길이 정보 참고 가능하도록 보존
                    unclassified.append({**entry, "type": "LINE"})

            # ── ARC ────────────────────────────────────────────────────────
            elif etype == "ARC":
                radius_mm = _to_mm(ent.get("radius"), factor)
                span      = _arc_span(
                    ent.get("start_angle") or 0,
                    ent.get("end_angle")   or 0,
                )

                if _is_door_swing(ent, factor):
                    openings.append({
                        "handle":     handle,
                        "layer":      layer,
                        "bbox":       bbox,
                        "category":   "door",
                        "radius_mm":  radius_mm,
                        "span_deg":   span,
                        "source":     "arc_swing",
                    })
                elif _WALL_LAYER.search(layer):
                    curved_walls.append({
                        "handle":       handle,
                        "layer":        layer,
                        "bbox":         bbox,
                        "radius_mm":    radius_mm,
                        "span_deg":     span,
                        "arc_length_mm": _to_mm(ent.get("arc_length"), factor),
                        "source":       "arc",
                    })
                else:
                    unclassified.append({
                        "handle": handle, "type": "ARC",
                        "layer": layer, "bbox": bbox,
                        "radius_mm": radius_mm, "span_deg": span,
                    })

            # ── CIRCLE ─────────────────────────────────────────────────────
            elif etype == "CIRCLE":
                radius_mm = _to_mm(ent.get("radius"), factor) or 0.0

                if _COL_LAYER.search(layer) or radius_mm <= _COLUMN_RADIUS_MAX_MM:
                    columns.append({
                        "handle":    handle,
                        "layer":     layer,
                        "bbox":      bbox,
                        "radius_mm": radius_mm,
                        "center":    ent.get("center"),
                        "source":    "circle",
                    })
                else:
                    unclassified.append({
                        "handle": handle, "type": "CIRCLE",
                        "layer": layer, "bbox": bbox, "radius_mm": radius_mm,
                    })

            # ── HATCH ──────────────────────────────────────────────────────
            elif etype == "HATCH":
                ha = ent.get("hatch_area")
                if ha:
                    spaces.append({
                        "handle":        handle,
                        "layer":         layer,
                        "bbox":          bbox,
                        "area_mm2":      _to_mm(ha, factor),
                        "pattern_name":  ent.get("pattern_name"),
                        "source":        "hatch",
                    })
                else:
                    unclassified.append({"handle": handle, "type": "HATCH", "layer": layer, "bbox": bbox})

            # ── ELLIPSE ────────────────────────────────────────────────────
            elif etype == "ELLIPSE":
                # 타원: major_axis 길이로 대략적 크기 파악
                major = ent.get("major_axis") or {}
                major_len_mm = _to_mm(
                    math.hypot(major.get("x", 0), major.get("y", 0)),
                    factor,
                ) if major else None
                minor_ratio  = ent.get("minor_ratio") or 1.0
                minor_len_mm = (major_len_mm * minor_ratio) if major_len_mm else None

                if _SPACE_LAYER.search(layer):
                    spaces.append({
                        "handle":        handle,
                        "layer":         layer,
                        "bbox":          bbox,
                        "major_len_mm":  major_len_mm,
                        "minor_len_mm":  minor_len_mm,
                        "source":        "ellipse",
                    })
                elif _DOOR_LAYER.search(layer) or _WIN_LAYER.search(layer):
                    openings.append({
                        "handle":       handle,
                        "layer":        layer,
                        "bbox":         bbox,
                        "category":     "door" if _DOOR_LAYER.search(layer) else "window",
                        "major_len_mm": major_len_mm,
                        "source":       "ellipse",
                    })
                else:
                    # 레이어 불명 ELLIPSE: 크기 정보와 함께 unclassified 보존
                    unclassified.append({
                        "handle":       handle,
                        "type":         "ELLIPSE",
                        "layer":        layer,
                        "bbox":         bbox,
                        "major_len_mm": major_len_mm,
                        "minor_len_mm": minor_len_mm,
                    })

            # ── INSERT (블록 삽입) ──────────────────────────────────────────
            elif etype == "INSERT":
                bn  = ent.get("block_name") or ""
                cat = "unknown"
                if _DOOR_BLOCK.search(bn) or _DOOR_LAYER.search(layer):
                    cat = "door"
                elif _WIN_BLOCK.search(bn) or _WIN_LAYER.search(layer):
                    cat = "window"
                elif _STAIR_BLOCK.search(bn) or _STAIR_LAYER.search(layer):
                    cat = "stair"
                openings.append({
                    "handle":       handle,
                    "layer":        layer,
                    "bbox":         bbox,
                    "block_name":   bn,
                    "category":     cat,
                    "insert_point": ent.get("insert_point"),
                    "rotation":     ent.get("rotation"),
                    "scale_x":      ent.get("scale_x"),
                    "scale_y":      ent.get("scale_y"),
                    "attributes":   ent.get("attributes"),
                    "source":       "insert",
                })

            # ── DIMENSION ──────────────────────────────────────────────────
            elif etype == "DIMENSION":
                m = ent.get("measurement")
                dimensions.append({
                    "handle":         handle,
                    "layer":          layer,
                    "bbox":           bbox,
                    "measurement_mm": _to_mm(m, factor) if m is not None else None,
                    "dim_text":       ent.get("dim_text"),
                    "dim_type":       ent.get("dim_type"),
                    "xline1_point":   ent.get("xline1_point"),
                    "xline2_point":   ent.get("xline2_point"),
                })

            # ── TEXT / MTEXT ────────────────────────────────────────────────
            elif etype in ("TEXT", "MTEXT"):
                annotations.append({
                    "handle":      handle,
                    "layer":       layer,
                    "bbox":        bbox,
                    "text":        ent.get("text") or "",
                    "text_height": ent.get("text_height"),
                })

            # ── 기타 미인식 타입 ────────────────────────────────────────────
            else:
                unclassified.append({
                    "handle": handle,
                    "type":   etype,
                    "layer":  layer,
                    "bbox":   bbox,
                })

        # ── elements 플랫 리스트 구성 ─────────────────────────────────────────
        # ArchTopologyBuilder / ArchGeometryPreprocessor 가 공통으로 소비하는 형식.
        # 각 카테고리 항목에 category 필드를 추가하여 평탄화한다.
        elements: list[dict[str, Any]] = []
        for cat, bucket in (
            ("wall",        walls),
            ("column",      columns),
            ("curved_wall", curved_walls),
            ("space",       spaces),
            ("opening",     openings),
            ("dimension",   dimensions),
            ("annotation",  annotations),
            ("unclassified", unclassified),
        ):
            for item in bucket:
                pos = (
                    item.get("center")
                    or item.get("insert_point")
                    or self._bbox_center_pos(item.get("bbox"))
                    or {"x": 0.0, "y": 0.0}
                )
                el: dict[str, Any] = {
                    "id":       item.get("handle", ""),
                    "handle":   item.get("handle", ""),
                    "type":     cat,
                    "layer":    item.get("layer", ""),
                    "position": pos,
                    "bbox":     item.get("bbox"),
                    "category": cat,
                }
                # 카테고리별 유용한 속성 전달
                for k in (
                    "area_mm2", "thickness_mm", "length_mm", "radius_mm",
                    "measurement_mm", "text", "block_name", "insert_point",
                    "rotation", "span_deg", "arc_length_mm", "start", "end",
                    "source", "is_closed",
                ):
                    if k in item:
                        el[k] = item[k]
                elements.append(el)

        return {
            "drawing_unit":       unit,
            "unit_factor":        factor,
            "elements":           elements,
            "walls":              walls,
            "columns":            columns,
            "curved_walls":       curved_walls,
            "spaces":             spaces,
            "openings":           openings,
            "dimensions":         dimensions,
            "annotations":        annotations,
            "unclassified":       unclassified,
            "raw_entity_count":   len(entities),
            "classified_summary": {
                "walls":        len(walls),
                "columns":      len(columns),
                "curved_walls": len(curved_walls),
                "spaces":       len(spaces),
                "openings":     len(openings),
                "dimensions":   len(dimensions),
                "annotations":  len(annotations),
                "unclassified": len(unclassified),
            },
        }

    # ── 텍스트 → 지오메트리 매핑 ────────────────────────────────────────────

    def map_texts_to_blocks(
        self,
        text_entities: list[dict],
        block_entities: list[dict],
        *,
        ambiguity_threshold: float = 10.0,
    ) -> list[MappingResult]:
        """
        (동기) 주석 텍스트를 공간·개구부 지오메트리에 1:1 매핑합니다.
        건축 레이어 보너스(A-ANNO → A-AREA) 적용.
        모호한 케이스(점수 차 ≤ ambiguity_threshold)는 result.is_ambiguous=True.
        """
        results: list[MappingResult] = []
        for text_ent in text_entities:
            result = find_best_match(
                text_ent,
                block_entities,
                ambiguity_threshold=ambiguity_threshold,
                score_kwargs={"layer_bonus_config": _ARCH_TEXT_BONUS},
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
        (비동기) 주석 텍스트 → 지오메트리 매핑 + 모호 케이스 LLM fallback.
        OOM 방지 전역 세마포어 및 label 필드 포함.

        Returns
        -------
        [{"text_handle", "block_handle", "label", "score", "method"}, ...]
        """
        return await _map_texts_to_blocks_util(
            text_entities,
            block_entities,
            domain_hint="건축",
            layer_bonus_config=_ARCH_TEXT_BONUS,
            ambiguity_threshold=ambiguity_threshold,
        )

    # ── 헬퍼 ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _bbox_center_pos(bbox: dict | None) -> dict | None:
        """bbox {x1,y1,x2,y2} 또는 {min_x,min_y,max_x,max_y} → 중심 좌표."""
        if not isinstance(bbox, dict):
            return None
        try:
            x1 = float(bbox.get("x1") or bbox.get("min_x") or 0)
            y1 = float(bbox.get("y1") or bbox.get("min_y") or 0)
            x2 = float(bbox.get("x2") or bbox.get("max_x") or 0)
            y2 = float(bbox.get("y2") or bbox.get("max_y") or 0)
            return {"x": (x1 + x2) / 2, "y": (y1 + y2) / 2}
        except (TypeError, ValueError):
            return None
