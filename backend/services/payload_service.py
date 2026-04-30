"""
File    : backend/services/payload_service.py
Author  : 양창일
Create  : 2026-04-13
Description : agent 요청 payload를 state 저장용 표준 구조로 정규화하는 서비스

Modification History :
    - 2026-04-13 (양창일) : drawing_data 및 retrieved_laws 정규화 함수 추가
    - 2026-04-28 (송주엽) : 단위 정규화(unit_to_mm_factor) + 도면층 JSON 추출(extract_layers_json) 추가
"""

from collections import Counter
from typing import Any

# C#에서 전체 + 선택 구간 동시 전송 시 cad_interop가 drawing_data에 설정
CONTEXT_MODE_FULL = "full"
CONTEXT_MODE_FULL_WITH_FOCUS = "full_with_focus"

# ─── 단위 변환 테이블 ─────────────────────────────────────────────────────────
_UNIT_TO_MM: dict[str, float] = {
    "mm":      1.0,
    "millimeter": 1.0,
    "cm":      10.0,
    "centimeter": 10.0,
    "m":       1000.0,
    "meter":   1000.0,
    "inch":    25.4,
    '"':       25.4,
    "in":      25.4,
    "ft":      304.8,
    "'":       304.8,
    "feet":    304.8,
    "foot":    304.8,
    "unknown": 1.0,  # 기본값: mm 가정
}

# ─── 도면층 색상 도메인 힌트 (AutoCAD Color Index, ACI) ───────────────────────
# CYAN 계열 → 배관(MEP), GRAY 계열 → 건축(ARCH), RED 계열 → 소방(FIRE)
_ACI_PIPE_COLORS: frozenset[int]  = frozenset({4, 154, 170, 30})   # cyan/teal
_ACI_ARCH_COLORS: frozenset[int]  = frozenset({8, 9, 250, 251, 252, 253, 254, 255})  # gray/white
_ACI_FIRE_COLORS: frozenset[int]  = frozenset({1, 10, 11})          # red
_ACI_ELEC_COLORS: frozenset[int]  = frozenset({3, 2, 62, 63})       # green/yellow


def should_preserve_full_entities(drawing_data: dict[str, Any]) -> bool:
    """전체 맥락 유지(선택으로 entities 축소하지 않음)."""
    return drawing_data.get("context_mode") == CONTEXT_MODE_FULL_WITH_FOCUS


def normalize_agent_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized_payload = dict(payload)
    normalized_payload["drawing_data"] = normalize_drawing_data(payload)
    normalized_payload["retrieved_laws"] = normalize_retrieved_laws(payload)
    normalized_payload["active_object_ids"] = normalize_active_object_ids(payload)
    return normalized_payload


def recompute_layer_entity_counts(cad_data: dict[str, Any]) -> None:
    """
    C#에서 청크로 쪼개 전송한 뒤 엔티티를 합친 경우, layers[].entity_count 를 entities 기준으로 다시 맞춘다.
    """
    if not isinstance(cad_data, dict):
        return
    entities = cad_data.get("entities") or []
    c = Counter()
    for e in entities:
        if not isinstance(e, dict):
            continue
        layer = e.get("layer")
        c[str(layer) if layer is not None else ""] += 1
    for layer in cad_data.get("layers") or []:
        if not isinstance(layer, dict):
            continue
        name = layer.get("name")
        if name is not None:
            layer["entity_count"] = c.get(str(name), 0)


# ─── 단위 정규화 헬퍼 ─────────────────────────────────────────────────────────

def _unit_to_mm_factor(drawing_unit: str) -> float:
    """drawing_unit 문자열 → mm 변환 계수. 미인식 단위는 1.0(mm 가정)."""
    return _UNIT_TO_MM.get(str(drawing_unit or "").strip().lower(), 1.0)


# ─── 도면 타이틀 블록 파싱 헬퍼 ────────────────────────────────────────────────────

import re as _re_ps

_SCALE_RE   = _re_ps.compile(r"1\s*:\s*(\d+)")            # "1:40", "1 : 100"
_DWG_NO_RE  = _re_ps.compile(r"[A-Z]-?\d{2,3}")           # "G-02", "P01"
_FLOOR_RE   = _re_ps.compile(r"(B?\d+)중|지(\d+)층|(?:B|\d+)F", _re_ps.IGNORECASE)
_DOMAIN_MAP = {"가스": "GAS", "급수": "WATER", "소화": "FIRE", "스프링클러": "FIRE",
               "없반": "HVAC", "구도": "DRAIN"}


def _parse_scale_ratio(text: str) -> float | None:
    """−1:40’ 스타일 문자열에서 scale ratio(40.0) 추출. 미인식시 None."""
    m = _SCALE_RE.search(text)
    return float(m.group(1)) if m else None


def _extract_title_block_info(entities: list[dict]) -> dict:
    """
    도면 타이틀 블록 영역(TEXT/MTEXT/MLEADER)에서 정보 추출.

    Returns:
        {
          "drawing_number"  : str | None    (e.g. "G-02")
          "drawing_title"   : str | None    (e.g. "1층 가스 배관 평면도")
          "drawing_scale"   : float | None  (e.g. 40.0 for 1:40)
          "floor"           : str | None    (e.g. "1층")
          "domain_hint"     : str | None    ("GAS" | "WATER" | ...)
        }
    """
    dwg_no: str | None    = None
    dwg_title: str | None = None
    scale: float | None   = None
    floor: str | None     = None
    domain: str | None    = None

    text_entities = [
        e for e in (entities or [])
        if isinstance(e, dict)
        and str(e.get("type") or "").upper() in ("TEXT", "MTEXT", "MLEADER")
    ]

    for e in text_entities:
        raw = str(e.get("text") or e.get("content") or "").strip()
        if not raw:
            continue

        # 축청 추출 (1:40, 1:100 등)
        if scale is None:
            s = _parse_scale_ratio(raw)
            if s is not None:
                scale = s

        # 도면 번호 (G-02, P-01 등)
        if dwg_no is None:
            m = _DWG_NO_RE.search(raw)
            if m:
                dwg_no = m.group(0)

        # 층 정보
        if floor is None:
            m = _FLOOR_RE.search(raw)
            if m:
                floor = m.group(0)

        # 도면 제목 (평면도 / 리스트 평면도 등)
        if dwg_title is None and ("평면도" in raw or "배관도" in raw or "플랜" in raw.lower()):
            dwg_title = raw[:60]

        # 도메인 힌트
        if domain is None:
            for kw, dom in _DOMAIN_MAP.items():
                if kw in raw:
                    domain = dom
                    break

    return {
        "drawing_number": dwg_no,
        "drawing_title":  dwg_title,
        "drawing_scale":  scale,
        "floor":          floor,
        "domain_hint":    domain,
    }


# ─── 도면층 분석 헬퍼 ─────────────────────────────────────────────────────────

def _color_int(val: Any) -> int | None:
    """색상 값을 int로 변환. 변환 불가 시 None."""
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _infer_layer_role(layer_name: str, color: int | None = None) -> str:
    """
    레이어 이름 + 색상으로 도메인 역할 추론.
    우선순위: NCS 접두어 > 색상 힌트 > 알 수 없음
    """
    name = str(layer_name or "").strip().upper()

    # NCS 접두어 기반 규칙 (건축/배관 표준)
    if name.startswith(("A-", "AR-", "ARCH")):
        return "arch"
    if name.startswith(("P-", "PI-", "PIPE", "MEP-", "L-", "GAS", "W-", "WTR")):
        return "mep"
    if name.startswith(("F-", "FP-", "SP-", "FIRE", "SPRINK")):
        return "fire"
    if name.startswith(("E-", "EL-", "ELEC")):
        return "elec"
    if name in ("0", "DEFPOINTS"):
        return "aux"

    # 색상 힌트 보조
    if color is not None:
        if color in _ACI_PIPE_COLORS:
            return "mep"
        if color in _ACI_ARCH_COLORS:
            return "arch"
        if color in _ACI_FIRE_COLORS:
            return "fire"
        if color in _ACI_ELEC_COLORS:
            return "elec"

    return "unknown"


def _get_dominant_type(entities: list[dict]) -> str:
    """엔티티 목록에서 가장 많은 raw_type을 반환."""
    if not entities:
        return ""
    c: Counter[str] = Counter()
    for e in entities:
        t = str(e.get("raw_type") or e.get("type") or "").upper()
        if t:
            c[t] += 1
    return c.most_common(1)[0][0] if c else ""


def _get_dominant_color(entities: list[dict]) -> int | None:
    """엔티티 목록에서 가장 많이 등장하는 색상(ACI int)."""
    if not entities:
        return None
    c: Counter = Counter()
    for e in entities:
        col = _color_int(e.get("color"))
        if col is not None:
            c[col] += 1
    return c.most_common(1)[0][0] if c else None


def _compute_avg_size(entities: list[dict]) -> float:
    """엔티티 bbox 기반 평균 크기(width*height). bbox 없는 엔티티는 제외."""
    sizes = []
    for e in entities:
        b = e.get("bbox")
        if not isinstance(b, dict):
            continue
        try:
            if "x1" in b:
                w = abs(float(b["x2"]) - float(b["x1"]))
                h = abs(float(b["y2"]) - float(b["y1"]))
            elif "min_x" in b:
                w = abs(float(b["max_x"]) - float(b["min_x"]))
                h = abs(float(b["max_y"]) - float(b["min_y"]))
            else:
                continue
            sizes.append(w * h)
        except (TypeError, ValueError, KeyError):
            continue
    return round(sum(sizes) / len(sizes), 2) if sizes else 0.0


def _compute_type_ratio(entities: list[dict], raw_type: str) -> float:
    """특정 raw_type 엔티티 비율 (0.0~1.0)."""
    if not entities:
        return 0.0
    target = raw_type.upper()
    count = sum(
        1 for e in entities
        if str(e.get("raw_type") or e.get("type") or "").upper() == target
    )
    return round(count / len(entities), 4)


def _compute_entity_type_distribution(entities: list[dict]) -> dict[str, int]:
    """엔티티 타입별 개수 분포."""
    c: Counter[str] = Counter()
    for e in entities:
        t = str(e.get("raw_type") or e.get("type") or "UNKNOWN").upper()
        c[t] += 1
    return dict(c)


# ─── 도면층 JSON 추출 ─────────────────────────────────────────────────────────

def extract_layers_json(cad_data: dict[str, Any]) -> dict[str, Any]:
    """
    CAD JSON에서 도면층 구조를 추출하고 레이어별 엔티티 특성을 계산한다.

    Returns:
        {
          "layers": [LayerInfo, ...],
          "layer_count": int,
          "entities_by_layer": {layer_name: [entity, ...]},
        }
    where LayerInfo = {name, color, role, entity_count, entity_types,
                       characteristics: {dominant_type, dominant_color,
                                         avg_entity_size, text_entity_ratio,
                                         block_entity_ratio}}
    """
    if not isinstance(cad_data, dict):
        return {"layers": [], "layer_count": 0, "entities_by_layer": {}}

    layers_raw: list[dict] = cad_data.get("layers") or []
    entities: list[dict]   = cad_data.get("entities") or []

    # 레이어별 엔티티 그룹화
    entities_by_layer: dict[str, list[dict]] = {}
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        layer = str(entity.get("layer") or "unknown")
        entities_by_layer.setdefault(layer, []).append(entity)

    # 레이어별 구조화
    layers_structured: list[dict] = []
    for layer_raw in layers_raw:
        if not isinstance(layer_raw, dict):
            continue
        name   = str(layer_raw.get("name") or "")
        color  = _color_int(layer_raw.get("color"))
        ents   = entities_by_layer.get(name, [])

        layers_structured.append({
            "name":        name,
            "color":       color,
            "linetype":    layer_raw.get("linetype", "Continuous"),
            "lineweight":  layer_raw.get("lineweight", -1),
            "is_locked":   bool(layer_raw.get("is_locked", False)),
            "is_frozen":   bool(layer_raw.get("is_frozen", False)),
            "is_visible":  bool(layer_raw.get("is_visible", True)),
            "role":        _infer_layer_role(name, color),
            "entity_count": len(ents),
            "entity_types": _compute_entity_type_distribution(ents),
            "characteristics": {
                "dominant_type":       _get_dominant_type(ents),
                "dominant_color":      _get_dominant_color(ents),
                "avg_entity_size":     _compute_avg_size(ents),
                "text_entity_ratio":   _compute_type_ratio(ents, "TEXT"),
                "block_entity_ratio":  _compute_type_ratio(ents, "INSERT"),
            },
        })

    return {
        "layers":           layers_structured,
        "layer_count":      len(layers_structured),
        "entities_by_layer": entities_by_layer,
    }


# ─── 정규화 함수 ─────────────────────────────────────────────────────────────

def normalize_drawing_data(payload: dict[str, Any]) -> dict[str, Any]:
    """
    C# `cad_data` / `drawing_data` 정규화.
    drawing_scale, drawing_number, drawing_title, domain_hint 필드를 추가로 추출합니다.
    """
    drawing_data = payload.get("drawing_data")
    if isinstance(drawing_data, dict):
        # drawing_data가 이미 정규화된 경우에도 필수 필드 보장
        out = dict(drawing_data)
        if "unit_to_mm_factor" not in out:
            out["unit_to_mm_factor"] = _unit_to_mm_factor(
                out.get("drawing_unit", "unknown")
            )
        if "drawing_scale" not in out:
            tb = _extract_title_block_info(out.get("entities") or [])
            out.setdefault("drawing_scale",  tb["drawing_scale"])
            out.setdefault("drawing_number", tb["drawing_number"])
            out.setdefault("drawing_title",  tb["drawing_title"])
            out.setdefault("domain_hint",    tb["domain_hint"])
            if tb["floor"] and not out.get("floor"):
                out["floor"] = tb["floor"]
        return out

    cad_data = payload.get("cad_data")
    if isinstance(cad_data, dict):
        entities    = cad_data.get("entities", [])
        layers      = cad_data.get("layers", [])
        metadata    = cad_data.get("metadata", {})
        drawing_unit = cad_data.get("drawing_unit", "unknown")
        # 타이틀 블록 파싱
        tb = _extract_title_block_info(entities)
        # metadata 스케일이 있으면 우선
        scale = (_parse_scale_ratio(str(metadata.get("scale") or ""))
                 or tb["drawing_scale"])
        return {
            "project_name":      metadata.get("project_name", ""),
            "floor":             metadata.get("floor") or tb["floor"] or "",
            "space_name":        metadata.get("space_name", ""),
            "usage":             metadata.get("usage", ""),
            "drawing_unit":      drawing_unit,
            "unit_to_mm_factor": _unit_to_mm_factor(drawing_unit),
            # 신규: 타이틀 블록 추출
            "drawing_scale":     scale,
            "drawing_number":    metadata.get("drawing_number") or tb["drawing_number"],
            "drawing_title":     metadata.get("drawing_title")  or tb["drawing_title"],
            "domain_hint":       tb["domain_hint"],
            "entity_count":      len(entities),
            "layer_count":       len(layers),
            "layers":            layers,
            "entities":          entities,
            "objects":           entities[:50],
        }

    return {}


def normalize_retrieved_laws(payload: dict[str, Any]) -> list[dict[str, Any]]:
    retrieved_laws = payload.get("retrieved_laws")
    if not isinstance(retrieved_laws, list):
        return []

    normalized_laws: list[dict[str, Any]] = []
    for index, law in enumerate(retrieved_laws):
        if not isinstance(law, dict):
            continue

        normalized_laws.append(
            {
                "chunk_id": str(law.get("chunk_id", f"temp_chunk_{index}")),
                "document_id": str(law.get("document_id", law.get("doc_id", ""))),
                "legal_reference": str(law.get("legal_reference", law.get("reference", ""))),
                "snippet": str(law.get("snippet", law.get("content", ""))),
                "score": float(law.get("score", 0.0) or 0.0),
                "source_type": str(law.get("source_type", "master")),
            }
        )

    return normalized_laws


def normalize_active_object_ids(payload: dict[str, Any]) -> list[str]:
    active_object_ids = payload.get("active_object_ids")
    if not isinstance(active_object_ids, list):
        return []
    return [str(object_id) for object_id in active_object_ids]
