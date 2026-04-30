"""
File    : backend/services/agents/electric/sub/action.py
Author  : 김지우
Create  : 2026-04-23
Description : 선택된 객체를 전체 도면·법규와 비교 분석해 수정 명령을 생성합니다. (전기 도메인)
"""

import json
import logging
from backend.services.llm_service import generate_answer
from backend.services.agents.elec.sub.review.parser import ParserAgent

# ── 객체 타입별 허용 fix 유형 안내 (LLM 판단 기준) ───────────────────────────
_TYPE_FIX_GUIDE = """\
[객체 타입별 수정 타입 제약]
▸ BLOCK / INSERT (설비 심볼)
    - ATTRIBUTE : 블록 속성 변경 (attribute_tag + new_value)
    - LAYER     : 레이어 변경
    - MOVE / ROTATE / SCALE / DELETE : 위치·배율·삭제
▸ LINE / POLYLINE / LWPOLYLINE (전선·배관 등 선 객체)
    - LAYER     : 전선 규격·회로 구분은 레이어로 표현 ← 전선 굵기 변경 시 반드시 이 방법 사용
    - COLOR / LINETYPE / LINEWEIGHT : 시각 표현 변경
    - MOVE / ROTATE / SCALE / DELETE
    ❌ ATTRIBUTE 불가 — 선 객체에는 블록 속성이 없음
▸ TEXT / MTEXT (주석·태그 텍스트)
    - TEXT_CONTENT : 텍스트 내용 변경  (new_text)
    - TEXT_HEIGHT  : 글자 크기 변경    (new_height)
    - LAYER / COLOR / MOVE / DELETE
    ❌ ATTRIBUTE 불가
▸ CIRCLE / ARC / ELLIPSE
    - LAYER / COLOR / LINETYPE / LINEWEIGHT / MOVE / DELETE
    - GEOMETRY : 원 중심·반지름·각도 변경 (좌표 계산 필요)
    ❌ ATTRIBUTE 불가

[전선 레이어 명명 규칙 — LAYER fix 사용 시]
  Cable_1.5SQ   Cable_2.5SQ   Cable_4.0SQ   Cable_6.0SQ
  Cable_10SQ    Cable_16SQ    Cable_25SQ    Cable_35SQ
  전압 구분이 필요하면: Cable_2.5SQ_220V, Cable_4.0SQ_380V 형식 사용"""

_MOVE_GUIDE = """\
[이동(MOVE) 규칙 — 반드시 준수]
- 사용자가 '~만큼 이동' 또는 '왼쪽/오른쪽/위/아래로 이동'을 요청하면:
  → auto_fix.type = "MOVE", delta_x = 수평이동량(mm), delta_y = 수직이동량(mm)
  → delta 값은 반드시 숫자(float)로 기입. 오른쪽→양수 X, 위→양수 Y
  ✅ 올바른 예: {"type": "MOVE", "delta_x": 500.0, "delta_y": 0.0}
  ❌ 잘못된 예: {"type": "GEOMETRY", ...}  ← 절대 사용 금지 (기존 형상 파괴 위험)
- 이격거리 보정 등 정확한 좌표를 계산할 수 없을 때는 delta를 0으로 두고
  reason에 "수동 이동 필요 — 이격거리 XXmm 확보 요망"을 기재하세요."""

_AUTO_FIX_SCHEMA = """\
[auto_fix 완전한 스키마 — 필드 누락 시 수정이 동작하지 않음]
  LAYER        : {"type": "LAYER",         "new_layer": "레이어명"}
  MOVE         : {"type": "MOVE",          "delta_x": 수평mm, "delta_y": 수직mm}
  ATTRIBUTE    : {"type": "ATTRIBUTE",     "attribute_tag": "태그명", "new_value": "새값"}
  TEXT_CONTENT : {"type": "TEXT_CONTENT",  "new_text": "내용"}
  TEXT_HEIGHT  : {"type": "TEXT_HEIGHT",   "new_height": 높이숫자}
  COLOR        : {"type": "COLOR",         "new_color": ACI정수} ← 1=빨강 2=노랑 3=초록 4=청록 5=파랑
  LINETYPE     : {"type": "LINETYPE",      "new_linetype": "CONTINUOUS"}
  LINEWEIGHT   : {"type": "LINEWEIGHT",    "new_lineweight": mm숫자} ← 예: 0.25
  ROTATE       : {"type": "ROTATE",        "angle": 도, "base_x": X, "base_y": Y}
  SCALE        : {"type": "SCALE",         "scale_x": 배율, "base_x": X, "base_y": Y}
  DELETE       : {"type": "DELETE"}
  GEOMETRY     : {"type": "GEOMETRY",      ...} ← 좌표가 확실할 때만 사용
  RECTANGLE_RESIZE:
    {"type":"RECTANGLE_RESIZE","stretch_side":"right|left|top|bottom","new_width":가로mm,"new_height":세로mm}
    ※닫힌 사각형 polyline 전용. 독립 LINE 4개로 폭/높이를 바꾸려면 각 선의 GEOMETRY를 별도 수정해야 함.
  CREATE_ENTITY: 새 도형/선 추가 (기존 객체 수정 아님! 완전히 새 객체 생성)
    원(Circle)    : {"type":"CREATE_ENTITY","new_center":{"x":X,"y":Y},"new_radius":반지름,"new_layer":"레이어명"}
    선(Line)      : {"type":"CREATE_ENTITY","new_start":{"x":X1,"y":Y1},"new_end":{"x":X2,"y":Y2},"new_layer":"레이어명"}
    폴리선/사각형  : {"type":"CREATE_ENTITY","new_vertices":[{"x":X,"y":Y,"bulge":0},...(4점)],"new_layer":"레이어명"}
    블록 삽입     : {"type":"CREATE_ENTITY","new_block_name":"블록명","base_x":X,"base_y":Y,"new_layer":"레이어명"}
  CREATE_TEXT   : {"type":"CREATE_TEXT","new_text":"표기","base_x":X,"base_y":Y,"new_height":글자높이,"new_layer":"레이어명"}"""


class ActionAgent:
    def __init__(self):
        self.parser = ParserAgent()

    async def analyze_and_fix(self, context: dict, domain: str = "elec") -> dict:
        active_ids = set(context.get("active_object_ids") or [])
        raw_layout = context.get("raw_layout_data") or "{}"
        # mapping_table이 None/{} 인 경우(action Fast Path) → parser가 raw entities 그대로 사용
        mapping_table = context.get("mapping_table") or None

        parsed_result = self.parser.parse(raw_layout, mapping_table=mapping_table)
        all_elements = parsed_result.get("elements") or []

        drawing_data = context.get("drawing_data") or {}
        is_focus_mode = drawing_data.get("context_mode") == "full_with_focus"

        if active_ids:
            selected    = [e for e in all_elements if e.get("handle") in active_ids]
            surrounding = [e for e in all_elements if e.get("handle") not in active_ids]
        elif is_focus_mode:
            selected    = all_elements
            surrounding = []
        else:
            selected    = all_elements
            surrounding = []

        if not selected:
            logging.info("[ActionAgent] 분석 대상 없음")
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

        domain_hint = "전기설비기술기준(KEC) 및 내선규정"

        system_prompt = f"""당신은 {domain_hint} 전문 CAD 도면 수정 엔지니어입니다.
[검토 대상 객체]를 분석하고, 수정이 필요한 항목에 대해 구체적인 수정 명령을 JSON으로 생성하세요.

━━━ 절대 규칙 ━━━
1. handle 값은 입력 데이터 그대로 복사하세요. 절대 바꾸지 마세요.
   (이 값으로 C# 플러그인이 AutoCAD 객체를 찾습니다.)
2. 수정이 불필요한 객체는 fixes에 포함하지 마세요.
3. auto_fix는 반드시 모든 필수 필드를 포함해야 합니다. 누락 시 수정이 무시됩니다.

{_TYPE_FIX_GUIDE}

{_MOVE_GUIDE}

{_AUTO_FIX_SCHEMA}

[출력 JSON 스키마]
{{
  "analysis": "객체별 분석 요약 (한국어, 마크다운 사용 가능)",
  "fixes": [
    {{
      "handle":   "원본 handle 값 그대로",
      "type":     "객체의 raw_type",
      "reason":   "수정 이유 (한국어)",
      "action":   "LAYER | MOVE | ATTRIBUTE | TEXT_CONTENT | COLOR | ...",
      "auto_fix": {{ /* 위 스키마에서 해당 타입의 완전한 객체 */ }}
    }}
  ],
  "message": "사용자에게 보여줄 요약 메시지 (한국어)"
}}"""

        user_instr = (context.get("user_request") or "").strip()
        user_block = f"[사용자가 요청한 작업 — 반드시 반영]\n{user_instr}\n\n" if user_instr else ""

        user_prompt = (
            f"{user_block}"
            f"[검토 대상 객체 ({len(selected)}개)]:\n"
            f"{json.dumps(selected, ensure_ascii=False, indent=2)}\n\n"
            f"[주변 참고 컨텍스트]:\n"
            f"{json.dumps(surrounding[:8], ensure_ascii=False, indent=2) if surrounding else '없음'}\n\n"
            f"[{domain_hint}]:\n{law_text}\n\n"
            "대상 객체의 타입(raw_type)을 확인하고, 해당 타입에 맞는 fix 유형을 선택하여 수정 명령을 JSON으로 출력하세요."
        )

        try:
            result = await generate_answer(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_prompt},
                ],
                response_format={"type": "json_object"},
            )
            if not isinstance(result, dict):
                result = json.loads(result) if isinstance(result, str) else {}

            # auto_fix 후처리: delta_x/delta_y 타입 보정 (LLM이 문자열로 줄 때)
            fixes = result.get("fixes") or []
            for fix in fixes:
                af = fix.get("auto_fix") or {}
                if isinstance(af, dict) and af.get("type") == "MOVE":
                    af["delta_x"] = _safe_float(af.get("delta_x", 0))
                    af["delta_y"] = _safe_float(af.get("delta_y", 0))
                    fix["auto_fix"] = af
                if isinstance(af, dict) and af.get("type") == "COLOR":
                    nc = af.get("new_color")
                    if nc is not None:
                        af["new_color"] = int(float(str(nc)))
                        fix["auto_fix"] = af

            result["fixes"] = fixes
            result.setdefault("analysis", "분석 완료")
            result.setdefault(
                "message",
                f"선택 객체 {len(selected)}개 분석 완료. {len(fixes)}개 수정 필요."
            )

            logging.info(
                "[ActionAgent] 분석 완료: selected=%d fixes=%d handles=%s",
                len(selected),
                len(fixes),
                [f.get("handle") for f in fixes[:5]],
            )
            return result

        except Exception as e:
            logging.error("[ActionAgent] LLM 분석 실패: %s", e)
            return {
                "analysis": "분석 중 오류 발생",
                "fixes": [],
                "message": f"수정 분석 중 오류가 발생했습니다: {e}"
            }


def _safe_float(value) -> float:
    """문자열·None 등 다양한 입력을 float으로 변환."""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# DrawCommandParser
# 사용자의 자연어 채팅 명령에서 CAD 직접 실행 payload를 생성한다.
#
# [경로 A] 신규 도형 생성 (CREATE)
#   "선택한 객체 오른쪽 1000mm 지점에 반지름 200mm 원 그려줘"
#   → {type:"CREATE_ENTITY", new_center:{x:...,y:...}, new_radius:200, new_layer:"AI_PROPOSAL"}
#   → CAD_ACTION 1건 전송
#
# [경로 B] 기존 객체 속성 수정 (MODIFY)
#   "선택한 선의 레이어를 GAS로 바꿔줘" / "선 굵기 0.5mm로 변경해줘"
#   → [{_handle:"2A4F", type:"LAYER", new_layer:"GAS"}, ...]  (선택된 핸들별로)
#   → CAD_ACTION N건 전송 (handle 포함)
# ─────────────────────────────────────────────────────────────────────────────

# ── 신규 생성 키워드 ──────────────────────────────────────────────────────────
_DRAW_KEYWORDS = (
    "추가", "그려", "만들어", "생성", "그려줘", "만들어줘", "추가해줘",
    "추가해", "그려라", "만들어라",
    "add", "draw", "create", "insert",
)

# ── 속성 수정 키워드 ──────────────────────────────────────────────────────────
_MODIFY_KEYWORDS = (
    "레이어", "layer",
    "굵기", "두께", "선굵기", "lineweight",
    "색상", "색깔", "컬러", "color",
    "선종류", "선 종류", "linetype",
    "삭제해", "지워줘", "제거해줘",
    "이동해", "옮겨줘", "이동시켜",
    "회전해", "돌려줘", "회전시켜",
    "텍스트 변경", "내용 바꿔", "글자 바꿔",
    "글자 크기", "텍스트 크기", "높이 변경",
    "속성 변경", "attribute",
)

# ── LLM 시스템 프롬프트 — 생성·수정 통합 ────────────────────────────────────
_COMMAND_PARSE_SYSTEM = """\
사용자 명령에서 AutoCAD 작업 의도를 분석하십시오.
반드시 아래 JSON 스키마만 반환하십시오. 부가 설명 금지.

════════ 응답 JSON 스키마 ════════
{
  "command_type": "draw | modify | none",

  // ── [command_type == "draw"] 신규 도형 생성 ──────────────────────────
  "shape":       "circle | line | rectangle | polyline | block | none",
  "direction":   "right | left | up | down | upper_right | upper_left | lower_right | lower_left | none",
  "distance_mm": <기준 객체 중심에서 새 도형까지 오프셋(mm). 모르면 500>,
  "size_mm":     <원=반지름, 선=길이, 사각형=한 변(mm). 모르면 200>,
  "width_mm":    <rectangle 가로 길이(mm). 없으면 size_mm>,
  "height_mm":   <rectangle 세로 길이(mm). 없으면 size_mm>,
  "block_name":  "<블록 삽입 시 블록 정의명. 해당 없으면 null>",
  "draw_layer":  "<신규 도형의 레이어명. 없으면 AI_PROPOSAL>",

  // ── [command_type == "modify"] 기존 객체 속성 수정 ───────────────────
  "action_type": "LAYER | LINEWEIGHT | COLOR | LINETYPE | DELETE | MOVE | ROTATE | SCALE | TEXT_CONTENT | TEXT_HEIGHT | ATTRIBUTE | RECTANGLE_RESIZE",
  "new_layer":   "<LAYER 타입 시 변경할 레이어명. 예: GAS, P-PIPE, Cable_2.5SQ>",
  "new_lineweight": <LINEWEIGHT 타입 시 mm 단위 소수점. 예: 0.25 | 0.35 | 0.50 | 0.70>,
  "new_color":   <COLOR 타입 시 AutoCAD ACI 정수. 1=빨강 2=노랑 3=초록 4=청록 5=파랑 6=보라 7=흰색>,
  "new_linetype": "<LINETYPE 타입 시 선종류. CONTINUOUS | DASHED | CENTER | HIDDEN | DOT>",
  "delta_x":     <MOVE 타입 시 X 방향 이동량(mm). 오른쪽=양수>,
  "delta_y":     <MOVE 타입 시 Y 방향 이동량(mm). 위=양수>,
  "angle":       <ROTATE 타입 시 회전 각도(도). 반시계=양수>,
  "scale_factor":<SCALE 타입 시 배율. 2.0 = 2배 확대>,
  "stretch_side":"<RECTANGLE_RESIZE 대상 변: right | left | top | bottom. 모르면 right>",
  "new_width":   <RECTANGLE_RESIZE 목표 가로 길이(mm). 가로 변경 요청일 때 사용>,
  "new_text":    "<TEXT_CONTENT 타입 시 변경할 텍스트 내용>",
  "new_height":  <TEXT_HEIGHT 타입 시 새 글자 크기(mm), RECTANGLE_RESIZE 타입 시 목표 세로 길이(mm)>,
  "attribute_tag":"<ATTRIBUTE 타입 시 블록 속성 태그명>",
  "attribute_value":"<ATTRIBUTE 타입 시 새 속성값>"
}

════════ 판단 규칙 ════════
1. "그려줘 / 추가해줘 / 만들어줘 / 삽입해줘" → command_type = "draw"
2. 레이어·색상·굵기·두께·선종류·이동·회전·삭제·크기 변경 → command_type = "modify"
3. 그 외 (질문·검토 요청 등) → command_type = "none"
4. "직사각형/사각형/네모의 가로를 N으로", "오른쪽 변을 N만큼 늘려"처럼 연결된 사각형의 한 변을 움직이는 요청은
   command_type = "modify", action_type = "RECTANGLE_RESIZE"로 판단한다.

════════ LINEWEIGHT 변환 가이드 ════════
  "얇게" / "가늘게"  → 0.18
  "기본"            → 0.25
  "보통 굵기"        → 0.35
  "굵게"            → 0.50
  "매우 굵게"        → 0.70
  숫자 직접 언급 시 그 숫자를 mm 단위로 변환 (예: "0.5mm" → 0.50)

════════ 방향 키워드 (draw용) ════════
  오른쪽·우·east → right    왼쪽·좌·west  → left
  위·북·north    → up       아래·남·south  → down
  우상·northeast → upper_right   우하·southeast → lower_right
  좌상·northwest → upper_left    좌하·southwest → lower_left"""

_DIRECTION_VECTOR: dict[str, tuple[float, float]] = {
    "right":       ( 1.0,  0.0),
    "left":        (-1.0,  0.0),
    "up":          ( 0.0,  1.0),
    "down":        ( 0.0, -1.0),
    "upper_right": ( 0.7071,  0.7071),
    "upper_left":  (-0.7071,  0.7071),
    "lower_right": ( 0.7071, -0.7071),
    "lower_left":  (-0.7071, -0.7071),
    "none":        ( 0.0,  0.0),
}


def _build_modify_auto_fix(parsed: dict) -> dict | None:
    """
    LLM 파싱 결과(modify 타입)를 DrawingPatcher가 이해하는 auto_fix dict로 변환한다.
    """
    action_type = str(parsed.get("action_type") or "").upper()
    if not action_type:
        return None

    if action_type == "LAYER":
        new_layer = (parsed.get("new_layer") or "").strip()
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
        lt = (parsed.get("new_linetype") or "").strip().upper() or "CONTINUOUS"
        return {"type": "LINETYPE", "new_linetype": lt}

    if action_type == "DELETE":
        return {"type": "DELETE"}

    if action_type == "MOVE":
        return {
            "type":    "MOVE",
            "delta_x": _safe_float(parsed.get("delta_x", 0)),
            "delta_y": _safe_float(parsed.get("delta_y", 0)),
        }

    if action_type == "ROTATE":
        return {
            "type":   "ROTATE",
            "angle":  _safe_float(parsed.get("angle", 0)),
            "base_x": 0.0,
            "base_y": 0.0,
        }

    if action_type == "SCALE":
        sf = _safe_float(parsed.get("scale_factor", 1.0))
        return {"type": "SCALE", "scale_x": sf, "base_x": 0.0, "base_y": 0.0}

    if action_type in ("RECTANGLE_RESIZE", "STRETCH_RECT"):
        out = {
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
        new_text = parsed.get("new_text") or ""
        if not new_text:
            return None
        return {"type": "TEXT_CONTENT", "new_text": new_text}

    if action_type == "TEXT_HEIGHT":
        val = parsed.get("new_height")
        if val is None:
            return None
        return {"type": "TEXT_HEIGHT", "new_height": _safe_float(val)}

    if action_type == "ATTRIBUTE":
        tag = (parsed.get("attribute_tag") or "").strip()
        value = (parsed.get("attribute_value") or "").strip()
        if not tag:
            return None
        return {"type": "ATTRIBUTE", "attribute_tag": tag, "new_value": value}

    return None


class DrawCommandParser:
    """
    사용자의 채팅 텍스트에서 CAD 직접 실행 payload를 생성한다.

    반환값 유형:
      None                         → 해당 없음 (LangGraph ActionAgent 경로로 처리)
      {"no_selection": True, ...}  → 수정 명령이지만 선택된 객체 없음 → 경고 메시지
      dict  (단일)                 → CREATE_ENTITY (신규 생성) → CAD_ACTION 1건
      list  (복수)                 → 속성 수정 → CAD_ACTION N건 (handle 포함)

    사용 흐름:
        parser = DrawCommandParser()
        result = await parser.parse(user_text, active_handles, entity_by_handle)
        # websocket.py에서 타입별 분기 처리
    """

    @staticmethod
    def _entity_center(entity: dict) -> tuple[float, float]:
        """entity의 bbox 또는 position에서 중심 좌표를 계산한다."""
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
        end   = entity.get("end")   or {}
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
    ) -> "dict | list | None":
        """
        user_text        : 사용자 입력 문자열
        active_handles   : 현재 선택된 AutoCAD 핸들 목록
        entity_by_handle : {handle: entity_dict} 인덱스 (drawing_data["entities"])

        반환: dict(CREATE) | list(MODIFY per handle) | {"no_selection":True} | None
        """
        # ── 1단계: 키워드 빠른 체크 ──────────────────────────────────────────
        has_draw_kw   = any(kw in user_text for kw in _DRAW_KEYWORDS)
        has_modify_kw = any(kw in user_text for kw in _MODIFY_KEYWORDS)
        if not has_draw_kw and not has_modify_kw:
            return None

        # ── 2단계: LLM으로 의도 파싱 ─────────────────────────────────────────
        try:
            parsed = await generate_answer(
                messages=[
                    {"role": "system", "content": _COMMAND_PARSE_SYSTEM},
                    {"role": "user",   "content": user_text},
                ],
                response_format={"type": "json_object"},
                temperature=0.0,
            )
            if isinstance(parsed, str):
                parsed = json.loads(parsed)
        except Exception as exc:
            logging.debug("[DrawCommandParser] LLM 파싱 실패: %s", exc)
            return None

        if not isinstance(parsed, dict):
            return None

        command_type = str(parsed.get("command_type") or "none").lower()

        # ── 경로 B: 속성 수정 명령 ───────────────────────────────────────────
        if command_type == "modify":
            auto_fix = _build_modify_auto_fix(parsed)
            if auto_fix is None:
                logging.debug("[DrawCommandParser] modify 파싱됐으나 auto_fix 생성 불가: %s", parsed)
                return None

            # 선택된 객체가 없으면 경고 신호 반환
            if not active_handles:
                return {
                    "no_selection": True,
                    "message": (
                        "수정할 객체를 AutoCAD에서 먼저 선택하세요. "
                        f"(요청: {parsed.get('action_type','?')} 변경)"
                    ),
                }

            # 각 선택 핸들마다 payload 생성 (_handle 필드로 핸들 전달)
            result_list: list[dict] = []
            for handle in active_handles:
                fix_payload = dict(auto_fix)
                fix_payload["_handle"] = str(handle)
                result_list.append(fix_payload)

            logging.info(
                "[DrawCommandParser] modify: action=%s handles=%d new_value=%s",
                auto_fix.get("type"),
                len(result_list),
                auto_fix.get("new_layer") or auto_fix.get("new_lineweight")
                or auto_fix.get("new_color") or auto_fix.get("new_linetype")
                or auto_fix.get("new_text") or "(no value)",
            )
            return result_list

        # ── 경로 A: 신규 도형 생성 ───────────────────────────────────────────
        if command_type == "draw":
            # 선택된 객체의 중심 좌표 계산 (없으면 원점 기준)
            base_x, base_y = 0.0, 0.0
            for handle in (active_handles or []):
                ent = entity_by_handle.get(str(handle))
                if ent:
                    base_x, base_y = self._entity_center(ent)
                    break

            shape      = str(parsed.get("shape", "circle")).lower()
            direction  = str(parsed.get("direction", "right")).lower()
            distance   = _safe_float(parsed.get("distance_mm", 500))
            size       = _safe_float(parsed.get("size_mm", 200))
            width      = _safe_float(parsed.get("width_mm", size)) or size
            height     = _safe_float(parsed.get("height_mm", size)) or size
            layer      = str(parsed.get("draw_layer") or "AI_PROPOSAL")
            block_name = parsed.get("block_name")

            dx, dy = _DIRECTION_VECTOR.get(direction, (1.0, 0.0))
            cx = base_x + dx * distance
            cy = base_y + dy * distance

            logging.info(
                "[DrawCommandParser] draw: shape=%s dir=%s dist=%.0f size=%.0f "
                "base=(%.0f,%.0f) → center=(%.0f,%.0f) layer=%s",
                shape, direction, distance, size, base_x, base_y, cx, cy, layer,
            )

            if shape == "circle":
                return {"type": "CREATE_ENTITY", "new_center": {"x": cx, "y": cy},
                        "new_radius": size, "new_layer": layer}

            if shape == "line":
                return {"type": "CREATE_ENTITY",
                        "new_start": {"x": cx, "y": cy},
                        "new_end":   {"x": cx + dx * size, "y": cy + dy * size},
                        "new_layer": layer}

            if shape in ("rectangle", "polyline"):
                half_w = width / 2.0
                half_h = height / 2.0
                return {"type": "CREATE_ENTITY",
                        "new_vertices": [
                            {"x": cx - half_w, "y": cy - half_h, "bulge": 0},
                            {"x": cx + half_w, "y": cy - half_h, "bulge": 0},
                            {"x": cx + half_w, "y": cy + half_h, "bulge": 0},
                            {"x": cx - half_w, "y": cy + half_h, "bulge": 0},
                        ],
                        "new_layer": layer}

            if shape == "block" and block_name:
                return {"type": "CREATE_ENTITY", "new_block_name": block_name,
                        "base_x": cx, "base_y": cy, "new_layer": layer}

            # fallback: 원
            logging.warning("[DrawCommandParser] 알 수 없는 shape=%s → circle 기본 생성", shape)
            return {"type": "CREATE_ENTITY", "new_center": {"x": cx, "y": cy},
                    "new_radius": size, "new_layer": layer}

        return None
