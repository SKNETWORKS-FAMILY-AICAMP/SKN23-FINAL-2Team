"""
File    : backend/services/agents/piping/sub/action.py
Author  : 송주엽
Create  : 2026-04-09
Description : 선택된 객체를 전체 도면·법규와 비교 분석해 수정 명령을 생성합니다.

Modification History :
    - 2026-04-09 (송주엽) : 제어용 JSON 명령 직렬화(Action) 로직 초안 작성
    - 2026-04-14 (송주엽) : RevisionAction enum 값과 매핑 일치, 누락 액션 처리 추가
    - 2026-04-20 (김지우) : LLM 기반 선택 객체 분석 및 handle 포함 수정 명령 생성으로 재구현
    - 2026-04-27 (김지우) : DrawCommandParser 추가 — 채팅 명령으로 직접 수정/생성 (CAD_ACTION Fast Path)
"""

import json
import logging
import math
import re
from backend.services.llm_service import generate_answer


from backend.services.agents.pipe.sub.review.parser import ParserAgent

# ── DrawCommandParser / 관련 상수·유틸은 elec 패키지에서 재활용 ──────────────
# elec와 pipe 모두 동일한 DrawCommandParser 로직을 사용한다.
# 코드 중복을 피하기 위해 elec 모듈에서 직접 import 한다.
from backend.services.agents.elec.sub.action import (
    DrawCommandParser,
    _DRAW_KEYWORDS,
    _MODIFY_KEYWORDS,
    _COMMAND_PARSE_SYSTEM,
    _DIRECTION_VECTOR,
    _TYPE_FIX_GUIDE,
    _MOVE_GUIDE,
    _AUTO_FIX_SCHEMA,
    _build_modify_auto_fix,
    _safe_float,
)

__all__ = [
    "ActionAgent",
    "DrawCommandParser",
    "PipeDrawCommandParser",
]


PIPE_ACTION_CAPABILITIES = [
    "PIPE_RUN_CREATE",
    "PIPE_GAP_CONNECT",
    "PIPE_LABEL_CREATE",
    "PIPE_T_BRANCH_PATTERN",
    "PIPE_VALVE_SYMBOL",
    "PIPE_FLOW_ARROW_PATTERN",
    "PIPE_REDUCER_PATTERN",
    "PIPE_CAP_END_PATTERN",
    "PIPE_RISER_MARK_PATTERN",
    "PIPE_SLEEVE_PATTERN",
    "PIPE_METER_SET_PATTERN",
    "PIPE_GAS_ALARM_PATTERN",
    "PIPE_FIRE_SEAL_PATTERN",
    "PIPE_HANGER_MARK_PATTERN",
    "PIPE_PROPOSAL_NOTE",
]

_PIPE_PATTERN_LAYER = "AI_PIPE_PATTERN"
_PIPE_PROPOSAL_LAYER = "AI_PROPOSAL"
_PIPE_NUMBER_RE = re.compile(r"(\d+(?:\.\d+)?)")
_PIPE_SIZE_RE = re.compile(r"(?:DN\s*(\d{1,3})|(\d{1,3})\s*A)", re.IGNORECASE)


def _num_from_text(text: str, default: float) -> float:
    m = _PIPE_NUMBER_RE.search(text or "")
    if not m:
        return default
    try:
        return float(m.group(1))
    except (TypeError, ValueError):
        return default


def _length_from_text(text: str, default: float) -> float:
    """Return a likely drawing length while avoiding pipe-size labels like 20A."""
    lowered = (text or "").lower()
    explicit = re.search(r"(\d+(?:\.\d+)?)\s*(?:mm|미리|길이|거리|폭|가로|세로)", lowered)
    if explicit:
        return _safe_float(explicit.group(1))
    nums = [float(n) for n in _PIPE_NUMBER_RE.findall(text or "")]
    if not nums:
        return default
    # In pipe commands, 15/20/25 often means diameter. Prefer a later/larger value.
    candidates = [n for n in nums if n >= 100]
    return candidates[-1] if candidates else default


def _entity_center(entity: dict) -> tuple[float, float]:
    bbox = entity.get("bbox") or {}
    if isinstance(bbox, dict) and all(k in bbox for k in ("x1", "y1", "x2", "y2")):
        return (
            (float(bbox.get("x1", 0)) + float(bbox.get("x2", 0))) / 2.0,
            (float(bbox.get("y1", 0)) + float(bbox.get("y2", 0))) / 2.0,
        )
    for key in ("center", "position", "insert_point", "start"):
        pos = entity.get(key)
        if isinstance(pos, dict):
            return float(pos.get("x", 0) or 0), float(pos.get("y", 0) or 0)
    start = entity.get("start") or {}
    end = entity.get("end") or {}
    if isinstance(start, dict) and isinstance(end, dict):
        return (
            (float(start.get("x", 0)) + float(end.get("x", 0))) / 2.0,
            (float(start.get("y", 0)) + float(end.get("y", 0))) / 2.0,
        )
    return 0.0, 0.0


def _rect_vertices(cx: float, cy: float, width: float, height: float) -> list[dict]:
    hw = width / 2.0
    hh = height / 2.0
    return [
        {"x": cx - hw, "y": cy - hh, "bulge": 0},
        {"x": cx + hw, "y": cy - hh, "bulge": 0},
        {"x": cx + hw, "y": cy + hh, "bulge": 0},
        {"x": cx - hw, "y": cy + hh, "bulge": 0},
    ]


def _create_line(x1: float, y1: float, x2: float, y2: float, layer: str = _PIPE_PATTERN_LAYER) -> dict:
    return {
        "type": "CREATE_ENTITY",
        "new_start": {"x": x1, "y": y1},
        "new_end": {"x": x2, "y": y2},
        "new_layer": layer,
    }


def _create_circle(cx: float, cy: float, radius: float, layer: str = _PIPE_PATTERN_LAYER) -> dict:
    return {
        "type": "CREATE_ENTITY",
        "new_center": {"x": cx, "y": cy},
        "new_radius": radius,
        "new_layer": layer,
    }


def _create_rect(cx: float, cy: float, width: float, height: float, layer: str = _PIPE_PATTERN_LAYER) -> dict:
    return {
        "type": "CREATE_ENTITY",
        "new_vertices": _rect_vertices(cx, cy, width, height),
        "new_layer": layer,
    }


def _create_text(text: str, x: float, y: float, height: float = 180.0, layer: str = _PIPE_PATTERN_LAYER) -> dict:
    return {
        "type": "CREATE_TEXT",
        "new_text": text,
        "base_x": x,
        "base_y": y,
        "new_height": height,
        "new_layer": layer,
    }


def _create_leader_note(
    note: str,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    layer: str = _PIPE_PROPOSAL_LAYER,
) -> list[dict]:
    return [
        _create_line(x1, y1, x2, y2, layer),
        _create_line(x2 - 80.0, y2 - 35.0, x2, y2, layer),
        _create_line(x2 - 80.0, y2 + 35.0, x2, y2, layer),
        _create_text(note, x2 + 80.0, y2 + 40.0, 130.0, layer),
    ]


def _closest_endpoint_connection(entities: list[dict]) -> dict | None:
    line_ents = []
    for ent in entities:
        if str(ent.get("type", "")).upper() != "LINE":
            continue
        s = ent.get("start") or {}
        e = ent.get("end") or {}
        if isinstance(s, dict) and isinstance(e, dict):
            line_ents.append(ent)
    if len(line_ents) < 2:
        return None

    a, b = line_ents[0], line_ents[1]
    pts_a = [a.get("start"), a.get("end")]
    pts_b = [b.get("start"), b.get("end")]
    best = None
    for pa in pts_a:
        for pb in pts_b:
            if not isinstance(pa, dict) or not isinstance(pb, dict):
                continue
            dx = float(pa.get("x", 0)) - float(pb.get("x", 0))
            dy = float(pa.get("y", 0)) - float(pb.get("y", 0))
            d = math.hypot(dx, dy)
            if best is None or d < best[0]:
                best = (d, pa, pb)
    if not best:
        return None
    _, pa, pb = best
    return _create_line(
        float(pa.get("x", 0)),
        float(pa.get("y", 0)),
        float(pb.get("x", 0)),
        float(pb.get("y", 0)),
        str(a.get("layer") or _PIPE_PROPOSAL_LAYER),
    )


def _normalize_auto_fix(auto_fix):
    """Coerce LLM-produced auto_fix payloads, including pattern lists."""
    if isinstance(auto_fix, list):
        normalized = []
        for item in auto_fix:
            n = _normalize_auto_fix(item)
            if isinstance(n, list):
                normalized.extend(n)
            elif n:
                normalized.append(n)
        return normalized
    if not isinstance(auto_fix, dict):
        return auto_fix

    af = dict(auto_fix)
    af_type = str(af.get("type") or "").upper()
    if af_type == "MOVE":
        af["delta_x"] = _safe_float(af.get("delta_x", 0))
        af["delta_y"] = _safe_float(af.get("delta_y", 0))
    elif af_type == "COLOR" and af.get("new_color") is not None:
        try:
            af["new_color"] = int(float(str(af.get("new_color"))))
        except (TypeError, ValueError):
            logging.debug("[ActionAgent] COLOR new_color coercion failed: %s", af)
    elif af_type in ("RECTANGLE_RESIZE", "STRETCH_RECT"):
        af["type"] = "RECTANGLE_RESIZE"
        af["stretch_side"] = str(af.get("stretch_side") or "right").lower()
        for key in ("new_width", "new_height", "delta_x", "delta_y"):
            if af.get(key) is not None:
                af[key] = _safe_float(af.get(key))
    elif af_type in ("CREATE_ENTITY", "CREATE_TEXT"):
        for key in ("base_x", "base_y", "new_height", "new_radius"):
            if af.get(key) is not None:
                af[key] = _safe_float(af.get(key))
        for point_key in ("new_start", "new_end", "new_center"):
            pt = af.get(point_key)
            if isinstance(pt, dict):
                pt["x"] = _safe_float(pt.get("x", 0))
                pt["y"] = _safe_float(pt.get("y", 0))
                af[point_key] = pt
        vertices = af.get("new_vertices")
        if isinstance(vertices, list):
            clean_vertices = []
            for vertex in vertices:
                if not isinstance(vertex, dict):
                    continue
                clean_vertices.append(
                    {
                        "x": _safe_float(vertex.get("x", 0)),
                        "y": _safe_float(vertex.get("y", 0)),
                        "bulge": _safe_float(vertex.get("bulge", 0)),
                    }
                )
            af["new_vertices"] = clean_vertices
    return af


class PipeDrawCommandParser(DrawCommandParser):
    """Pipe-only deterministic CAD command parser for common partial patterns."""

    async def parse(
        self,
        user_text: str,
        active_handles: list[str],
        entity_by_handle: dict[str, dict],
    ) -> "dict | list | None":
        text = (user_text or "").strip()
        lowered = text.lower()
        selected = [
            entity_by_handle.get(str(h))
            for h in (active_handles or [])
            if entity_by_handle.get(str(h))
        ]
        base_x, base_y = self._pipe_base_point(selected)

        pattern = self._parse_pipe_pattern(text, lowered, selected, base_x, base_y)
        if pattern is not None:
            return pattern
        return await super().parse(user_text, active_handles, entity_by_handle)

    @staticmethod
    def _pipe_base_point(selected: list[dict]) -> tuple[float, float]:
        if selected:
            return _entity_center(selected[0])
        return 0.0, 0.0

    def _parse_pipe_pattern(
        self,
        text: str,
        lowered: str,
        selected: list[dict],
        base_x: float,
        base_y: float,
    ) -> "dict | list | None":
        has_draw = any(k in text for k in ("그려", "추가", "삽입", "만들", "넣어", "표기", "생성"))
        wants_recommendation = any(k in text for k in ("추천", "제안", "검토 포인트", "개선"))
        if not has_draw and not wants_recommendation and not any(k in lowered for k in ("draw", "add", "insert", "create")):
            if any(k in text for k in ("연결", "이어", "붙여")):
                conn = _closest_endpoint_connection(selected)
                return [conn] if conn else None
            return None

        offset = _length_from_text(text, 500.0)
        cx = base_x + (offset if selected else 0.0)
        cy = base_y

        if wants_recommendation:
            return self._proposal_note(base_x, base_y, text, selected)

        if any(k in text for k in ("끊긴", "연결", "이어", "붙여")):
            conn = _closest_endpoint_connection(selected)
            return [conn] if conn else None

        if any(k in text for k in ("계량기", "메타기", "미터기")):
            return self._meter_set(cx, cy, text)

        if any(k in text for k in ("티", "tee", "분기", "가지관", "t자", "T자")):
            return self._tee_branch(cx, cy, text)

        if any(k in text for k in ("차단밸브", "밸브", "valve")):
            return self._valve_symbol(cx, cy, text)

        if any(k in text for k in ("흐름", "방향", "화살표", "arrow", "flow")):
            return self._flow_arrow(cx, cy, text)

        if any(k in text for k in ("리듀서", "레듀샤", "reducer", "축소", "이경")):
            return self._reducer(cx, cy, text)

        if any(k in text for k in ("캡", "말단", "끝단", "마감", "cap")):
            return self._cap_end(cx, cy, text)

        if any(k in text for k in ("입상", "riser", "상향", "하향")):
            return self._riser_mark(cx, cy, text)

        if any(k in text for k in ("슬리브", "sleeve", "관통구", "관통부")):
            return self._sleeve_mark(cx, cy, text)

        if any(k in text for k in ("누설", "경보기", "알람", "alarm")):
            return self._gas_alarm(cx, cy)

        if any(k in text for k in ("관통", "방화", "실링", "seal")):
            return self._fire_seal(cx, cy)

        if any(k in text for k in ("행거", "지지", "서포트", "support")):
            return self._hanger_mark(cx, cy)

        if any(k in text for k in ("라벨", "구경", "표기", "텍스트", "text", "20a", "25a", "g ")):
            label = self._label_from_text(text)
            return _create_text(label, cx, cy + 250.0, 180.0, _PIPE_PATTERN_LAYER)

        if any(k in text for k in ("배관", "파이프", "pipe")):
            length = _length_from_text(text, 1000.0)
            label = self._label_from_text(text)
            return [
                _create_line(cx, cy, cx + length, cy, _PIPE_PATTERN_LAYER),
                _create_text(label, cx + length / 2.0, cy + 180.0, 160.0, _PIPE_PATTERN_LAYER),
            ]

        return None

    @staticmethod
    def _label_from_text(text: str) -> str:
        up = text.upper()
        sizes = PipeDrawCommandParser._diameters_from_text(up)
        size = f"{sizes[0]}A" if sizes else "20A"
        gas = "G" if any(k in up for k in ("G", "GAS", "가스")) else ""
        return f"{gas} {size}".strip()

    @staticmethod
    def _diameters_from_text(text: str) -> list[str]:
        sizes = []
        for dn_size, a_size in _PIPE_SIZE_RE.findall(text or ""):
            value = (dn_size or a_size or "").upper().replace("DN", "").replace(" ", "")
            if value and value not in sizes:
                sizes.append(value)
        return sizes

    def _meter_set(self, cx: float, cy: float, text: str) -> list[dict]:
        width = max(_length_from_text(text, 900.0), 600.0)
        height = max(width * 0.45, 360.0)
        return [
            _create_rect(cx, cy, width, height, _PIPE_PATTERN_LAYER),
            _create_circle(cx - width * 0.18, cy, height * 0.22, _PIPE_PATTERN_LAYER),
            _create_circle(cx + width * 0.18, cy, height * 0.22, _PIPE_PATTERN_LAYER),
            _create_line(cx + width / 2.0, cy, cx + width / 2.0 + 600.0, cy, _PIPE_PATTERN_LAYER),
            _create_text("GAS METER", cx - width / 2.0, cy + height / 2.0 + 180.0, 160.0, _PIPE_PATTERN_LAYER),
            _create_text("G 20A", cx + width / 2.0 + 180.0, cy + 160.0, 150.0, _PIPE_PATTERN_LAYER),
        ]

    @staticmethod
    def _valve_symbol(cx: float, cy: float, text: str) -> list[dict]:
        size = max(_length_from_text(text, 300.0), 240.0)
        half = size / 2.0
        return [
            _create_line(cx - half, cy, cx + half, cy, _PIPE_PATTERN_LAYER),
            _create_line(cx - half * 0.55, cy - half * 0.55, cx + half * 0.55, cy + half * 0.55, _PIPE_PATTERN_LAYER),
            _create_line(cx - half * 0.55, cy + half * 0.55, cx + half * 0.55, cy - half * 0.55, _PIPE_PATTERN_LAYER),
            _create_text("V", cx - 60.0, cy + half + 120.0, 150.0, _PIPE_PATTERN_LAYER),
        ]

    @staticmethod
    def _tee_branch(cx: float, cy: float, text: str) -> list[dict]:
        size = max(_length_from_text(text, 700.0), 300.0)
        half = size / 2.0
        label = PipeDrawCommandParser._label_from_text(text)
        return [
            _create_line(cx - half, cy, cx + half, cy, _PIPE_PATTERN_LAYER),
            _create_line(cx, cy, cx, cy + half, _PIPE_PATTERN_LAYER),
            _create_text(f"T {label}".strip(), cx + 90.0, cy + half + 120.0, 130.0, _PIPE_PATTERN_LAYER),
        ]

    @staticmethod
    def _flow_arrow(cx: float, cy: float, text: str) -> list[dict]:
        length = max(_length_from_text(text, 500.0), 240.0)
        head = min(length * 0.22, 140.0)
        return [
            _create_line(cx - length / 2.0, cy, cx + length / 2.0, cy, _PIPE_PATTERN_LAYER),
            _create_line(cx + length / 2.0, cy, cx + length / 2.0 - head, cy + head * 0.45, _PIPE_PATTERN_LAYER),
            _create_line(cx + length / 2.0, cy, cx + length / 2.0 - head, cy - head * 0.45, _PIPE_PATTERN_LAYER),
            _create_text("FLOW", cx - length / 2.0, cy + 120.0, 100.0, _PIPE_PATTERN_LAYER),
        ]

    def _reducer(self, cx: float, cy: float, text: str) -> list[dict]:
        sizes = self._diameters_from_text(text)
        label = f"REDUCER {sizes[0]}A-{sizes[1]}A" if len(sizes) >= 2 else "REDUCER"
        length = max(_length_from_text(text, 520.0), 320.0)
        return [
            _create_line(cx - length / 2.0, cy, cx - 80.0, cy, _PIPE_PATTERN_LAYER),
            _create_line(cx + 80.0, cy, cx + length / 2.0, cy, _PIPE_PATTERN_LAYER),
            _create_line(cx - 80.0, cy + 90.0, cx + 80.0, cy + 45.0, _PIPE_PATTERN_LAYER),
            _create_line(cx - 80.0, cy - 90.0, cx + 80.0, cy - 45.0, _PIPE_PATTERN_LAYER),
            _create_text(label, cx - 180.0, cy + 160.0, 110.0, _PIPE_PATTERN_LAYER),
        ]

    @staticmethod
    def _cap_end(cx: float, cy: float, text: str) -> list[dict]:
        size = max(_length_from_text(text, 260.0), 160.0)
        return [
            _create_line(cx - size / 2.0, cy, cx + size / 2.0, cy, _PIPE_PATTERN_LAYER),
            _create_line(cx + size / 2.0, cy - 120.0, cx + size / 2.0, cy + 120.0, _PIPE_PATTERN_LAYER),
            _create_text("CAP", cx + size / 2.0 + 80.0, cy + 100.0, 110.0, _PIPE_PATTERN_LAYER),
        ]

    @staticmethod
    def _riser_mark(cx: float, cy: float, text: str) -> list[dict]:
        label = "RISE" if "하향" not in text else "DROP"
        return [
            _create_circle(cx, cy, 150.0, _PIPE_PATTERN_LAYER),
            _create_line(cx - 110.0, cy - 110.0, cx + 110.0, cy + 110.0, _PIPE_PATTERN_LAYER),
            _create_text(label, cx + 190.0, cy + 80.0, 110.0, _PIPE_PATTERN_LAYER),
        ]

    @staticmethod
    def _sleeve_mark(cx: float, cy: float, text: str) -> list[dict]:
        radius = max(_length_from_text(text, 220.0) / 2.0, 120.0)
        return [
            _create_circle(cx, cy, radius, _PIPE_PATTERN_LAYER),
            _create_circle(cx, cy, radius * 0.68, _PIPE_PATTERN_LAYER),
            _create_text("SLEEVE", cx + radius + 80.0, cy + radius * 0.35, 110.0, _PIPE_PATTERN_LAYER),
        ]

    @staticmethod
    def _proposal_note(base_x: float, base_y: float, text: str, selected: list[dict]) -> list[dict]:
        if selected:
            note = "배관 연속성/구경/밸브 위치 검토"
            if any(k in text for k in ("끊김", "연속", "연결")):
                note = "끊김 후보: 중심선 연결 또는 주석 브리지 확인"
            elif any(k in text for k in ("밸브", "차단")):
                note = "차단밸브 접근성 및 전후단 표기 검토"
            elif any(k in text for k in ("계량", "미터")):
                note = "계량기 전후단 밸브/누설경보기 패턴 검토"
        else:
            note = "AI 배관 제안: 선택 객체 기준으로 부분 패턴 생성 권장"
        return _create_leader_note(note, base_x, base_y, base_x + 550.0, base_y + 380.0)

    @staticmethod
    def _gas_alarm(cx: float, cy: float) -> list[dict]:
        return [
            _create_rect(cx, cy, 420.0, 260.0, _PIPE_PATTERN_LAYER),
            _create_text("GAS", cx - 160.0, cy + 40.0, 120.0, _PIPE_PATTERN_LAYER),
            _create_text("ALARM", cx - 170.0, cy - 90.0, 100.0, _PIPE_PATTERN_LAYER),
        ]

    @staticmethod
    def _fire_seal(cx: float, cy: float) -> list[dict]:
        return [
            _create_circle(cx, cy, 180.0, _PIPE_PATTERN_LAYER),
            _create_line(cx - 220.0, cy, cx + 220.0, cy, _PIPE_PATTERN_LAYER),
            _create_line(cx, cy - 220.0, cx, cy + 220.0, _PIPE_PATTERN_LAYER),
            _create_text("FIRE SEAL", cx + 240.0, cy + 120.0, 120.0, _PIPE_PATTERN_LAYER),
        ]

    @staticmethod
    def _hanger_mark(cx: float, cy: float) -> list[dict]:
        return [
            _create_line(cx, cy - 220.0, cx, cy + 220.0, _PIPE_PATTERN_LAYER),
            _create_line(cx - 160.0, cy + 220.0, cx + 160.0, cy + 220.0, _PIPE_PATTERN_LAYER),
            _create_text("HANGER", cx + 180.0, cy + 150.0, 110.0, _PIPE_PATTERN_LAYER),
        ]

class ActionAgent:
    def __init__(self):
        self.parser = ParserAgent()

    async def analyze_and_fix(self, context: dict, domain: str = "pipe") -> dict:
        """
        선택된 객체(active_object_ids) 또는 포커스 영역을 정규화하여 분석하고 수정 명령을 생성합니다.
        """
        active_ids = set(context.get("active_object_ids") or [])
        raw_layout = context.get("raw_layout_data") or "{}"
        # mapping_table이 None/{} 인 경우(action Fast Path) → parser가 raw entities 그대로 사용
        mapping_table = context.get("mapping_table") or None

        # 1. 도면 데이터 정규화 (ComplianceAgent와 일관성 확보)
        parsed_result = self.parser.parse(raw_layout, mapping_table=mapping_table)
        all_elements = parsed_result.get("elements") or []

        # 2. 분석 대상 필터링
        # context_mode가 focus이거나 active_object_ids가 있는 경우 해당 요소들만 분석
        drawing_data = context.get("drawing_data") or {}
        is_focus_mode = drawing_data.get("context_mode") == "full_with_focus"

        if active_ids:
            # handle(CAD 원본) 또는 id(TAG_NAME 우선 → handle 폴백) 둘 다 매칭
            selected    = [e for e in all_elements
                           if e.get("handle") in active_ids or e.get("id") in active_ids]
            selected_keys = {e.get("handle") for e in selected} | {e.get("id") for e in selected}
            surrounding = [e for e in all_elements
                           if e.get("handle") not in selected_keys and e.get("id") not in selected_keys]
        elif is_focus_mode:
            # 포커스 영역 전체 대상
            selected    = all_elements
            surrounding = [] # 포커스 모드에서는 영역 밖 컨텍스트 생략 (노이즈 방지)
        else:
            # 전체 도면
            selected    = all_elements
            surrounding = []

        if not selected:
            logging.info("[ActionAgent] 분석 대상 없음 (active_ids=%s, count=%d)", active_ids, len(all_elements))
            return {
                "analysis": "분석할 수 있는 선택 객체가 없습니다.",
                "fixes": [],
                "message": "수정할 객체를 AutoCAD에서 선택하거나 범위에 포함시킨 후 다시 시도하세요."
            }

        retrieved_laws = context.get("retrieved_laws") or []
        law_text = "\n".join(
            r.get("content", "") or r.get("snippet", "")
            for r in retrieved_laws[:5]
        ) or "법규 데이터 없음"

        domain_hint = {
            "pipe": "배관 시방서 및 KS 배관 설계 기준",
            "fire": "소방(NFSC) 법규 및 스프링클러·소화전 설치 기준",
            "arch": "건축법 시행령 방화구획·복도·계단 기준",
            "elec": "전기설비기술기준(KEC) 및 내선규정",
        }.get(domain, "관련 법규 기준")

        system_prompt = f"""당신은 {domain_hint} 전문 CAD 도면 수정 엔지니어입니다.
[검토 대상 객체]를 [법규 기준]에 비추어 분석하고, 수정이 필요한 항목에 대해 구체적인 수정 명령을 생성하세요.

규칙:
- 사용자가 이동·삭제·색상·레이어·텍스트·블록 교체 등 명시적인 편집을 지시한 경우,
  법규 기준보다 사용자 지시를 우선하여 해당 수정 명령을 생성하세요.
- 법규 데이터가 없거나 불충분하면 법규 위반을 새로 추론하지 말고, 사용자 지시 또는 pending_fixes에
  근거한 수정만 생성하세요.
- 레이어명·색상·선종류 표준은 사용자/프로젝트마다 다를 수 있습니다. 색상이나 레이어명 하나만으로
  위반 또는 수정 필요성을 단정하지 말고, 입력 객체의 layer_role, attributes, type, handle, 사용자 지시를 함께 보세요.
- handle 값은 입력받은 데이터를 기반으로 정확히 매칭하세요.
- 수정이 불필요한 객체는 fixes 에 포함하지 마세요.
- auto_fix.type 및 상세 필드는 C# DrawingPatcher 규격을 엄격히 준수하세요:
  * LAYER: {{"type": "LAYER", "new_layer": "레이어명"}}
  * MOVE: {{"type": "MOVE", "delta_x": 상대X, "delta_y": 상대Y}}
  * ATTRIBUTE: {{"type": "ATTRIBUTE", "attribute_tag": "태그명", "new_value": "새값"}}
  * TEXT_CONTENT: {{"type": "TEXT_CONTENT", "new_text": "내용"}}
  * TEXT_HEIGHT: {{"type": "TEXT_HEIGHT", "new_height": 높이}}
  * COLOR: {{"type": "COLOR", "new_color": ACI번호}} — AutoCAD ACI 색상 번호(예: 1=빨강, 2=노랑, 3=초록). 사용자가 '빨간색' 등만 말해도 이 번호로 변환.
  * ROTATE: {{"type": "ROTATE", "angle": 각도(도), "base_x": X, "base_y": Y}}
  * SCALE: {{"type": "SCALE", "scale_x": 배율, "base_x": X, "base_y": Y}}
  * DELETE | LINETYPE | LINEWEIGHT | GEOMETRY
  * BLOCK_REPLACE: {{"type": "BLOCK_REPLACE", "new_block_name": "도면에 로드된 블록명"}}  (심볼·독립블록 규격 상향)
  * DYNAMIC_BLOCK_PARAM: {{"type": "DYNAMIC_BLOCK_PARAM", "param_name": "AutoCAD동적조회명", "param_value": "값"}}  (C#이 BlockReference의 DynamicBlockReferenceProperty에 직접 반영. 대상이 동적 블록·해당 파라미터가 있을 때)

[수정 강도 — auto_fix.type 기준(생략 시 백엔드가 type으로 추론)]
  - **속성만(기하 유지)**: LAYER, COLOR, TEXT_*, LINE*, ATTRIBUTE
  - **이동/회전/블록(원본 entity에 직접, 승인 시 확인)**: **MOVE**, **ROTATE**, **BLOCK_REPLACE**
  - **동적 블록**: DYNAMIC_BLOCK_PARAM
  - **원본 유지+제안(AI_PROPOSAL)**: GEOMETRY(좌표 직접), DELETE, **SCALE** 등 — 복잡/위험 기하

[출력 JSON 스키마]
{{
  "analysis": "객체별 분석 요약 (한국어)",
  "fixes": [
    {{
      "handle":   "원본 handle",
      "type":     "객체 타입",
      "reason":   "수정 이유",
      "action":   "수정 종류 (LAYER, MOVE 등)",
      "auto_fix": {{ "type": "MOVE", "delta_x": 100, "delta_y": 0 }}
    }}
  ],
  "message": "사용자 요약 메시지"
}}"""

        system_prompt += (
            "\n\n[공통 CAD 수정 스키마 보강]\n"
            f"{_TYPE_FIX_GUIDE}\n\n{_MOVE_GUIDE}\n\n{_AUTO_FIX_SCHEMA}\n"
            "- RECTANGLE_RESIZE는 닫힌 사각형 polyline의 한쪽 변을 기준으로 폭/높이를 바꾸는 명령입니다.\n"
            "- 독립 LINE 4개로 만든 사각형은 하나의 SCALE이 아니라 관련 LINE들의 GEOMETRY/MOVE 조합이 필요하므로, "
            "대상 handle과 새 좌표를 확정할 수 없으면 MANUAL_REVIEW로 남기세요.\n"
            "\n[배관 전용 추천/부분 패턴 액션]\n"
            "- 사용자가 배관 패턴 생성 또는 추천을 요청하면 전체 도면을 새로 만들지 말고 선택 객체 기준의 작은 패턴만 생성하세요.\n"
            "- 지원 후보: PIPE_RUN_CREATE, PIPE_GAP_CONNECT, PIPE_LABEL_CREATE, PIPE_VALVE_SYMBOL, "
            "PIPE_METER_SET_PATTERN, PIPE_GAS_ALARM_PATTERN, PIPE_FIRE_SEAL_PATTERN, PIPE_HANGER_MARK_PATTERN, PIPE_PROPOSAL_NOTE.\n"
            "- 추가 지원 후보: PIPE_T_BRANCH_PATTERN, PIPE_FLOW_ARROW_PATTERN, PIPE_REDUCER_PATTERN, PIPE_CAP_END_PATTERN, "
            "PIPE_RISER_MARK_PATTERN, PIPE_SLEEVE_PATTERN.\n"
            "- CAD에 직접 그릴 수 있는 것은 CREATE_ENTITY/CREATE_TEXT 목록으로 나누어 auto_fix에 넣으세요. "
            "여러 객체가 필요한 패턴은 fixes[].auto_fix를 list로 반환해도 됩니다.\n"
            "- 블록 정의가 도면에 있는지 확실하지 않으면 BLOCK 삽입보다 선/원/텍스트로 된 AI_PIPE_PATTERN 형상을 우선 사용하세요.\n"
            "- 확실하지 않은 법규 조항/도면 상태를 단정하지 마세요. 근거가 선택 객체나 제공 법규에 없으면 "
            "AI_PROPOSAL 레이어의 제안 노트 또는 MANUAL_REVIEW 성격의 fix만 반환하세요.\n"
        )

        user_instr = (context.get("user_request") or "").strip()
        user_block = (
            f"[사용자가 요청한 작업(반드시 반영)]\n{user_instr}\n\n"
            if user_instr
            else ""
        )
        user_prompt = f"""{user_block}[검토 대상 객체 ({len(selected)}개)]:
{json.dumps(selected, ensure_ascii=False, indent=2)}

[주변 참고 컨텍스트]:
{json.dumps(surrounding[:10], ensure_ascii=False, indent=2) if surrounding else "없음"}

[{domain_hint}]:
{law_text}

대상 객체들이 법규에 비추어 수정이 필요한지 판단하고 수정 명령을 JSON으로 출력하세요."""

        try:
            result = await generate_answer(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_prompt},
                ],
                response_format={"type": "json_object"},
            )
            if not isinstance(result, dict):
                if isinstance(result, str) and result.strip():
                    result = json.loads(result)
                else:
                    result = {}
            fixes = result.get("fixes") or []
            valid_ids = {
                str(e.get("handle") or e.get("id"))
                for e in selected
                if e.get("handle") or e.get("id")
            }
            clean_fixes = []
            for fix in fixes:
                if not isinstance(fix, dict):
                    continue
                af = _normalize_auto_fix(fix.get("auto_fix"))
                if not af:
                    continue
                handle = str(fix.get("handle") or "")
                af_items = af if isinstance(af, list) else [af]
                is_create_only = all(
                    isinstance(item, dict) and str(item.get("type") or "").upper().startswith("CREATE")
                    for item in af_items
                )
                if handle and valid_ids and handle not in valid_ids and not is_create_only:
                    logging.info("[ActionAgent] unknown handle fix dropped: %s", handle)
                    continue
                fix["auto_fix"] = af
                clean_fixes.append(fix)

            result["fixes"] = clean_fixes
            result.setdefault("analysis", "분석 완료")
            result.setdefault("message", f"선택 객체 {len(selected)}개 분석 완료. {len(result['fixes'])}개 수정 필요.")
            return result
        except Exception as e:
            logging.error("[ActionAgent] LLM 분석 실패: %s", e)
            return {
                "analysis": "분석 중 오류 발생",
                "fixes": [],
                "message": f"수정 분석 중 오류가 발생했습니다: {e}"
            }
