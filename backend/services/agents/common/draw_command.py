"""
Shared parser for direct CAD edit/draw chat commands.

This module intentionally lives under agents.common because the payload it
produces is CAD/DrawingPatcher-oriented, not specific to electrical or piping.
Domain modules can subclass DrawCommandParser for domain-only patterns.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from backend.services.llm_service import generate_answer


_DRAW_KEYWORDS = (
    "추가", "그려", "그리", "만들", "생성", "삽입", "넣어", "표기",
    "생성해줘", "생성해", "생성하",
    "호", "아크", "타원", "텍스트", "글자", "문자",
    "add", "draw", "create", "insert", "arc", "ellipse",
)

_COLOR_KEYWORD_ACI: dict[str, int] = {
    "빨간색": 1, "빨강": 1, "레드": 1, "red": 1,
    "노란색": 2, "노랑": 2, "옐로우": 2, "yellow": 2,
    "초록색": 3, "초록": 3, "녹색": 3, "그린": 3, "green": 3,
    "청록색": 4, "청록": 4, "시안": 4, "cyan": 4,
    "파란색": 5, "파랑": 5, "블루": 5, "blue": 5,
    "보라색": 6, "보라": 6, "마젠타": 6, "자주": 6, "magenta": 6,
    "흰색": 7, "흰": 7, "화이트": 7, "white": 7,
    "회색": 8, "gray": 8, "grey": 8,
    "검은색": 250, "검정": 250, "블랙": 250, "black": 250,
}


def _extract_color_from_text(text: str) -> int | None:
    lower = text.lower()
    for kw, aci in _COLOR_KEYWORD_ACI.items():
        if kw in lower:
            return aci
    return None

_MODIFY_KEYWORDS = (
    "레이어", "도면층", "layer",
    "두께", "굵기", "선가중치", "lineweight",
    "색상", "색깔", "컬러", "color",
    "선종류", "선 타입", "linetype",
    "삭제", "지워", "제거", "delete", "remove", "erase",
    "이동", "옮겨", "move",
    "회전", "돌려", "rotate",
    "크기", "축척", "scale",
    "텍스트", "글자", "내용", "속성", "attribute",
    "바꿔", "변경", "수정", "적용",
)

_TYPE_FIX_GUIDE = """\
Auto-fix action types:
- LAYER: new_layer
- LINEWEIGHT: new_lineweight in mm
- COLOR: new_color as AutoCAD ACI integer
- LINETYPE: new_linetype
- DELETE
- MOVE: delta_x, delta_y
- ROTATE: angle
- SCALE: scale_factor
- TEXT_CONTENT: new_text
- TEXT_HEIGHT: new_height
- ATTRIBUTE: attribute_tag, attribute_value
- RECTANGLE_RESIZE: stretch_side plus new_width/new_height or delta_x/delta_y
"""

_MOVE_GUIDE = """\
Direction guide:
- right/east: positive X
- left/west: negative X
- up/north: positive Y
- down/south: negative Y
- Use drawing units as millimeters unless the user explicitly says otherwise.
"""

_AUTO_FIX_SCHEMA = """\
Return JSON only. For direct modification, produce:
{
  "command_type": "modify",
  "action_type": "LAYER | LINEWEIGHT | COLOR | LINETYPE | DELETE | MOVE | ROTATE | SCALE | TEXT_CONTENT | TEXT_HEIGHT | ATTRIBUTE | RECTANGLE_RESIZE",
  "new_layer": "...",
  "new_lineweight": 0.25,
  "new_color": 1,
  "new_linetype": "CONTINUOUS",
  "delta_x": 0,
  "delta_y": 0,
  "angle": 0,
  "scale_factor": 1.0,
  "new_text": "...",
  "new_height": 100,
  "attribute_tag": "...",
  "attribute_value": "..."
}
"""

_COMMAND_PARSE_SYSTEM = f"""\
사용자 명령에서 AutoCAD 직접 작업 의도를 분석하세요.
반드시 JSON 객체 하나만 반환하고 설명 문장은 넣지 마세요.

지원 스키마:
{{
  "command_type": "draw | modify | none",
  "shape": "circle | line | rectangle | polyline | arc | ellipse | text | block | none",
  "direction": "right | left | up | down | upper_right | upper_left | lower_right | lower_left | none",
  "distance_mm": 500,
  "size_mm": 200,
  "width_mm": 200,
  "height_mm": 200,
  "start_angle_deg": 0,
  "end_angle_deg": 270,
  "semi_minor_mm": 100,
  "rotation_deg": 0,
  "text_content": "TEXT",
  "text_height_mm": 150,
  "block_name": null,
  "draw_layer": "AI_PROPOSAL",
  "action_type": "LAYER | LINEWEIGHT | COLOR | LINETYPE | DELETE | MOVE | ROTATE | SCALE | TEXT_CONTENT | TEXT_HEIGHT | ATTRIBUTE | RECTANGLE_RESIZE | CREATE_WIRE | CONNECT_DEVICE | REPLACE_WIRE_SIZE | CLEANUP_DUPLICATE",
  "new_layer": "P-PIPE",
  "new_lineweight": 0.25,
  "new_color": 1,
  "new_linetype": "CONTINUOUS",
  "delta_x": 0,
  "delta_y": 0,
  "angle": 0,
  "scale_factor": 1.0,
  "stretch_side": "right",
  "new_width": 1000,
  "new_height": 500,
  "new_text": "TEXT",
  "attribute_tag": "TAG",
  "attribute_value": "VALUE",
  "start_handle": "",
  "end_handle": "",
  "device_handle": "",
  "panel_handle": "",
  "target_handle": "",
  "keep_handle": "",
  "remove_handle": "",
  "wire_size": "2.5SQ",
  "voltage": 220,
  "circuit": "L1"
}}

판단 규칙:
1. 새 객체 추가/그리기/삽입 요청이면 command_type="draw".
   - 원/circle → shape="circle", size_mm=반지름
   - 선/line   → shape="line",   size_mm=길이
   - 사각형/rectangle → shape="rectangle", width_mm, height_mm
   - 호/arc    → shape="arc",    size_mm=반지름, start_angle_deg, end_angle_deg
   - 타원/ellipse → shape="ellipse", size_mm=반장축, semi_minor_mm=반단축, rotation_deg
   - 텍스트/글자/문자 → shape="text", text_content=내용, text_height_mm=글자높이
2. 선택 객체의 레이어, 색상, 선두께, 선종류, 이동, 회전, 삭제, 크기, 텍스트, 속성 변경이면 command_type="modify".
3. 질문, 검토, 설명 요청이면 command_type="none".
4. 도메인 전용 패턴은 처리하지 말고 일반 CAD payload로 표현 가능한 경우만 반환하세요.

색상(new_color) 매핑 — AutoCAD ACI 인덱스:
  빨간색/red=1, 노란색/yellow=2, 초록색/green=3, 청록색/cyan=4,
  파란색/blue=5, 마젠타/magenta=6, 흰색/white=7, 회색/gray=8,
  검은색/black=250
draw 요청에 색상이 명시되면 반드시 new_color 필드를 포함하세요.
예) "빨간색 원을 그려줘" → command_type="draw", shape="circle", new_color=1
예) "반지름 300인 호 그려줘" → command_type="draw", shape="arc", size_mm=300, start_angle_deg=0, end_angle_deg=270
예) "반장축 400 타원 그려줘" → command_type="draw", shape="ellipse", size_mm=400, semi_minor_mm=200
예) "안녕하세요 텍스트 삽입" → command_type="draw", shape="text", text_content="안녕하세요", text_height_mm=150

{_TYPE_FIX_GUIDE}
{_MOVE_GUIDE}
{_AUTO_FIX_SCHEMA}
"""

_DIRECTION_VECTOR: dict[str, tuple[float, float]] = {
    "right": (1.0, 0.0),
    "left": (-1.0, 0.0),
    "up": (0.0, 1.0),
    "down": (0.0, -1.0),
    "upper_right": (0.7071, 0.7071),
    "upper_left": (-0.7071, 0.7071),
    "lower_right": (0.7071, -0.7071),
    "lower_left": (-0.7071, -0.7071),
    "none": (0.0, 0.0),
}


def _safe_float(value: Any) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0


def _build_modify_auto_fix(parsed: dict[str, Any]) -> dict[str, Any] | None:
    action_type = str(parsed.get("action_type") or "").upper()
    if not action_type:
        return None

    if action_type == "LAYER":
        new_layer = str(parsed.get("new_layer") or "").strip()
        if not new_layer:
            return None
        return {"type": "LAYER", "new_layer": new_layer}

    if action_type == "LINEWEIGHT":
        val = parsed.get("new_lineweight")
        if val is None:
            return None
        return {"type": "LINEWEIGHT", "new_lineweight": _safe_float(val)}

    if action_type == "COLOR":
        val = parsed.get("new_color")
        if val is None:
            return None
        try:
            return {"type": "COLOR", "new_color": int(float(str(val)))}
        except (TypeError, ValueError):
            return None

    if action_type == "LINETYPE":
        lt = str(parsed.get("new_linetype") or "").strip().upper() or "CONTINUOUS"
        return {"type": "LINETYPE", "new_linetype": lt}

    if action_type == "DELETE":
        return {"type": "DELETE"}

    if action_type == "MOVE":
        return {
            "type": "MOVE",
            "delta_x": _safe_float(parsed.get("delta_x", 0)),
            "delta_y": _safe_float(parsed.get("delta_y", 0)),
        }

    if action_type == "ROTATE":
        return {
            "type": "ROTATE",
            "angle": _safe_float(parsed.get("angle", 0)),
            "base_x": 0.0,
            "base_y": 0.0,
        }

    if action_type == "SCALE":
        sf = _safe_float(parsed.get("scale_factor", 1.0))
        return {"type": "SCALE", "scale_x": sf, "base_x": 0.0, "base_y": 0.0}

    if action_type in ("RECTANGLE_RESIZE", "STRETCH_RECT"):
        out: dict[str, Any] = {
            "type": "RECTANGLE_RESIZE",
            "stretch_side": str(parsed.get("stretch_side") or "right").lower(),
        }
        if parsed.get("new_width") is not None:
            out["new_width"] = _safe_float(parsed.get("new_width"))
        if parsed.get("new_height") is not None:
            out["new_height"] = _safe_float(parsed.get("new_height"))
        if parsed.get("delta_x") is not None:
            out["delta_x"] = _safe_float(parsed.get("delta_x"))
        if parsed.get("delta_y") is not None:
            out["delta_y"] = _safe_float(parsed.get("delta_y"))
        return out

    if action_type == "TEXT_CONTENT":
        new_text = str(parsed.get("new_text") or "")
        if not new_text:
            return None
        return {"type": "TEXT_CONTENT", "new_text": new_text}

    if action_type == "TEXT_HEIGHT":
        val = parsed.get("new_height")
        if val is None:
            return None
        return {"type": "TEXT_HEIGHT", "new_height": _safe_float(val)}

    if action_type == "ATTRIBUTE":
        tag = str(parsed.get("attribute_tag") or "").strip()
        value = str(parsed.get("attribute_value") or "").strip()
        if not tag:
            return None
        return {"type": "ATTRIBUTE", "attribute_tag": tag, "new_value": value}

    if action_type == "CREATE_WIRE":
        return {
            "type": "create_wire",
            "start_handle": str(parsed.get("start_handle") or ""),
            "end_handle": str(parsed.get("end_handle") or ""),
            "wire_size": str(parsed.get("wire_size") or "2.5SQ"),
            "voltage": int(_safe_float(parsed.get("voltage", 220))),
        }

    if action_type == "CONNECT_DEVICE":
        return {
            "type": "connect_device",
            "device_handle": str(parsed.get("device_handle") or ""),
            "panel_handle": str(parsed.get("panel_handle") or ""),
            "circuit": str(parsed.get("circuit") or "L1"),
        }

    if action_type == "REPLACE_WIRE_SIZE":
        new_layer = str(parsed.get("new_layer") or "").strip()
        if not new_layer:
            return None
        return {
            "type": "replace_wire_size",
            "target_handle": str(parsed.get("target_handle") or ""),
            "new_layer": new_layer,
        }

    if action_type == "CLEANUP_DUPLICATE":
        return {
            "type": "cleanup_duplicate",
            "keep_handle": str(parsed.get("keep_handle") or ""),
            "remove_handle": str(parsed.get("remove_handle") or ""),
        }

    return None


def _extract_available_blocks(entity_by_handle: dict[str, dict]) -> list[str]:
    """도면 엔티티에서 실제 존재하는 블록 정의명 목록을 반환한다."""
    seen: set[str] = set()
    result: list[str] = []
    for e in entity_by_handle.values():
        if not isinstance(e, dict):
            continue
        if e.get("type") not in ("INSERT", "BlockReference", "BLOCK", "INSERT_BLOCK"):
            continue
        name = str(e.get("block_name") or e.get("effective_name") or "").strip()
        if name and name not in seen:
            seen.add(name)
            result.append(name)
        if len(result) >= 40:
            break
    return result


def _match_block_name(requested: str, available: list[str]) -> str | None:
    """요청된 블록명과 가장 유사한 실제 블록명을 반환한다. 없으면 None."""
    if not available or not requested:
        return None
    req_up = requested.upper()
    # 1. 완전 일치
    for name in available:
        if name.upper() == req_up:
            return name
    # 2. 포함 관계
    for name in available:
        if req_up in name.upper() or name.upper() in req_up:
            return name
    return None


class DrawCommandParser:
    """Build DrawingPatcher-compatible payloads from short direct CAD commands."""

    @staticmethod
    def _entity_center(entity: dict[str, Any]) -> tuple[float, float]:
        bbox = entity.get("bbox") or {}
        if bbox:
            return (
                (float(bbox.get("x1", 0)) + float(bbox.get("x2", 0))) / 2.0,
                (float(bbox.get("y1", 0)) + float(bbox.get("y2", 0))) / 2.0,
            )
        pos = entity.get("position") or {}
        if pos:
            return float(pos.get("x", 0)), float(pos.get("y", 0))
        start = entity.get("start") or {}
        end = entity.get("end") or {}
        if start and end:
            return (
                (float(start.get("x", 0)) + float(end.get("x", 0))) / 2.0,
                (float(start.get("y", 0)) + float(end.get("y", 0))) / 2.0,
            )
        return 0.0, 0.0

    async def parse(
        self,
        user_text: str,
        active_handles: list[str],
        entity_by_handle: dict[str, dict],
        view_center: "tuple[float, float] | None" = None,
    ) -> dict | list | None:
        has_draw_kw = any(kw in user_text for kw in _DRAW_KEYWORDS)
        has_modify_kw = any(kw in user_text for kw in _MODIFY_KEYWORDS)
        if not has_draw_kw and not has_modify_kw:
            return None

        available_blocks = _extract_available_blocks(entity_by_handle)
        block_hint = (
            f"\n\n[이 도면에 정의된 블록 목록 — 블록 삽입 시 반드시 이 중에서 선택]\n"
            + ", ".join(available_blocks)
            if available_blocks else ""
        )
        system_prompt = _COMMAND_PARSE_SYSTEM + block_hint

        try:
            parsed = await generate_answer(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text},
                ],
                response_format={"type": "json_object"},
                temperature=0.0,
            )
            if isinstance(parsed, str):
                parsed = json.loads(parsed)
        except Exception as exc:
            logging.debug("[DrawCommandParser] LLM parse failed: %s", exc)
            return None

        if not isinstance(parsed, dict):
            return None

        command_type = str(parsed.get("command_type") or "none").lower()

        if command_type == "modify":
            auto_fix = _build_modify_auto_fix(parsed)
            if auto_fix is None:
                logging.debug("[DrawCommandParser] modify parsed but auto_fix unavailable: %s", parsed)
                return None

            if not active_handles:
                return {
                    "no_selection": True,
                    "message": (
                        "수정할 객체를 AutoCAD에서 먼저 선택하신 뒤 다시 요청해주세요. "
                        f"(요청: {parsed.get('action_type', '?')} 변경)"
                    ),
                }

            result_list: list[dict[str, Any]] = []
            for handle in active_handles:
                fix_payload = dict(auto_fix)
                fix_payload["_handle"] = str(handle)
                result_list.append(fix_payload)

            logging.info(
                "[DrawCommandParser] modify: action=%s handles=%d",
                auto_fix.get("type"),
                len(result_list),
            )
            return result_list

        if command_type == "draw":
            # 기준 좌표: 1) 선택 객체 중심  2) 뷰포트 중심  3) 도면 무게중심  4) 원점
            base_x, base_y = 0.0, 0.0
            found_base = False
            for handle in active_handles or []:
                ent = entity_by_handle.get(str(handle))
                if ent:
                    base_x, base_y = self._entity_center(ent)
                    found_base = True
                    break
            if not found_base and view_center is not None:
                base_x, base_y = view_center
                found_base = True
            if not found_base and entity_by_handle:
                xs, ys = [], []
                for e in list(entity_by_handle.values())[:50]:
                    if not isinstance(e, dict):
                        continue
                    cx, cy = self._entity_center(e)
                    if cx != 0.0 or cy != 0.0:
                        xs.append(cx); ys.append(cy)
                if xs:
                    base_x, base_y = sum(xs) / len(xs), sum(ys) / len(ys)

            shape = str(parsed.get("shape", "circle")).lower()
            direction = str(parsed.get("direction", "right")).lower()
            distance = _safe_float(parsed.get("distance_mm", 500))
            size = _safe_float(parsed.get("size_mm", 200))
            width = _safe_float(parsed.get("width_mm", size)) or size
            height = _safe_float(parsed.get("height_mm", size)) or size
            layer = str(parsed.get("draw_layer") or "AI_PROPOSAL")
            block_name = parsed.get("block_name")

            # 색상 (ACI 인덱스) — LLM 결과 우선, 없으면 텍스트에서 직접 추출
            raw_color = parsed.get("new_color")
            new_color: int | None = None
            if raw_color is not None:
                try:
                    new_color = int(float(str(raw_color)))
                except (TypeError, ValueError):
                    pass
            if new_color is None:
                new_color = _extract_color_from_text(user_text)

            dx, dy = _DIRECTION_VECTOR.get(direction, (1.0, 0.0))
            cx = base_x + dx * distance
            cy = base_y + dy * distance

            logging.info(
                "[DrawCommandParser] draw: shape=%s dir=%s dist=%.0f size=%.0f "
                "base=(%.0f,%.0f) center=(%.0f,%.0f) layer=%s color=%s",
                shape,
                direction,
                distance,
                size,
                base_x,
                base_y,
                cx,
                cy,
                layer,
                new_color,
            )

            def _with_color(payload: dict) -> dict:
                if new_color is not None:
                    payload["new_color"] = new_color
                return payload

            if shape == "circle":
                return _with_color({
                    "type": "CREATE_ENTITY",
                    "new_center": {"x": cx, "y": cy},
                    "new_radius": size,
                    "new_layer": layer,
                })

            if shape == "line":
                return _with_color({
                    "type": "CREATE_ENTITY",
                    "new_start": {"x": cx, "y": cy},
                    "new_end": {"x": cx + dx * size, "y": cy + dy * size},
                    "new_layer": layer,
                })

            if shape in ("rectangle", "polyline"):
                half_w = width / 2.0
                half_h = height / 2.0
                return _with_color({
                    "type": "CREATE_ENTITY",
                    "new_vertices": [
                        {"x": cx - half_w, "y": cy - half_h, "bulge": 0},
                        {"x": cx + half_w, "y": cy - half_h, "bulge": 0},
                        {"x": cx + half_w, "y": cy + half_h, "bulge": 0},
                        {"x": cx - half_w, "y": cy + half_h, "bulge": 0},
                    ],
                    "new_layer": layer,
                })

            if shape == "arc":
                start_a = _safe_float(parsed.get("start_angle_deg", 0))
                end_a = _safe_float(parsed.get("end_angle_deg", 270))
                return _with_color({
                    "type": "CREATE_ENTITY",
                    "new_center": {"x": cx, "y": cy},
                    "new_radius": size,
                    "new_start_angle": start_a,
                    "new_end_angle": end_a,
                    "new_layer": layer,
                })

            if shape == "ellipse":
                semi_minor = _safe_float(parsed.get("semi_minor_mm", 0)) or (size * 0.5)
                rotation = _safe_float(parsed.get("rotation_deg", 0))
                return _with_color({
                    "type": "CREATE_ENTITY",
                    "new_center": {"x": cx, "y": cy},
                    "new_semi_major": size,
                    "new_semi_minor": semi_minor,
                    "new_rotation": rotation,
                    "new_layer": layer,
                })

            if shape == "text":
                text_content = str(parsed.get("text_content") or parsed.get("new_text") or "TEXT")
                text_height = _safe_float(parsed.get("text_height_mm", 150)) or 150.0
                return {
                    "type": "CREATE_ENTITY",
                    "new_text": text_content,
                    "base_x": cx,
                    "base_y": cy,
                    "new_height": text_height,
                    "new_layer": layer,
                }

            if shape == "block" and block_name:
                matched = _match_block_name(block_name, available_blocks)
                if matched:
                    return _with_color({
                        "type": "CREATE_ENTITY",
                        "new_block_name": matched,
                        "base_x": cx,
                        "base_y": cy,
                        "new_layer": layer,
                    })
                logging.warning(
                    "[DrawCommandParser] block '%s' not found in drawing (available: %s) → circle fallback",
                    block_name,
                    available_blocks[:5],
                )
                return _with_color({
                    "type": "CREATE_ENTITY",
                    "new_center": {"x": cx, "y": cy},
                    "new_radius": size,
                    "new_layer": layer,
                })

            logging.warning("[DrawCommandParser] unknown shape=%s; using circle fallback", shape)
            return _with_color({
                "type": "CREATE_ENTITY",
                "new_center": {"x": cx, "y": cy},
                "new_radius": size,
                "new_layer": layer,
            })

        return None


__all__ = [
    "DrawCommandParser",
    "_AUTO_FIX_SCHEMA",
    "_COMMAND_PARSE_SYSTEM",
    "_DIRECTION_VECTOR",
    "_DRAW_KEYWORDS",
    "_MODIFY_KEYWORDS",
    "_MOVE_GUIDE",
    "_TYPE_FIX_GUIDE",
    "_build_modify_auto_fix",
    "_safe_float",
    "_extract_color_from_text",
    "_COLOR_KEYWORD_ACI",
]
