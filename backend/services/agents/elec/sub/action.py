"""
File    : backend/services/agents/electric/sub/action.py
Author  : 김지우
Create  : 2026-04-23
Description : 선택된 객체를 전체 도면·법규와 비교 분석해 수정 명령을 생성합니다. (전기 도메인)
"""

import json
import logging
import re
from backend.services.llm_service import generate_answer
from backend.services.agents.elec.sub.review.parser import ParserAgent

# ── 전기 카테고리 분류 패턴 ────────────────────────────────────────────────────
_CAT_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("PANEL",   re.compile(r"PANEL|MDP|SDB|분전반|배전반", re.IGNORECASE)),
    ("BREAKER", re.compile(r"ELB|MCCB|MCB|차단기|BREAKER|NFB", re.IGNORECASE)),
    ("CABLE",   re.compile(r"Cable_|Wire_|CABLE|전선|배선", re.IGNORECASE)),
    ("LIGHT",   re.compile(r"E-LIGHT|조명|LAMP|LIGHT|FL|LED|CFL", re.IGNORECASE)),
    ("SWITCH",  re.compile(r"E-SWITCH|스위치|SW\b|SWITCH", re.IGNORECASE)),
    ("SOCKET",  re.compile(r"E-OUTLET|E-SOCKET|콘센트|OUTLET|SOCKET|RECEPTACLE", re.IGNORECASE)),
]

# 카테고리별 검토 규칙 (LLM 프롬프트 주입용)
_CATEGORY_RULES: dict[str, str] = {
    "PANEL":   "분전반: 차단기 용량·배선 규격·이격거리(KEC 312.3) 중심 검토",
    "BREAKER": "차단기: 정격전류·차단용량·트립특성·협조 검토 (KEC 212.5)",
    "CABLE":   "전선: 굵기(SQ)·허용전류·색상·이격거리·전압강하 검토 (KEC 212.4)",
    "LIGHT":   "조명: 배치 간격·회로 부하·비상조명 겸용 여부 검토",
    "SWITCH":  "스위치: 전선 연결·회로 분리·위치 기준 검토",
    "SOCKET":  "콘센트: 접지 여부·정격전류·방수등급·전선 굵기 검토",
    "UNKNOWN": "전기 규정 전반 검토",
}


def _classify_elec_category(entity: dict) -> str:
    """블록명/레이어명 패턴으로 전기 카테고리를 분류한다."""
    name  = str(entity.get("effective_name") or entity.get("block_name") or entity.get("name") or "")
    layer = str(entity.get("layer") or "")
    combined = f"{name} {layer}"
    for cat, pat in _CAT_PATTERNS:
        if pat.search(combined):
            return cat
    return "UNKNOWN"


_ELEC_LAYER_KW_RE = re.compile(r"(?:\ub808\uc774\uc5b4|layer)", re.IGNORECASE)
_ELEC_LAYER_SELECTION_RE = re.compile(r"(?:\uc120\ud0dd|selected)", re.IGNORECASE)
_ELEC_LAYER_PATTERNS = [
    re.compile(
        r"(?:\ub808\uc774\uc5b4|layer).{0,24}?([A-Za-z][A-Za-z0-9_-]{1,})\s*(?:\ub85c|\uc73c\ub85c|to|\ubcc0\uacbd|\ubc14\uafd4|\uad50\uccb4)",
        re.IGNORECASE,
    ),
    re.compile(r"(?:\ub808\uc774\uc5b4|layer)\s*[:=]\s*([A-Za-z][A-Za-z0-9_-]{1,})", re.IGNORECASE),
    re.compile(r"(?:to|->|=>)\s*([A-Za-z][A-Za-z0-9_-]{1,})", re.IGNORECASE),
    re.compile(r"([A-Za-z][A-Za-z0-9_-]{1,})\s*(?:\ub85c|\uc73c\ub85c)\s*(?:\ubc14\uafd4|\ubcc0\uacbd|\uad50\uccb4)", re.IGNORECASE),
]


def _extract_elec_layer_change(user_request: str) -> str | None:
    text = (user_request or "").strip()
    if not text:
        return None
    for pattern in _ELEC_LAYER_PATTERNS:
        match = pattern.search(text)
        if match:
            candidate = match.group(1).strip()
            if len(candidate) >= 2:
                return candidate
    return None


def _selected_layer_fix_for_elec(context: dict, active_ids: set[str]) -> dict | None:
    user_request = (context.get("user_request") or "").strip()
    layer_name = _extract_elec_layer_change(user_request)
    layer_kw_present = bool(_ELEC_LAYER_KW_RE.search(user_request))
    selection_mentioned = bool(_ELEC_LAYER_SELECTION_RE.search(user_request))

    if not (layer_kw_present or selection_mentioned or layer_name):
        return None
    if not active_ids:
        return {
            "analysis": "No selected objects",
            "fixes": [],
            "message": "선택된 객체가 없습니다. AutoCAD에서 객체를 선택한 뒤 다시 요청해 주세요.",
        }
    if not layer_name:
        return None

    active_ids_ordered = [str(x) for x in (context.get("active_object_ids_ordered") or []) if x]
    handles = [h for h in active_ids_ordered if h in active_ids] or sorted(active_ids)
    logging.info(
        "[ElecActionAgent] deterministic layer change layer=%s handles=%d",
        layer_name,
        len(handles),
    )
    return {
        "analysis": f"선택 객체 {len(handles)}개 레이어를 [{layer_name}]로 변경합니다.",
        "fixes": [{
            "handle": handles[0],
            "type": "MULTI_LAYER",
            "reason": f"사용자 레이어 변경 요청: {user_request}",
            "action": "LAYER",
            "auto_fix": {
                "type": "LAYER",
                "new_layer": layer_name,
                "target_handles": handles,
            },
        }],
        "message": (
            f"선택 객체 {len(handles)}개의 레이어를 [{layer_name}]로 변경합니다. "
            "수정 후보를 확인한 뒤 승인해 주세요."
        ),
    }

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
  CREATE_TEXT   : {"type":"CREATE_TEXT","new_text":"표기","base_x":X,"base_y":Y,"new_height":글자높이,"new_layer":"레이어명"}

[전기 특화 액션 — 전기 도면 수정 시 사용]
  create_wire      : {"type":"create_wire","start_handle":"핸들A","end_handle":"핸들B","wire_size":"2.5SQ","voltage":220}
  connect_device   : {"type":"connect_device","device_handle":"기기핸들","panel_handle":"분전반핸들","circuit":"L1"}
  replace_wire_size: {"type":"replace_wire_size","target_handle":"핸들","new_layer":"Cable_4.0SQ"}
  cleanup_duplicate: {"type":"cleanup_duplicate","keep_handle":"유지핸들","remove_handle":"제거핸들"}"""


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

        layer_fast_result = _selected_layer_fix_for_elec(context, active_ids)
        if layer_fast_result is not None:
            return layer_fast_result

        if not selected:
            logging.info("[ActionAgent] 분석 대상 없음")
            return {
                "analysis": "분석할 수 있는 선택 객체가 없습니다.",
                "fixes": [],
                "message": "수정할 객체를 AutoCAD에서 선택하거나 범위에 포함시킨 후 다시 시도하세요."
            }

        # ── 선택 객체 카테고리 분류 ─────────────────────────────────────────
        category_map: dict[str, str] = {}
        for e in selected:
            h = str(e.get("handle") or "")
            if h:
                category_map[h] = _classify_elec_category(e)

        # 주요 카테고리 집합 추출 (중복 제거)
        categories = set(category_map.values()) - {"UNKNOWN"}
        category_rules = "\n".join(
            f"  - {_CATEGORY_RULES.get(c, '')}" for c in sorted(categories)
        ) or f"  - {_CATEGORY_RULES['UNKNOWN']}"

        logging.info(
            "[ActionAgent] 카테고리 분류: %s",
            {c: sum(1 for v in category_map.values() if v == c) for c in set(category_map.values())}
        )
        # ──────────────────────────────────────────────────────────────────────

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

[선택 객체 카테고리 — 아래 규칙에 집중하여 검토]
{category_rules}

{_TYPE_FIX_GUIDE}

{_MOVE_GUIDE}

{_AUTO_FIX_SCHEMA}

[출력 JSON 스키마]
{{
  "analysis": "객체별 분석 요약 (한국어, 마크다운 사용 가능)",
  "selection_category": "주요 카테고리 (PANEL/BREAKER/CABLE/LIGHT/SWITCH/SOCKET/MIXED)",
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
                f"선택하신 {len(selected)}개 객체를 살펴봤고, 그중 {len(fixes)}개 수정 항목을 찾았습니다."
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
# ── 색상 키워드 → AutoCAD ACI 인덱스 ─────────────────────────────────────────
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
    """사용자 텍스트에서 색상 키워드를 찾아 ACI 인덱스를 반환한다."""
    lower = text.lower()
    for kw, aci in _COLOR_KEYWORD_ACI.items():
        if kw in lower:
            return aci
    return None


_DRAW_KEYWORDS = (
    "추가", "그려", "만들어", "생성", "그려줘", "만들어줘", "추가해줘",
    "추가해", "그려라", "만들어라", "생성해줘", "생성해", "생성하",
    "호", "아크", "타원", "텍스트", "글자", "문자",
    "add", "draw", "create", "insert", "arc", "ellipse",
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
  "shape":       "circle | line | rectangle | polyline | arc | ellipse | text | block | none",
  "direction":   "right | left | up | down | upper_right | upper_left | lower_right | lower_left | none",
  "distance_mm": <기준 객체 중심에서 새 도형까지 오프셋(mm). 모르면 500>,
  "size_mm":     <원=반지름, 선=길이, 사각형=한 변, 호=반지름, 타원=반장축(mm). 모르면 200>,
  "width_mm":    <rectangle 가로 길이(mm). 없으면 size_mm>,
  "height_mm":   <rectangle 세로 길이(mm). 없으면 size_mm>,
  "start_angle_deg": <arc 시작 각도(도). 없으면 0>,
  "end_angle_deg":   <arc 끝 각도(도). 없으면 270>,
  "semi_minor_mm":   <ellipse 반단축 길이(mm). 없으면 size_mm의 절반>,
  "rotation_deg":    <ellipse 회전 각도(도). 없으면 0>,
  "text_content":    "<text shape일 때 삽입할 텍스트 내용>",
  "text_height_mm":  <text shape일 때 글자 높이(mm). 없으면 150>,
  "block_name":  "<블록 삽입 시 블록 정의명. 해당 없으면 null>",
  "draw_layer":  "<신규 도형의 레이어명. 없으면 AI_PROPOSAL>",
  "new_color":   <draw 시 색상 지정. AutoCAD ACI 정수. 1=빨강 2=노랑 3=초록 4=청록 5=파랑 6=보라 7=흰색 8=회색. 색상 미지정이면 생략>,

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
1. "그려줘 / 그려 / 추가해줘 / 추가해 / 만들어줘 / 만들어 / 생성해줘 / 생성해 / 삽입해줘 / 삽입" → command_type = "draw"
   - 원/circle → shape="circle", size_mm=반지름
   - 선/line   → shape="line",   size_mm=길이
   - 사각형/rectangle → shape="rectangle", width_mm, height_mm
   - 호/arc    → shape="arc",    size_mm=반지름, start_angle_deg, end_angle_deg
   - 타원/ellipse → shape="ellipse", size_mm=반장축, semi_minor_mm=반단축
   - 텍스트/글자/문자 → shape="text", text_content=내용, text_height_mm=글자높이
   draw 요청에 색상(빨간색, 파란색 등)이 포함되면 반드시 new_color 필드를 포함할 것.
   예: "빨간색 원 그려줘" → command_type="draw", shape="circle", new_color=1
   예: "파란색 선 생성해줘" → command_type="draw", shape="line", new_color=5
   예: "초록색 사각형 추가해줘" → command_type="draw", shape="rectangle", new_color=3
   예: "반지름 300인 호 그려줘" → command_type="draw", shape="arc", size_mm=300, start_angle_deg=0, end_angle_deg=270
   예: "반장축 400 타원 그려줘" → command_type="draw", shape="ellipse", size_mm=400, semi_minor_mm=200
   예: "안녕하세요 텍스트 삽입" → command_type="draw", shape="text", text_content="안녕하세요", text_height_mm=150
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

    # ── 전기 특화 액션 ──────────────────────────────────────────────────────
    if action_type == "CREATE_WIRE":
        return {
            "type":         "create_wire",
            "start_handle": str(parsed.get("start_handle") or ""),
            "end_handle":   str(parsed.get("end_handle") or ""),
            "wire_size":    str(parsed.get("wire_size") or "2.5SQ"),
            "voltage":      int(_safe_float(parsed.get("voltage", 220))),
        }

    if action_type == "CONNECT_DEVICE":
        return {
            "type":          "connect_device",
            "device_handle": str(parsed.get("device_handle") or ""),
            "panel_handle":  str(parsed.get("panel_handle") or ""),
            "circuit":       str(parsed.get("circuit") or "L1"),
        }

    if action_type == "REPLACE_WIRE_SIZE":
        new_layer = str(parsed.get("new_layer") or "").strip()
        if not new_layer:
            return None
        return {
            "type":          "replace_wire_size",
            "target_handle": str(parsed.get("target_handle") or ""),
            "new_layer":     new_layer,
        }

    if action_type == "CLEANUP_DUPLICATE":
        return {
            "type":          "cleanup_duplicate",
            "keep_handle":   str(parsed.get("keep_handle") or ""),
            "remove_handle": str(parsed.get("remove_handle") or ""),
        }

    return None


def _extract_available_blocks(entity_by_handle: dict) -> list[str]:
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
    for name in available:
        if name.upper() == req_up:
            return name
    for name in available:
        if req_up in name.upper() or name.upper() in req_up:
            return name
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
        view_center: "tuple[float, float] | None" = None,
    ) -> "dict | list | None":
        """
        user_text        : 사용자 입력 문자열
        active_handles   : 현재 선택된 AutoCAD 핸들 목록
        entity_by_handle : {handle: entity_dict} 인덱스 (drawing_data["entities"])
        view_center      : 현재 뷰포트 중심 좌표 (선택 객체 없을 때 기준점)

        반환: dict(CREATE) | list(MODIFY per handle) | {"no_selection":True} | None
        """
        # ── 1단계: 키워드 빠른 체크 ──────────────────────────────────────────
        has_draw_kw   = any(kw in user_text for kw in _DRAW_KEYWORDS)
        has_modify_kw = any(kw in user_text for kw in _MODIFY_KEYWORDS)
        if not has_draw_kw and not has_modify_kw:
            return None

        # ── 2단계: LLM으로 의도 파싱 ─────────────────────────────────────────
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
            # 기준 좌표: 1) 선택 객체 중심  2) 뷰포트 중심  3) 도면 무게중심  4) 원점
            base_x, base_y = 0.0, 0.0
            found_base = False
            for handle in (active_handles or []):
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

            shape      = str(parsed.get("shape", "circle")).lower()
            direction  = str(parsed.get("direction", "right")).lower()
            distance   = _safe_float(parsed.get("distance_mm", 500))
            size       = _safe_float(parsed.get("size_mm", 200))
            width      = _safe_float(parsed.get("width_mm", size)) or size
            height     = _safe_float(parsed.get("height_mm", size)) or size
            layer      = str(parsed.get("draw_layer") or "AI_PROPOSAL")
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
                "base=(%.0f,%.0f) → center=(%.0f,%.0f) layer=%s color=%s",
                shape, direction, distance, size, base_x, base_y, cx, cy, layer, new_color,
            )

            def _with_color(payload: dict) -> dict:
                if new_color is not None:
                    payload["new_color"] = new_color
                return payload

            if shape == "circle":
                return _with_color({"type": "CREATE_ENTITY", "new_center": {"x": cx, "y": cy},
                                    "new_radius": size, "new_layer": layer})

            if shape == "line":
                return _with_color({"type": "CREATE_ENTITY",
                                    "new_start": {"x": cx, "y": cy},
                                    "new_end":   {"x": cx + dx * size, "y": cy + dy * size},
                                    "new_layer": layer})

            if shape in ("rectangle", "polyline"):
                half_w = width / 2.0
                half_h = height / 2.0
                return _with_color({"type": "CREATE_ENTITY",
                                    "new_vertices": [
                                        {"x": cx - half_w, "y": cy - half_h, "bulge": 0},
                                        {"x": cx + half_w, "y": cy - half_h, "bulge": 0},
                                        {"x": cx + half_w, "y": cy + half_h, "bulge": 0},
                                        {"x": cx - half_w, "y": cy + half_h, "bulge": 0},
                                    ],
                                    "new_layer": layer})

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
                    return _with_color({"type": "CREATE_ENTITY", "new_block_name": matched,
                                        "base_x": cx, "base_y": cy, "new_layer": layer})
                logging.warning(
                    "[DrawCommandParser] 블록 '%s' 도면에 없음 (available: %s) → circle fallback",
                    block_name, available_blocks[:5],
                )
                return _with_color({"type": "CREATE_ENTITY", "new_center": {"x": cx, "y": cy},
                                    "new_radius": size, "new_layer": layer})

            # fallback: 원
            logging.warning("[DrawCommandParser] 알 수 없는 shape=%s → circle 기본 생성", shape)
            return _with_color({"type": "CREATE_ENTITY", "new_center": {"x": cx, "y": cy},
                                "new_radius": size, "new_layer": layer})

        return None


# ── 전기 전용 다중 객체 생성 배치 프롬프트 ────────────────────────────────────
_ELEC_BATCH_SYSTEM = """\
전기 설비 CAD 배치 설계 에이전트입니다.
사용자 요청을 분석하여 전기 객체와 배선을 JSON 배열로 생성하세요.

[좌표 규칙]
- base_x, base_y를 중심으로 객체를 배치하세요.
- 설비 간격: 최소 1500mm
- 전선은 설비 중심을 연결하는 직선(line)으로 표현

══════════════════════════════════════════════════════════════
[전기 조명 심벌 표현] — R = 사용자 지정 반지름, 없으면 기본값 사용
──────────────────────────────────────────────────────────────
각 심벌은 여러 개의 CREATE_ENTITY 객체로 구성됩니다.
N개를 그릴 경우, 아래 패턴을 N번 반복하되 X 좌표만 step씩 이동.

■ 전구 일반 / 백열등 (원 + X 대각선 2개) — 기본 R=300, D=R*0.707, layer E-LIGHT
  ※ R=300 → D=212,  R=1000 → D=707
  원:       {"type":"CREATE_ENTITY","new_center":{"x":X,"y":Y},"new_radius":R,"new_layer":"E-LIGHT"}
  대각1:    {"type":"CREATE_ENTITY","new_start":{"x":X-D,"y":Y-D},"new_end":{"x":X+D,"y":Y+D},"new_layer":"E-LIGHT"}
  대각2:    {"type":"CREATE_ENTITY","new_start":{"x":X+D,"y":Y-D},"new_end":{"x":X-D,"y":Y+D},"new_layer":"E-LIGHT"}
  예(R=300, X=5000, Y=5000): D=212
    원:     {"type":"CREATE_ENTITY","new_center":{"x":5000,"y":5000},"new_radius":300,"new_layer":"E-LIGHT"}
    대각1:  {"type":"CREATE_ENTITY","new_start":{"x":4788,"y":4788},"new_end":{"x":5212,"y":5212},"new_layer":"E-LIGHT"}
    대각2:  {"type":"CREATE_ENTITY","new_start":{"x":5212,"y":4788},"new_end":{"x":4788,"y":5212},"new_layer":"E-LIGHT"}

■ 형광등 (등기구 직사각형 + 내부 형광관선) — 기본 R=300, L=R*4, W=R*1.0, layer E-LIGHT-FL
  외곽 박스:  {"type":"CREATE_ENTITY","new_vertices":[
    {"x":X-L/2,"y":Y-W/2,"bulge":0},{"x":X+L/2,"y":Y-W/2,"bulge":0},
    {"x":X+L/2,"y":Y+W/2,"bulge":0},{"x":X-L/2,"y":Y+W/2,"bulge":0}
  ],"new_layer":"E-LIGHT-FL"}
  내부 형광관: {"type":"CREATE_ENTITY","new_start":{"x":X-L/2+50,"y":Y},"new_end":{"x":X+L/2-50,"y":Y},"new_layer":"E-LIGHT-FL"}
  ※ R=300 → L=1200, W=300 (가로 1200 × 세로 300 직사각형 + 중앙선)
  ※ R=1000 → L=4000, W=1000
  ※ 형광등 1개 = 객체 2개 (직사각형 1 + 내부선 1)

■ 비상등 / 비상조명 (사각형 + X 대각선) — 기본 R=300, HW=R, HH=R*0.6, layer E-EMRG
  ※ R=300 → HW=300, HH=180 (가로 600 × 세로 360 직사각형)
  사각형:   {"type":"CREATE_ENTITY","new_vertices":[{"x":X-HW,"y":Y-HH,"bulge":0},{"x":X+HW,"y":Y-HH,"bulge":0},{"x":X+HW,"y":Y+HH,"bulge":0},{"x":X-HW,"y":Y+HH,"bulge":0}],"new_layer":"E-EMRG"}
  대각1:    {"type":"CREATE_ENTITY","new_start":{"x":X-HW,"y":Y-HH},"new_end":{"x":X+HW,"y":Y+HH},"new_layer":"E-EMRG"}
  대각2:    {"type":"CREATE_ENTITY","new_start":{"x":X+HW,"y":Y-HH},"new_end":{"x":X-HW,"y":Y+HH},"new_layer":"E-EMRG"}
  예(R=300, X=5000, Y=5000): HW=300, HH=180
    사각형: {"type":"CREATE_ENTITY","new_vertices":[{"x":4700,"y":4820,"bulge":0},{"x":5300,"y":4820,"bulge":0},{"x":5300,"y":5180,"bulge":0},{"x":4700,"y":5180,"bulge":0}],"new_layer":"E-EMRG"}
    대각1:  {"type":"CREATE_ENTITY","new_start":{"x":4700,"y":4820},"new_end":{"x":5300,"y":5180},"new_layer":"E-EMRG"}
    대각2:  {"type":"CREATE_ENTITY","new_start":{"x":5300,"y":4820},"new_end":{"x":4700,"y":5180},"new_layer":"E-EMRG"}

■ 투광기 (원 + X대각 + 오른쪽 긴 선) — 기본 R=300, D=R*0.707, layer E-PROJ
  ※ R=300 → D=212 (원 내부 대각선 끝점 오프셋)
  원:       {"type":"CREATE_ENTITY","new_center":{"x":X,"y":Y},"new_radius":R,"new_layer":"E-PROJ"}
  대각1:    {"type":"CREATE_ENTITY","new_start":{"x":X-D,"y":Y-D},"new_end":{"x":X+D,"y":Y+D},"new_layer":"E-PROJ"}
  대각2:    {"type":"CREATE_ENTITY","new_start":{"x":X+D,"y":Y-D},"new_end":{"x":X-D,"y":Y+D},"new_layer":"E-PROJ"}
  방향선:   {"type":"CREATE_ENTITY","new_start":{"x":X+R,"y":Y},"new_end":{"x":X+R*2.5,"y":Y},"new_layer":"E-PROJ"}
  ※ 방향선 시작/끝 Y는 반드시 원 중심 Y와 동일 (Y 값 변경 금지)
  예(R=300, X=5000, Y=5000): D=212
    원:     {"type":"CREATE_ENTITY","new_center":{"x":5000,"y":5000},"new_radius":300,"new_layer":"E-PROJ"}
    대각1:  {"type":"CREATE_ENTITY","new_start":{"x":4788,"y":4788},"new_end":{"x":5212,"y":5212},"new_layer":"E-PROJ"}
    대각2:  {"type":"CREATE_ENTITY","new_start":{"x":5212,"y":4788},"new_end":{"x":4788,"y":5212},"new_layer":"E-PROJ"}
    방향선: {"type":"CREATE_ENTITY","new_start":{"x":5300,"y":5000},"new_end":{"x":5750,"y":5000},"new_layer":"E-PROJ"}

■ 수은등 / 방전등 (원 + 수평선 + 수직선 = 십자) — 기본 R=300, layer E-HID
  원:       {"type":"CREATE_ENTITY","new_center":{"x":X,"y":Y},"new_radius":R,"new_layer":"E-HID"}
  수평선:   {"type":"CREATE_ENTITY","new_start":{"x":X-R,"y":Y},"new_end":{"x":X+R,"y":Y},"new_layer":"E-HID"}
  수직선:   {"type":"CREATE_ENTITY","new_start":{"x":X,"y":Y-R},"new_end":{"x":X,"y":Y+R},"new_layer":"E-HID"}
  ※ 수평선·수직선 모두 원 중심을 지나 원의 끝점까지 그려야 함 (둘 다 필수)
  예(R=300, X=5000, Y=5000):
    원:     {"type":"CREATE_ENTITY","new_center":{"x":5000,"y":5000},"new_radius":300,"new_layer":"E-HID"}
    수평선: {"type":"CREATE_ENTITY","new_start":{"x":4700,"y":5000},"new_end":{"x":5300,"y":5000},"new_layer":"E-HID"}
    수직선: {"type":"CREATE_ENTITY","new_start":{"x":5000,"y":4700},"new_end":{"x":5000,"y":5300},"new_layer":"E-HID"}

■ 매입형 형광등 / 다운라이트 (사각형 + 내부 원) — 기본 R=300, L=R*2, layer E-EMBD
  사각형:   {"type":"CREATE_ENTITY","new_vertices":[{"x":X-L/2,"y":Y-R*0.4,"bulge":0},{"x":X+L/2,"y":Y-R*0.4,"bulge":0},{"x":X+L/2,"y":Y+R*0.4,"bulge":0},{"x":X-L/2,"y":Y+R*0.4,"bulge":0}],"new_layer":"E-EMBD"}
  내부원:   {"type":"CREATE_ENTITY","new_center":{"x":X,"y":Y},"new_radius":R*0.25,"new_layer":"E-EMBD"}

■ 벽부등 (삼각형 + 연결선) — 기본 R=300, H=R*0.5, layer E-WALL
  ※ R=300 → H=150 (삼각형 반높이), 삼각형 밑변 Y=Y-H, 꼭짓점 Y=Y+H
  삼각형:   {"type":"CREATE_ENTITY","new_vertices":[{"x":X-H,"y":Y-H,"bulge":0},{"x":X+H,"y":Y-H,"bulge":0},{"x":X,"y":Y+H,"bulge":0}],"new_layer":"E-WALL"}
  연결선:   {"type":"CREATE_ENTITY","new_start":{"x":X,"y":Y-H},"new_end":{"x":X,"y":Y-H*2},"new_layer":"E-WALL"}
  ※ 연결선은 삼각형 밑변 중앙(X, Y-H)에서 아래(X, Y-H*2)로 내려가는 짧은 수직선
  예(R=300, X=5000, Y=5000): H=150
    삼각형: {"type":"CREATE_ENTITY","new_vertices":[{"x":4850,"y":4850,"bulge":0},{"x":5150,"y":4850,"bulge":0},{"x":5000,"y":5150,"bulge":0}],"new_layer":"E-WALL"}
    연결선: {"type":"CREATE_ENTITY","new_start":{"x":5000,"y":4850},"new_end":{"x":5000,"y":4700},"new_layer":"E-WALL"}

■ LED등 (삼각형 + 캐소드선 + 방사선) — 기본 R=300, H=R (삼각형 높이), layer E-LED
  ※ R=300 → H=300, 삼각형: 밑변폭 H, 높이 H
  삼각형:   {"type":"CREATE_ENTITY","new_vertices":[{"x":X-H/2,"y":Y-H/2,"bulge":0},{"x":X+H/2,"y":Y-H/2,"bulge":0},{"x":X,"y":Y+H/2,"bulge":0}],"new_layer":"E-LED"}
  캐소드:   {"type":"CREATE_ENTITY","new_start":{"x":X-H/2,"y":Y+H/2},"new_end":{"x":X+H/2,"y":Y+H/2},"new_layer":"E-LED"}
  방사1:    {"type":"CREATE_ENTITY","new_start":{"x":X+H/2+30,"y":Y+H/4},"new_end":{"x":X+H/2+H/3,"y":Y+H/2+H/4},"new_layer":"E-LED"}
  방사2:    {"type":"CREATE_ENTITY","new_start":{"x":X+H/2+30,"y":Y},"new_end":{"x":X+H/2+H/3+30,"y":Y},"new_layer":"E-LED"}
  예(R=300, X=5000, Y=5000): H=300
    삼각형: {"type":"CREATE_ENTITY","new_vertices":[{"x":4850,"y":4850,"bulge":0},{"x":5150,"y":4850,"bulge":0},{"x":5000,"y":5150,"bulge":0}],"new_layer":"E-LED"}
    캐소드: {"type":"CREATE_ENTITY","new_start":{"x":4850,"y":5150},"new_end":{"x":5150,"y":5150},"new_layer":"E-LED"}
    방사1:  {"type":"CREATE_ENTITY","new_start":{"x":5180,"y":5075},"new_end":{"x":5280,"y":5225},"new_layer":"E-LED"}
    방사2:  {"type":"CREATE_ENTITY","new_start":{"x":5180,"y":5000},"new_end":{"x":5330,"y":5000},"new_layer":"E-LED"}

══════════════════════════════════════════════════════════════
[설비 심벌 표현]
──────────────────────────────────────────────────────────────
■ 스위치 (사각형 300×200):
  {"type":"CREATE_ENTITY","new_vertices":[{"x":X-150,"y":Y-100,"bulge":0},{"x":X+150,"y":Y-100,"bulge":0},{"x":X+150,"y":Y+100,"bulge":0},{"x":X-150,"y":Y+100,"bulge":0}],"new_layer":"E-SWITCH"}

■ 분전반/배전반 (사각형 1000×600):
  {"type":"CREATE_ENTITY","new_vertices":[{"x":X-500,"y":Y-300,"bulge":0},{"x":X+500,"y":Y-300,"bulge":0},{"x":X+500,"y":Y+300,"bulge":0},{"x":X-500,"y":Y+300,"bulge":0}],"new_layer":"E-PANEL"}

■ 단상 콘센트 (외부 원 + 플러그 홀 2개 + 수직 줄기) — 기본 R=200, layer E-OUTLET
  ※ R=200 → 외부 원 반지름 200, 플러그 홀 반지름 r_hole=25 (R*0.125)
  ※ 좌측 홀: center (X-60, Y-30), 우측 홀: center (X+60, Y-30)  [R=200 기준]
  ※ 줄기: 원 하단(Y-R)에서 Y-R*3까지 수직선 (길이 2R=400mm → 배선 인식 최소 300mm 충족)
  ⚠️ 단상 콘센트 1개 = 반드시 4개 엔티티 (외부 원 1 + 플러그 홀 2 + 줄기 1)
  외부원: {"type":"CREATE_ENTITY","new_center":{"x":X,"y":Y},"new_radius":R,"new_layer":"E-OUTLET"}
  좌홀:   {"type":"CREATE_ENTITY","new_center":{"x":X-60,"y":Y-30},"new_radius":25,"new_layer":"E-OUTLET"}
  우홀:   {"type":"CREATE_ENTITY","new_center":{"x":X+60,"y":Y-30},"new_radius":25,"new_layer":"E-OUTLET"}
  줄기:   {"type":"CREATE_ENTITY","new_start":{"x":X,"y":Y-R},"new_end":{"x":X,"y":Y-R*3},"new_layer":"E-OUTLET"}
  예(R=200, X=5000, Y=5000):
    외부원: {"type":"CREATE_ENTITY","new_center":{"x":5000,"y":5000},"new_radius":200,"new_layer":"E-OUTLET"}
    좌홀:   {"type":"CREATE_ENTITY","new_center":{"x":4940,"y":4970},"new_radius":25,"new_layer":"E-OUTLET"}
    우홀:   {"type":"CREATE_ENTITY","new_center":{"x":5060,"y":4970},"new_radius":25,"new_layer":"E-OUTLET"}
    줄기:   {"type":"CREATE_ENTITY","new_start":{"x":5000,"y":4800},"new_end":{"x":5000,"y":4400},"new_layer":"E-OUTLET"}

■ 2구 콘센트 / 이중 콘센트 (위 단상 콘센트 심볼 2개를 나란히) — 기본 R=200, layer E-OUTLET
  ※ step=R*5=1000, 심볼 2개를 X 방향으로 step 간격으로 배치
  ※ 각 심볼 = 외부원 + 플러그홀2개 + 줄기 = 4개 엔티티 × 2 = 총 8개 엔티티
  콘센트1(X=5000): 외부원(5000,5000,R=200) + 좌홀(4940,4970,r=25) + 우홀(5060,4970,r=25) + 줄기(5000→5000,4800→4600)
  콘센트2(X=6000): 외부원(6000,5000,R=200) + 좌홀(5940,4970,r=25) + 우홀(6060,4970,r=25) + 줄기(6000→6000,4800→4600)

■ 차단기/NFB/MCCB (사각형 400×200):
  {"type":"CREATE_ENTITY","new_vertices":[{"x":X-200,"y":Y-100,"bulge":0},{"x":X+200,"y":Y-100,"bulge":0},{"x":X+200,"y":Y+100,"bulge":0},{"x":X-200,"y":Y+100,"bulge":0}],"new_layer":"E-BREAKER"}

■ 배선/전선/간선 (기기 중심 연결 직선):
  {"type":"CREATE_ENTITY","new_start":{"x":X1,"y":Y1},"new_end":{"x":X2,"y":Y2},"new_layer":"E-WIRE"}

■ 접지선 (인접 기기 사이 짧은 수평 선분 — 기기N개 → 선분 N-1개):
  기기1 중심~기기2 중심, Y = 기기Y - (R+300)
  {"type":"CREATE_ENTITY","new_start":{"x":X1,"y":GY},"new_end":{"x":X2,"y":GY},"new_layer":"E-GRND"}

■ 중성선:
  {"type":"CREATE_ENTITY","new_start":{"x":X1,"y":Y1},"new_end":{"x":X2,"y":Y2},"new_layer":"E-NEUTRAL"}

■ 접지봉 (삼중원 심벌) — D=직경(mm), L=길이(mm), N=개수, layer E-GRND
  ※ 물리 규격(D, L)은 레이블로만 표현. 심벌 크기는 고정값 사용.
  ※ 도식 간격 S_draw=1000 (기본값 — 지시사항에서 다른 값이 주어지면 그 값을 우선 사용)
  ※ 원 반지름 기본값(200, 130, 60) — 지시사항에서 다른 반지름이 주어지면 그 값을 우선 사용
  ※ 원 반지름 값은 심벌 크기일 뿐, D(직경)가 아님. 레이블에 이 값 절대 사용 금지.
  ※ 레이블 규칙:
    ⚠️ 스펙 레이블("접지봉 (ΦDxL)xNEA")은 사용자가 D 또는 L을 명시했을 때만 생성.
       D·L 미지정 시 스펙 레이블 생성 금지.
    - D·L 모두 지정 시: 스펙 레이블 "접지봉 (ΦDxL)xNEA" 생성. 예) "접지봉 (Φ18x2400)x3EA"
    - D만 있고 L 없을 때: 스펙 레이블 "접지봉 (ΦD)xNEA" 생성.
    - D·L 미지정 시: 스펙 레이블 없음. 심벌만 생성.
    - 접지 계통 번호(E1, E2, E3 등): 사용자가 요청하면 별도 텍스트 엔티티로 생성
      위치: 심벌 그룹 중앙 상단 base_y+400 (스펙 레이블 있으면 그 위 300mm)
    예) "접지봉 3개" → 스펙 레이블 없음, 심벌만 생성
    예) "접지봉 3개에 E3 레이블" → 스펙 레이블 없음, E3 텍스트(base_y+400)만 생성
    예) "직경 18mm 길이 2400mm 접지봉 3개" → 스펙 레이블 "접지봉 (Φ18x2400)x3EA" 생성
    예) "직경 18mm 길이 2400mm 접지봉 3개에 E3" → 스펙 레이블(base_y+400) + E3(base_y+700)
  접지봉 1개 = 외부원(R=200) + 중간원(R=130) + 내부원(R=60) = 3개 엔티티
  외부원: {"type":"CREATE_ENTITY","new_center":{"x":X,"y":Y},"new_radius":200,"new_layer":"E-GRND"}
  중간원: {"type":"CREATE_ENTITY","new_center":{"x":X,"y":Y},"new_radius":130,"new_layer":"E-GRND"}
  내부원: {"type":"CREATE_ENTITY","new_center":{"x":X,"y":Y},"new_radius":60,"new_layer":"E-GRND"}

[접지봉 배치 규칙]
- S_draw = 지시사항의 값 우선 사용 (기본값 1000 — L값에 관계없이 도식 간격 사용)
- R_outer = 지시사항의 값 우선 사용 (기본값 200)
- N=2: 수평 배치. Rod1=(base_x, base_y), Rod2=(base_x+S_draw, base_y)
        접지선 1개: Rod1↔Rod2
        인출선 1개: Rod1에서만 하단 수직선 (X=base_x, Y=base_y-R_outer → Y=base_y-R_outer*3)
        ⚠️ Rod2에서 인출선 절대 생성 금지 — Y자·역삼각형 모양이 되면 안 됨
- N=3: 삼각 배치 (정삼각형, 아래 꼭짓점이 인출선)
        Rod1=(base_x, base_y)
        Rod2=(base_x+S_draw, base_y)
        Rod3=(base_x+S_draw/2, base_y-S_draw*0.866)
        접지선 3개: Rod1↔Rod2, Rod1↔Rod3, Rod2↔Rod3
        인출선 1개: Rod3에서 하단 (X=Rod3_x, Y=Rod3_y-R_outer → Y=Rod3_y-R_outer*3)
- N=4: 정방형(사각형) 배치
        Rod1=(base_x, base_y)              ← 좌상
        Rod2=(base_x+S_draw, base_y)       ← 우상
        Rod3=(base_x, base_y-S_draw)       ← 좌하
        Rod4=(base_x+S_draw, base_y-S_draw) ← 우하
        접지선 4개 (사각형 4변): Rod1↔Rod2, Rod3↔Rod4, Rod1↔Rod3, Rod2↔Rod4
        인출선: Rod4에서 하단 (Rod4_x, Rod4_y-R_outer*1.5) → (Rod4_x, Rod4_y-R_outer*4)
        레이블: 사각형 중앙 상단 (base_x+S_draw/2, base_y+R_outer*2)
- N>=5: 수평 체인 배치. step=S_draw, 인접 봉끼리 접지선 연결, 마지막 봉 하단 인출선
- 레이블: "접지봉 (ΦDxL)xNEA" 텍스트를 그룹 중앙 상단 R_outer*2 위에 배치
          new_height=R_outer*0.75, layer=E-GRND

[색상(new_color) 규칙]
- 사용자 요청에 색상이 포함되면 해당 색상의 ACI 인덱스를 new_color 필드로 모든 객체에 추가하세요.
  빨간색/red=1, 노란색/yellow=2, 초록색/green=3, 청록색/cyan=4,
  파란색/blue=5, 보라색/magenta=6, 흰색/white=7, 회색/gray=8, 검은색/black=250
  예) "빨간색 원 2개" → 각 객체에 "new_color": 1 포함

[일반 도형 규칙] — 전기 심벌이 아닌 단순 원/선/사각형 요청 시
- 원(circle) N개: 각 원에 "new_center", "new_radius", "new_layer":"AI_PROPOSAL" 사용
  step = max(new_radius×5, 2000). base_x+i*step 으로 X축 배치 (겹침 금지)
  예) "원 2개 반지름 500" → step=max(2500,2000)=2500, 위치: (base_x, base_y), (base_x+2500, base_y)
- 선(line) N개, 사각형(rectangle) N개도 동일 규칙으로 겹치지 않게 배치

[배치 규칙]
- 간격(step):
  · 원형 심벌(전구/투광기/수은등/LED/벽부등/비상등/콘센트): step = max(R×5, 2000)
    콘센트 R=200 → step=max(1000,2000)=2000
  · 형광등(직사각형): step = max(L×1.5, 2000)  ← L=R*4이므로 R=300→step=1800→2000, R=500→step=3000
  · 일반 원: step = max(R×5, 2000)
- base_x를 시작점으로 X축 방향으로 step 간격 배치
  예) 전구 3개 R=300: step=1500→2000 → x=base_x, base_x+2000, base_x+4000
  예) 형광등 3개 R=300: L=1200, step=max(1800,2000)=2000 → x=base_x, base_x+2000, base_x+4000
- ⚠️ 배선(E-WIRE) / 접지선(E-GRND) / 중성선(E-NEUTRAL)은 사용자가 명시적으로 요청할 때만 생성
  → "배선 연결해줘", "접지선 추가", "전선 그려줘" 등 명시 요청 없으면 절대 생성하지 않는다
  → 조명/스위치/콘센트만 요청하면 심벌만 생성하고 배선은 생성하지 않는다
  접지선이 필요한 경우: 설비 중심 Y에서 R+300 아래 수평선으로 연결
  예) R=1000 이면 접지선 Y = base_y - 1300
- 각 객체는 반드시 서로 다른 좌표에 배치
- 사용자가 개수를 지정하면 정확히 그 개수만큼 생성
- 크기 미지정 시 기본값: 조명류 R=300, 콘센트 R=200, 스위치 300×200, 분전반 1000×600

[심벌 선택 기준]
- "전등", "백열등", "조명", "전구" → 전구 일반 (원+X)
- "형광등", "FL등" → 형광등 (막대+연결선)
- "비상등", "비상조명" → 비상등 (사각형+X)
- "투광기", "서치라이트" → 투광기 (원+X+방향선)
- "수은등", "방전등", "HID" → 수은등 (원+십자)
- "매입형", "다운라이트", "매입등" → 매입형 형광등 (사각형+원)
- "벽부등", "브라켓" → 벽부등 (삼각형+연결선)
- "LED", "LED등" → LED등 (삼각형+방사선)
- "콘센트", "단상 콘센트" → 단상 콘센트 (원+줄기1개)
- "2구 콘센트", "이중 콘센트", "더블 콘센트" → 2구 콘센트 (원+줄기2개)
- "접지봉", "어스봉", "접지극" → 접지봉 (이중원+접지선+인출선, 삼각/수평 배치)

[예시 A] "전구 3개, 접지선 2개, 사이즈 1000" (base_x=5000, base_y=5000):
- R=1000, step=5000, d=707, GY=5000-1300=3700
{"objects": [
  {"type":"CREATE_ENTITY","new_center":{"x":5000,"y":5000},"new_radius":1000,"new_layer":"E-LIGHT"},
  {"type":"CREATE_ENTITY","new_start":{"x":4293,"y":4293},"new_end":{"x":5707,"y":5707},"new_layer":"E-LIGHT"},
  {"type":"CREATE_ENTITY","new_start":{"x":5707,"y":4293},"new_end":{"x":4293,"y":5707},"new_layer":"E-LIGHT"},
  {"type":"CREATE_ENTITY","new_center":{"x":10000,"y":5000},"new_radius":1000,"new_layer":"E-LIGHT"},
  {"type":"CREATE_ENTITY","new_start":{"x":9293,"y":4293},"new_end":{"x":10707,"y":5707},"new_layer":"E-LIGHT"},
  {"type":"CREATE_ENTITY","new_start":{"x":10707,"y":4293},"new_end":{"x":9293,"y":5707},"new_layer":"E-LIGHT"},
  {"type":"CREATE_ENTITY","new_center":{"x":15000,"y":5000},"new_radius":1000,"new_layer":"E-LIGHT"},
  {"type":"CREATE_ENTITY","new_start":{"x":14293,"y":4293},"new_end":{"x":15707,"y":5707},"new_layer":"E-LIGHT"},
  {"type":"CREATE_ENTITY","new_start":{"x":15707,"y":4293},"new_end":{"x":14293,"y":5707},"new_layer":"E-LIGHT"},
  {"type":"CREATE_ENTITY","new_start":{"x":5000,"y":3700},"new_end":{"x":10000,"y":3700},"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_start":{"x":10000,"y":3700},"new_end":{"x":15000,"y":3700},"new_layer":"E-GRND"}
]}

[예시 B] "형광등 3개, 사이즈 300" (base_x=5000, base_y=5000):
- R=300, L=1200, W=300, step=max(L*1.5,2000)=max(1800,2000)=2000
- 형광등 1개 = 직사각형(1200×300) + 중앙 가로선 → 객체 2개
{"objects": [
  {"type":"CREATE_ENTITY","new_vertices":[{"x":4400,"y":4850,"bulge":0},{"x":5600,"y":4850,"bulge":0},{"x":5600,"y":5150,"bulge":0},{"x":4400,"y":5150,"bulge":0}],"new_layer":"E-LIGHT-FL"},
  {"type":"CREATE_ENTITY","new_start":{"x":4450,"y":5000},"new_end":{"x":5550,"y":5000},"new_layer":"E-LIGHT-FL"},
  {"type":"CREATE_ENTITY","new_vertices":[{"x":6400,"y":4850,"bulge":0},{"x":7600,"y":4850,"bulge":0},{"x":7600,"y":5150,"bulge":0},{"x":6400,"y":5150,"bulge":0}],"new_layer":"E-LIGHT-FL"},
  {"type":"CREATE_ENTITY","new_start":{"x":6450,"y":5000},"new_end":{"x":7550,"y":5000},"new_layer":"E-LIGHT-FL"},
  {"type":"CREATE_ENTITY","new_vertices":[{"x":8400,"y":4850,"bulge":0},{"x":9600,"y":4850,"bulge":0},{"x":9600,"y":5150,"bulge":0},{"x":8400,"y":5150,"bulge":0}],"new_layer":"E-LIGHT-FL"},
  {"type":"CREATE_ENTITY","new_start":{"x":8450,"y":5000},"new_end":{"x":9550,"y":5000},"new_layer":"E-LIGHT-FL"}
]}

[예시 C] "콘센트 3개" (base_x=5000, base_y=5000):
- R=200, step=2000, r_hole=25, 홀offset_x=60, 홀offset_y=30
- 줄기: Y-R=4800 → Y-R*3=4400 (길이 400mm, 배선 인식 가능)
- ⚠️ 콘센트 1개 = 외부원 1 + 좌홀 1 + 우홀 1 + 줄기 1 = 반드시 4개 엔티티
{"objects": [
  {"type":"CREATE_ENTITY","new_center":{"x":5000,"y":5000},"new_radius":200,"new_layer":"E-OUTLET"},
  {"type":"CREATE_ENTITY","new_center":{"x":4940,"y":4970},"new_radius":25,"new_layer":"E-OUTLET"},
  {"type":"CREATE_ENTITY","new_center":{"x":5060,"y":4970},"new_radius":25,"new_layer":"E-OUTLET"},
  {"type":"CREATE_ENTITY","new_start":{"x":5000,"y":4800},"new_end":{"x":5000,"y":4400},"new_layer":"E-OUTLET"},
  {"type":"CREATE_ENTITY","new_center":{"x":7000,"y":5000},"new_radius":200,"new_layer":"E-OUTLET"},
  {"type":"CREATE_ENTITY","new_center":{"x":6940,"y":4970},"new_radius":25,"new_layer":"E-OUTLET"},
  {"type":"CREATE_ENTITY","new_center":{"x":7060,"y":4970},"new_radius":25,"new_layer":"E-OUTLET"},
  {"type":"CREATE_ENTITY","new_start":{"x":7000,"y":4800},"new_end":{"x":7000,"y":4400},"new_layer":"E-OUTLET"},
  {"type":"CREATE_ENTITY","new_center":{"x":9000,"y":5000},"new_radius":200,"new_layer":"E-OUTLET"},
  {"type":"CREATE_ENTITY","new_center":{"x":8940,"y":4970},"new_radius":25,"new_layer":"E-OUTLET"},
  {"type":"CREATE_ENTITY","new_center":{"x":9060,"y":4970},"new_radius":25,"new_layer":"E-OUTLET"},
  {"type":"CREATE_ENTITY","new_start":{"x":9000,"y":4800},"new_end":{"x":9000,"y":4400},"new_layer":"E-OUTLET"}
]}

[예시 E] "직경 18mm 길이 2400mm 접지봉 3개" (base_x=5000, base_y=5000):
- D=18, L=2400, N=3, S_draw=1000(고정)
- 삼각배치: Rod1=(5000,5000), Rod2=(6000,5000), Rod3=(5500,4134)
  ※ Rod2: x=5000+1000=6000
  ※ Rod3: x=5000+500=5500, y=5000-866=4134
- 접지봉 1개 = 삼중원(R=200, R=130, R=60) = 3개 엔티티
- 접지선: Rod1↔Rod2, Rod1↔Rod3, Rod2↔Rod3
- 인출선: Rod3 하단 (5500,3934)→(5500,3534)
- 레이블: 중앙 상단 (5500, 5400)
{"objects": [
  {"type":"CREATE_ENTITY","new_center":{"x":5000,"y":5000},"new_radius":200,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_center":{"x":5000,"y":5000},"new_radius":130,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_center":{"x":5000,"y":5000},"new_radius":60,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_center":{"x":6000,"y":5000},"new_radius":200,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_center":{"x":6000,"y":5000},"new_radius":130,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_center":{"x":6000,"y":5000},"new_radius":60,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_center":{"x":5500,"y":4134},"new_radius":200,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_center":{"x":5500,"y":4134},"new_radius":130,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_center":{"x":5500,"y":4134},"new_radius":60,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_start":{"x":5000,"y":5000},"new_end":{"x":6000,"y":5000},"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_start":{"x":5000,"y":5000},"new_end":{"x":5500,"y":4134},"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_start":{"x":6000,"y":5000},"new_end":{"x":5500,"y":4134},"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_start":{"x":5500,"y":3934},"new_end":{"x":5500,"y":3534},"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_text":"접지봉 (Φ18x2400)x3EA","base_x":5500,"base_y":5400,"new_height":150,"new_layer":"E-GRND"}
]}

[텍스트 레이블 규칙]
- 사용자가 "레이블", "텍스트", "번호", "이름" 등을 언급하거나, 분전반·차단기·콘센트에 회로명/용량을 요청하면 텍스트 엔티티도 함께 생성
- 텍스트 생성 형식:
  {"type":"CREATE_ENTITY","new_text":"텍스트내용","base_x":X,"base_y":Y,"new_height":높이,"new_layer":"레이어명"}
- 텍스트 위치: 심벌 중심 X, 심벌 하단 Y - R - 200 (심벌 아래 여백 200mm)
- 텍스트 높이: new_height = 150 (기본), 작은 심벌이면 100
- 레이어: 심벌 레이어와 동일
- ⚠️ 사용자가 레이블을 명시적으로 요청할 때만 생성. 언급 없으면 텍스트 생성 금지.

⚠️ [레이블 추가 vs 심벌 생성 — 반드시 구분]
- "분전반"이라는 단어가 "레이블" 문맥으로 쓰이면 분전반 심벌(직사각형)은 절대 그리지 않는다.
- "접지봉 N개에 MDP-1 레이블 추가해줘" → 접지봉 N개 그리고 MDP-1 텍스트도 추가. 분전반 사각형 생성 안 함.
- "분전반 MDP-1 그려줘" (분전반 자체 생성 요청) → 분전반 심벌(사각형) + 텍스트 생성.
- 핵심: 요청된 주 심벌(접지봉·형광등 등)은 그리고, 레이블로 언급된 분전반 명칭(MDP-1 등)은 텍스트로만 표현.

[텍스트 레이블 예시]
"분전반 MDP-1 그려줘" (base_x=5000, base_y=5000):
{"objects": [
  {"type":"CREATE_ENTITY","new_vertices":[{"x":4500,"y":4700,"bulge":0},{"x":5500,"y":4700,"bulge":0},{"x":5500,"y":5300,"bulge":0},{"x":4500,"y":5300,"bulge":0}],"new_layer":"E-PANEL"},
  {"type":"CREATE_ENTITY","new_text":"MDP-1","base_x":5000,"base_y":4500,"new_height":150,"new_layer":"E-PANEL"}
]}

"접지봉 D18 L2400 3개에 분전반 MDP-1 레이블 추가해줘" (base_x=5000, base_y=5000):
→ 접지봉 3개(삼중원) 그리고, MDP-1 텍스트 추가. 분전반 사각형 생성 안 함.
{"objects": [
  {"type":"CREATE_ENTITY","new_center":{"x":5000,"y":5000},"new_radius":200,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_center":{"x":5000,"y":5000},"new_radius":130,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_center":{"x":5000,"y":5000},"new_radius":60,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_center":{"x":6000,"y":5000},"new_radius":200,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_center":{"x":6000,"y":5000},"new_radius":130,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_center":{"x":6000,"y":5000},"new_radius":60,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_center":{"x":5500,"y":4134},"new_radius":200,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_center":{"x":5500,"y":4134},"new_radius":130,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_center":{"x":5500,"y":4134},"new_radius":60,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_start":{"x":5000,"y":5000},"new_end":{"x":6000,"y":5000},"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_start":{"x":5000,"y":5000},"new_end":{"x":5500,"y":4134},"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_start":{"x":6000,"y":5000},"new_end":{"x":5500,"y":4134},"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_start":{"x":5500,"y":3934},"new_end":{"x":5500,"y":3534},"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_text":"접지봉 (Φ18x2400)x3EA","base_x":5500,"base_y":5400,"new_height":150,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_text":"MDP-1","base_x":5500,"base_y":5700,"new_height":150,"new_layer":"E-GRND"}
]}

"접지봉 4개에 E4 레이블 추가해줘" (D·L 미지정, base_x=5000, base_y=5000):
→ N=4 정방형(사각형) 배치. D·L 없으므로 스펙 레이블 없음. E4만 생성.
- Rod1=(5000,5000)좌상, Rod2=(6000,5000)우상, Rod3=(5000,4000)좌하, Rod4=(6000,4000)우하
- 접지선: Rod1↔Rod2(상변), Rod3↔Rod4(하변), Rod1↔Rod3(좌변), Rod2↔Rod4(우변)
- 인출선: Rod4에서 하단 (6000,3800)→(6000,3400)
- E4 텍스트: 중앙 상단 (5500, 5400)
{"objects": [
  {"type":"CREATE_ENTITY","new_center":{"x":5000,"y":5000},"new_radius":200,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_center":{"x":5000,"y":5000},"new_radius":130,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_center":{"x":5000,"y":5000},"new_radius":60,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_center":{"x":6000,"y":5000},"new_radius":200,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_center":{"x":6000,"y":5000},"new_radius":130,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_center":{"x":6000,"y":5000},"new_radius":60,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_center":{"x":5000,"y":4000},"new_radius":200,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_center":{"x":5000,"y":4000},"new_radius":130,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_center":{"x":5000,"y":4000},"new_radius":60,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_center":{"x":6000,"y":4000},"new_radius":200,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_center":{"x":6000,"y":4000},"new_radius":130,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_center":{"x":6000,"y":4000},"new_radius":60,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_start":{"x":5000,"y":5000},"new_end":{"x":6000,"y":5000},"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_start":{"x":5000,"y":4000},"new_end":{"x":6000,"y":4000},"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_start":{"x":5000,"y":5000},"new_end":{"x":5000,"y":4000},"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_start":{"x":6000,"y":5000},"new_end":{"x":6000,"y":4000},"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_start":{"x":6000,"y":3800},"new_end":{"x":6000,"y":3400},"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_text":"E4","base_x":5500,"base_y":5400,"new_height":150,"new_layer":"E-GRND"}
]}

"접지봉 2개에 E2 레이블 추가해줘" (D·L 미지정, base_x=5000, base_y=5000):
→ N=2 수평배치. 삼중원(R=200,130,60). 인출선은 Rod1(왼쪽)에서만 1개. D·L 없으므로 스펙 레이블 없음. E2만.
{"objects": [
  {"type":"CREATE_ENTITY","new_center":{"x":5000,"y":5000},"new_radius":200,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_center":{"x":5000,"y":5000},"new_radius":130,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_center":{"x":5000,"y":5000},"new_radius":60,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_center":{"x":6000,"y":5000},"new_radius":200,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_center":{"x":6000,"y":5000},"new_radius":130,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_center":{"x":6000,"y":5000},"new_radius":60,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_start":{"x":5000,"y":5000},"new_end":{"x":6000,"y":5000},"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_start":{"x":5000,"y":4800},"new_end":{"x":5000,"y":4400},"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_text":"E2","base_x":5500,"base_y":5400,"new_height":150,"new_layer":"E-GRND"}
]}

"접지봉 3개에 E3 레이블 추가해줘" (D·L 미지정, base_x=5000, base_y=5000):
→ D·L 없으므로 스펙 레이블 없음. E3만 생성. 삼중원(R=200,130,60).
{"objects": [
  {"type":"CREATE_ENTITY","new_center":{"x":5000,"y":5000},"new_radius":200,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_center":{"x":5000,"y":5000},"new_radius":130,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_center":{"x":5000,"y":5000},"new_radius":60,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_center":{"x":6000,"y":5000},"new_radius":200,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_center":{"x":6000,"y":5000},"new_radius":130,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_center":{"x":6000,"y":5000},"new_radius":60,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_center":{"x":5500,"y":4134},"new_radius":200,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_center":{"x":5500,"y":4134},"new_radius":130,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_center":{"x":5500,"y":4134},"new_radius":60,"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_start":{"x":5000,"y":5000},"new_end":{"x":6000,"y":5000},"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_start":{"x":5000,"y":5000},"new_end":{"x":5500,"y":4134},"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_start":{"x":6000,"y":5000},"new_end":{"x":5500,"y":4134},"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_start":{"x":5500,"y":3934},"new_end":{"x":5500,"y":3534},"new_layer":"E-GRND"},
  {"type":"CREATE_ENTITY","new_text":"E3","base_x":5500,"base_y":5400,"new_height":150,"new_layer":"E-GRND"}
]}

"콘센트 3개, 20A 레이블 추가" (base_x=5000, base_y=5000):
{"objects": [
  {"type":"CREATE_ENTITY","new_center":{"x":5000,"y":5000},"new_radius":200,"new_layer":"E-OUTLET"},
  {"type":"CREATE_ENTITY","new_start":{"x":5000,"y":4800},"new_end":{"x":5000,"y":4600},"new_layer":"E-OUTLET"},
  {"type":"CREATE_ENTITY","new_text":"20A","base_x":5000,"base_y":4400,"new_height":100,"new_layer":"E-OUTLET"},
  {"type":"CREATE_ENTITY","new_center":{"x":7000,"y":5000},"new_radius":200,"new_layer":"E-OUTLET"},
  {"type":"CREATE_ENTITY","new_start":{"x":7000,"y":4800},"new_end":{"x":7000,"y":4600},"new_layer":"E-OUTLET"},
  {"type":"CREATE_ENTITY","new_text":"20A","base_x":7000,"base_y":4400,"new_height":100,"new_layer":"E-OUTLET"},
  {"type":"CREATE_ENTITY","new_center":{"x":9000,"y":5000},"new_radius":200,"new_layer":"E-OUTLET"},
  {"type":"CREATE_ENTITY","new_start":{"x":9000,"y":4800},"new_end":{"x":9000,"y":4600},"new_layer":"E-OUTLET"},
  {"type":"CREATE_ENTITY","new_text":"20A","base_x":9000,"base_y":4400,"new_height":100,"new_layer":"E-OUTLET"}
]}

[응답 형식]
반드시 {"objects": [ ... ]} 형식의 JSON 객체로만 반환하세요. 설명 없이 JSON만.
"""


class ElecDrawCommandParser(DrawCommandParser):
    """
    전기 도메인 전용 파서.
    단일 객체 생성은 부모 클래스 DrawCommandParser를 사용하고,
    복수 객체 배치 요청(전등 N개, 스위치, 분전반 등)은 배치 LLM으로 처리한다.
    """

    _BATCH_TRIGGER_RE = re.compile(
        r"(\d+\s*개|\d+개|여러|다수"
        r"|전등|조명|전구|백열등|형광등|비상등|투광기|수은등|방전등|매입형|다운라이트|매입등|벽부등|브라켓|LED등|LED"
        r"|스위치|분전반|배전반|콘센트|차단기|접지봉|접지극|어스봉|접지선|중성선|간선|NFB|MCCB)"
        r".*?(그려|추가|만들|생성|삽입|넣어|연결|배치|설치|줘|주세요)",
        re.DOTALL,
    )
    # 동사 없이 "심벌 + N개"만으로도 batch 경로 사용 (예: "접지봉 2개", "형광등 5개")
    _BATCH_QUICK_RE = re.compile(
        r"(?:접지봉|접지극|어스봉|전구|백열등|형광등|비상등|투광기|수은등|방전등|매입형|다운라이트|매입등|벽부등|브라켓|LED등|LED|콘센트|분전반|배전반|MCCB|NFB)"
        r".{0,80}?\d+\s*개",
        re.DOTALL,
    )

    @staticmethod
    def _drawing_center(entity_by_handle: dict) -> tuple[float, float]:
        """도면 엔티티들의 중심 좌표를 반환. 없으면 (5000, 5000) 기본값."""
        xs, ys = [], []
        for e in entity_by_handle.values():
            if not isinstance(e, dict):
                continue
            pos = e.get("position") or {}
            if pos.get("x") is not None:
                xs.append(float(pos["x"]))
                ys.append(float(pos["y"]))
            bbox = e.get("bbox") or {}
            if bbox.get("x1") is not None:
                xs.append((float(bbox["x1"]) + float(bbox["x2"])) / 2)
                ys.append((float(bbox["y1"]) + float(bbox["y2"])) / 2)
        if xs:
            return sum(xs) / len(xs), sum(ys) / len(ys)
        return 5000.0, 5000.0

    async def parse(
        self,
        user_text: str,
        active_handles: list[str],
        entity_by_handle: dict[str, dict],
        view_center: "tuple[float, float] | None" = None,
    ) -> "dict | list | None":
        # 복수 객체 배치 요청 감지 (동사 포함 또는 심벌+개수 단독 패턴)
        if self._BATCH_TRIGGER_RE.search(user_text) or self._BATCH_QUICK_RE.search(user_text):
            return await self._parse_batch(user_text, active_handles, entity_by_handle, view_center)
        # 단일 객체 또는 수정 → 부모 처리 (view_center 전달하여 뷰포트 기준 좌표 사용)
        return await super().parse(user_text, active_handles, entity_by_handle, view_center=view_center)

    # ── 콘센트 직접 그리기 의도 감지 ─────────────────────────────────────────
    # "콘센트 3개 그려줘", "전기 콘센트 몇개 그려줘", "콘센트 5개 추가" 등
    _OUTLET_DRAW_RE = re.compile(
        r"(?:전기\s*)?콘센트\s*(\d+|몇)\s*개",
        re.IGNORECASE | re.DOTALL,
    )

    @staticmethod
    def _build_outlet_cmds(
        n: int,
        base_x: float,
        base_y: float,
        R: float = 200.0,
    ) -> list[dict]:
        """
        전기 콘센트 심볼을 n개 생성한다.
        심볼 구성 (4개 엔티티 × n):
          1) 외부 원 (본체): center=(cx, cy), radius=R
          2) 좌측 플러그 홀: center=(cx-R*0.3, cy-R*0.15), radius=R*0.125
          3) 우측 플러그 홀: center=(cx+R*0.3, cy-R*0.15), radius=R*0.125
          4) 연결 줄기: (cx, cy-R) → (cx, cy-R*2)
        간격: step = max(R * 10, 2000)mm
        """
        step = max(R * 10, 2000.0)
        r_hole = round(R * 0.125)
        hole_dx = round(R * 0.3)
        hole_dy = round(R * 0.15)
        layer = "E-OUTLET"
        cmds: list[dict] = []
        for i in range(n):
            cx = round(base_x + i * step, 2)
            cy = round(base_y, 2)
            cmds += [
                # 1) 외부 원 (본체)
                {
                    "type": "CREATE_ENTITY",
                    "new_center": {"x": cx, "y": cy},
                    "new_radius": R,
                    "new_layer": layer,
                },
                # 2) 좌측 플러그 홀
                {
                    "type": "CREATE_ENTITY",
                    "new_center": {"x": round(cx - hole_dx, 2), "y": round(cy - hole_dy, 2)},
                    "new_radius": r_hole,
                    "new_layer": layer,
                },
                # 3) 우측 플러그 홀
                {
                    "type": "CREATE_ENTITY",
                    "new_center": {"x": round(cx + hole_dx, 2), "y": round(cy - hole_dy, 2)},
                    "new_radius": r_hole,
                    "new_layer": layer,
                },
                # 4) 연결 줄기 (원 하단에서 아래로 2R 길이 → 최소 400mm, 배선 인식 가능)
                {
                    "type": "CREATE_ENTITY",
                    "new_start": {"x": cx, "y": round(cy - R, 2)},
                    "new_end":   {"x": cx, "y": round(cy - R * 3, 2)},
                    "new_layer": layer,
                },
            ]
        return cmds

    # ── 접지봉 교체/삭제 의도 감지 패턴 ──────────────────────────────────────
    _GRND_DELETE_RE = re.compile(
        r"(?:접지봉|어스봉|접지극).{0,30}?(?:삭제|없애|제거|지워|지우|빼|빼줘)",
        re.DOTALL,
    )
    _GRND_REPLACE_RE = re.compile(
        r"(?:접지봉|어스봉|접지극).{0,50}?(?:바꿔|변경|교체|수정|개로\s*바꿔|개로\s*변경)",
        re.DOTALL,
    )

    _GRND_ROD_LEN_RE = re.compile(
        r"(?:Φ|φ|fi)\s*\d+\s*[xX×]\s*(\d+)", re.IGNORECASE
    )
    _GRND_TARGET_COUNT_RE = re.compile(
        r"(?:(?:접지봉|어스봉|접지극).{0,50}?(\d+)\s*개|(\d+)\s*개.{0,50}?(?:접지봉|어스봉|접지극))",
        re.DOTALL,
    )
    _GRND_QTY_TEXT_RE = re.compile(r"([xX×]\s*)\d+(\s*EA\b)", re.IGNORECASE)
    _GRND_E_LABEL_RE = re.compile(r"^\s*E\s*\d+\s*$", re.IGNORECASE)

    @staticmethod
    def _detect_grnd_scale(
        handles: list[str],
        entity_by_handle: dict,
    ) -> dict:
        """
        접지봉 심벌 스케일 감지.
        1) 원(CIRCLE) 반지름 → R_outer
        2) 블록(INSERT) x_scale → R_outer 추정
        3) 텍스트 "Φ18x2400" → 봉 길이로 S_draw 기본값 계산
        4) 봉 그룹 중심 간 거리 → S_draw (실측 우선)

        반환: {"R_outer": int, "R_mid": int, "R_inner": int, "S_draw": int, "n_rods": int}
               감지 실패 시 빈 dict
        """
        _ROD_LEN_RE = re.compile(r"(?:Φ|φ|fi)\s*\d+\s*[xX×]\s*(\d+)", re.IGNORECASE)

        circles: list[dict] = []
        rod_length_mm: int | None = None

        for h in handles:
            e = entity_by_handle.get(str(h))
            if not isinstance(e, dict):
                continue

            etype = str(e.get("type") or e.get("entity_type") or "").upper()

            # 1) 원 엔티티 직접 반지름
            r = e.get("radius") or e.get("new_radius")
            if r:
                pos = e.get("position") or e.get("center") or {}
                x, y = pos.get("x"), pos.get("y")
                if x is not None and y is not None:
                    circles.append({"r": float(r), "x": float(x), "y": float(y)})
                continue

            # 2) 블록(INSERT) — x_scale × 기준 반지름(200)으로 추정
            if etype in ("INSERT", "BLOCK", "BLOCKREF"):
                scale = e.get("x_scale") or e.get("scale") or e.get("xscale")
                if scale:
                    r_est = float(scale) * 200
                    pos = (
                        e.get("insert_point")
                        or e.get("position")
                        or e.get("center")
                        or {}
                    )
                    x, y = pos.get("x"), pos.get("y")
                    if x is not None and y is not None:
                        circles.append({"r": r_est, "x": float(x), "y": float(y)})
                continue

            # 3) 텍스트에서 봉 길이 추출 (Φ18x2400 → 2400mm)
            text_val = str(
                e.get("text") or e.get("string") or e.get("content") or ""
            )
            m = _ROD_LEN_RE.search(text_val)
            if m and rod_length_mm is None:
                rod_length_mm = int(m.group(1))

        if not circles:
            return {}

        R_outer = max(c["r"] for c in circles)

        # 봉 그룹 클러스터링: 거리 < R_outer*2.5인 원들은 같은 봉의 동심원
        used: set[int] = set()
        rod_centers: list[tuple[float, float]] = []
        for i, c in enumerate(circles):
            if i in used:
                continue
            group = [c]
            used.add(i)
            for j, c2 in enumerate(circles):
                if j in used:
                    continue
                dist = ((c["x"] - c2["x"]) ** 2 + (c["y"] - c2["y"]) ** 2) ** 0.5
                if dist < R_outer * 2.5:
                    group.append(c2)
                    used.add(j)
            biggest = max(group, key=lambda cc: cc["r"])
            rod_centers.append((biggest["x"], biggest["y"]))

        # S_draw: 봉간 실측 거리 우선, 없으면 봉 길이×2(KEC 140.6 최소 이격) 기본값
        S_draw: int
        if len(rod_centers) >= 2:
            dists = [
                ((rod_centers[i][0] - rod_centers[j][0]) ** 2
                 + (rod_centers[i][1] - rod_centers[j][1]) ** 2) ** 0.5
                for i in range(len(rod_centers))
                for j in range(i + 1, len(rod_centers))
            ]
            S_draw = max(int(round(min(dists) / 100) * 100), 100)
        elif rod_length_mm is not None:
            # 봉이 1개뿐 → 봉 길이×2를 도면 단위(mm)로 변환해 S_draw 추정
            S_draw = rod_length_mm * 2
        else:
            S_draw = 1000

        return {
            "R_outer": int(round(R_outer)),
            "R_mid":   int(round(R_outer * 130 / 200)),
            "R_inner": int(round(R_outer * 60 / 200)),
            "S_draw":  S_draw,
            "n_rods":  len(rod_centers),
            "rod_length_mm": rod_length_mm,
        }

    # 접지봉 레이어 패턴: E-GRND, E_GRND, E3, GRND, 접지 등
    _GRND_LAYER_RE = re.compile(
        r"^(?:E[-_]?GRND|E[-_]?GND|E3|GRND|GND|접지|EARTH|EARTHING)",
        re.IGNORECASE,
    )
    # 접지봉 블록명 패턴
    _GRND_BLOCK_RE = re.compile(
        r"접지봉|GRND[-_]?ROD|GND[-_]?ROD|EARTH[-_]?ROD|E[-_]?GRND",
        re.IGNORECASE,
    )
    # 접지봉 텍스트 패턴 (Φ18x2400, fi18x2400 등)
    _GRND_TEXT_RE = re.compile(
        r"접지봉|(?:Φ|φ|fi)\s*\d+\s*[xX×]\s*\d+",
        re.IGNORECASE,
    )

    @staticmethod
    def _find_grnd_handles(entity_by_handle: dict) -> tuple[list[str], float, float]:
        """
        도면 전체에서 접지봉 관련 엔티티를 자동 탐지.
        우선순위: 1) 접지봉 레이어  2) 접지봉 블록명  3) 접지봉 텍스트 포함
        """
        _GRND_LAYER_RE = re.compile(
            r"^(?:E[-_]?GRND|E[-_]?GND|E3|GRND|GND|접지|EARTH|EARTHING)",
            re.IGNORECASE,
        )
        _GRND_BLOCK_RE = re.compile(
            r"접지봉|GRND[-_]?ROD|GND[-_]?ROD|EARTH[-_]?ROD|E[-_]?GRND",
            re.IGNORECASE,
        )
        _GRND_TEXT_RE = re.compile(
            r"접지봉|(?:Φ|φ|fi)\s*\d+\s*[xX×]\s*\d+",
            re.IGNORECASE,
        )

        handles: list[str] = []
        xs: list[float] = []
        ys: list[float] = []

        def _add(h: str, e: dict) -> None:
            handles.append(h)
            pos = e.get("position") or e.get("center") or e.get("insert_point") or {}
            if pos.get("x") is not None:
                xs.append(float(pos["x"]))
                ys.append(float(pos["y"]))

        for handle, entity in entity_by_handle.items():
            if not isinstance(entity, dict):
                continue
            layer = str(entity.get("layer") or "").strip()
            block_name = str(
                entity.get("block_name") or entity.get("effective_name") or entity.get("name") or ""
            )
            text_val = str(entity.get("text") or entity.get("string") or entity.get("content") or "")

            if _GRND_LAYER_RE.match(layer):
                _add(str(handle), entity)
            elif _GRND_BLOCK_RE.search(block_name):
                _add(str(handle), entity)
            elif _GRND_TEXT_RE.search(text_val):
                _add(str(handle), entity)

        cx = sum(xs) / len(xs) if xs else 5000.0
        cy = sum(ys) / len(ys) if ys else 5000.0
        return handles, cx, cy

    @classmethod
    def _parse_grnd_target_count(cls, user_text: str) -> int | None:
        match = cls._GRND_TARGET_COUNT_RE.search(user_text or "")
        if not match:
            return None
        raw = next((g for g in match.groups() if g), None)
        if raw is None:
            return None
        try:
            return max(0, min(int(raw), 50))
        except ValueError:
            return None

    @staticmethod
    def _grnd_entity_center(entity: dict) -> tuple[float, float] | None:
        pos = (
            entity.get("insert_point")
            or entity.get("position")
            or entity.get("center")
            or {}
        )
        if pos.get("x") is not None and pos.get("y") is not None:
            return float(pos["x"]), float(pos["y"])
        bbox = entity.get("bbox") or {}
        if bbox.get("x1") is not None and bbox.get("x2") is not None:
            return (
                (float(bbox["x1"]) + float(bbox["x2"])) / 2.0,
                (float(bbox["y1"]) + float(bbox["y2"])) / 2.0,
            )
        return None

    @staticmethod
    def _grnd_entity_radius(entity: dict) -> float:
        if entity.get("radius") is not None:
            try:
                return abs(float(entity["radius"]))
            except (TypeError, ValueError):
                pass
        bbox = entity.get("bbox") or {}
        try:
            width = abs(float(bbox.get("x2", 0)) - float(bbox.get("x1", 0)))
            height = abs(float(bbox.get("y2", 0)) - float(bbox.get("y1", 0)))
            return max(width, height) / 2.0
        except (TypeError, ValueError):
            return 100.0

    @classmethod
    def _is_grnd_rod_symbol(cls, entity: dict) -> bool:
        etype = str(entity.get("type") or entity.get("entity_type") or "").upper()
        layer = str(entity.get("layer") or "").strip()
        block_blob = " ".join([
            str(entity.get("block_name") or ""),
            str(entity.get("effective_name") or ""),
            str(entity.get("name") or ""),
        ])
        if etype in {"INSERT", "BLOCK", "BLOCKREF"}:
            return bool(cls._GRND_BLOCK_RE.search(block_blob) or cls._GRND_TEXT_RE.search(block_blob))
        if etype == "CIRCLE":
            return bool(cls._GRND_LAYER_RE.match(layer) or cls._GRND_BLOCK_RE.search(block_blob))
        return False

    @classmethod
    def _find_grnd_rod_groups(
        cls,
        entity_by_handle: dict,
        handles: list[str] | None = None,
    ) -> list[dict]:
        handle_filter = {str(h) for h in handles} if handles else None
        blocks: list[dict] = []
        circles: list[dict] = []

        for handle, entity in entity_by_handle.items():
            if handle_filter is not None and str(handle) not in handle_filter:
                continue
            if not isinstance(entity, dict) or not cls._is_grnd_rod_symbol(entity):
                continue
            center = cls._grnd_entity_center(entity)
            if center is None:
                continue
            item = {
                "handles": [str(handle)],
                "x": center[0],
                "y": center[1],
                "radius": max(cls._grnd_entity_radius(entity), 50.0),
            }
            etype = str(entity.get("type") or entity.get("entity_type") or "").upper()
            if etype in {"INSERT", "BLOCK", "BLOCKREF"}:
                blocks.append(item)
            else:
                circles.append(item)

        # 블록 접지봉은 한 블록이 한 봉이므로 블록을 우선 사용한다.
        if blocks:
            return blocks

        groups: list[dict] = []
        for circle in circles:
            merged = False
            for group in groups:
                dist = ((circle["x"] - group["x"]) ** 2 + (circle["y"] - group["y"]) ** 2) ** 0.5
                if dist <= max(circle["radius"], group["radius"]) * 0.35:
                    group["handles"].extend(circle["handles"])
                    group["x"] = (group["x"] + circle["x"]) / 2.0
                    group["y"] = (group["y"] + circle["y"]) / 2.0
                    group["radius"] = max(group["radius"], circle["radius"])
                    merged = True
                    break
            if not merged:
                groups.append(dict(circle))
        return groups

    @staticmethod
    def _grnd_touch_points(entity: dict) -> list[tuple[float, float]]:
        points: list[tuple[float, float]] = []
        for key in ("start", "end", "start_point", "end_point"):
            pt = entity.get(key) or {}
            if pt.get("x") is not None and pt.get("y") is not None:
                points.append((float(pt["x"]), float(pt["y"])))
        for vertex in entity.get("vertices") or []:
            if isinstance(vertex, dict) and vertex.get("x") is not None and vertex.get("y") is not None:
                points.append((float(vertex["x"]), float(vertex["y"])))
        center = ElecDrawCommandParser._grnd_entity_center(entity)
        if center:
            points.append(center)
        return points

    @classmethod
    def _handles_for_grnd_count_reduction(
        cls,
        rod_groups: list[dict],
        target_count: int,
        active_handles: list[str],
        entity_by_handle: dict,
    ) -> list[str]:
        if target_count >= len(rod_groups):
            return []

        kept = sorted(rod_groups, key=lambda g: (-g["y"], g["x"]))[:target_count]
        kept_ids = {id(g) for g in kept}
        deleted = [g for g in rod_groups if id(g) not in kept_ids]

        delete_handles: list[str] = []
        for group in deleted:
            delete_handles.extend(str(h) for h in group.get("handles") or [])

        selected = {str(h) for h in active_handles or []}
        if selected:
            rod_handles = {
                str(h)
                for group in rod_groups
                for h in (group.get("handles") or [])
            }
            deleted_centers = [
                (float(g["x"]), float(g["y"]), max(float(g.get("radius") or 100.0) * 6.0, 500.0))
                for g in deleted
            ]
            for handle in selected:
                if handle in rod_handles or handle in delete_handles:
                    continue
                entity = entity_by_handle.get(handle)
                if not isinstance(entity, dict):
                    continue
                etype = str(entity.get("type") or entity.get("entity_type") or "").upper()
                if etype not in {"LINE", "ARC", "LWPOLYLINE", "POLYLINE", "POLYLINE2D", "POLYLINE3D"}:
                    continue
                points = cls._grnd_touch_points(entity)
                if not points:
                    continue
                touches_deleted = any(
                    ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5 <= radius
                    for px, py in points
                    for cx, cy, radius in deleted_centers
                )
                if touches_deleted:
                    delete_handles.append(handle)

        return list(dict.fromkeys(delete_handles))

    @classmethod
    def _build_grnd_count_text_updates(
        cls,
        entity_by_handle: dict,
        target_count: int,
        rod_groups: list[dict],
    ) -> list[dict]:
        if not rod_groups:
            return []
        cx = sum(float(g["x"]) for g in rod_groups) / len(rod_groups)
        cy = sum(float(g["y"]) for g in rod_groups) / len(rod_groups)
        search_radius = 5000.0
        updates: list[dict] = []

        for handle, entity in entity_by_handle.items():
            if not isinstance(entity, dict):
                continue
            if str(entity.get("type") or entity.get("entity_type") or "").upper() not in {"TEXT", "MTEXT"}:
                continue
            text = str(entity.get("text") or entity.get("string") or entity.get("content") or "")
            if not text:
                continue
            pos = cls._grnd_entity_center(entity)
            if not pos or ((pos[0] - cx) ** 2 + (pos[1] - cy) ** 2) ** 0.5 > search_radius:
                continue

            new_text: str | None = None
            if cls._GRND_QTY_TEXT_RE.search(text):
                new_text = cls._GRND_QTY_TEXT_RE.sub(
                    lambda m: f"{m.group(1)}{target_count}{m.group(2)}",
                    text,
                    count=1,
                )
            elif cls._GRND_E_LABEL_RE.match(text):
                new_text = f"E{target_count}"

            if new_text and new_text != text:
                updates.append({
                    "type": "TEXT_CONTENT",
                    "target_handles": [str(handle)],
                    "new_text": new_text,
                    "direct_apply": True,
                })

        return updates

    async def _parse_batch(
        self,
        user_text: str,
        active_handles: list[str],
        entity_by_handle: dict[str, dict],
        view_center: "tuple[float, float] | None" = None,
    ) -> "list | None":
        # ── 콘센트 직접 생성 (LLM 없이 deterministic) ───────────────────────
        # "2구", "이중" 요청은 LLM 경로로 넘김 (배치가 다름)
        is_dual = bool(re.search(r"2구|이중|더블|double", user_text, re.IGNORECASE))
        outlet_m = self._OUTLET_DRAW_RE.search(user_text)
        if outlet_m and not is_dual:
            raw_n = outlet_m.group(1)
            n = 3 if raw_n == "몇" else min(int(raw_n), 20)
            if view_center is not None:
                bx, by = view_center
            else:
                bx, by = self._drawing_center(entity_by_handle)
                for h in active_handles:
                    e = entity_by_handle.get(str(h))
                    if e:
                        bx, by = self._entity_center(e)
                        break
            cmds = self._build_outlet_cmds(n, bx, by)
            outlet_color = _extract_color_from_text(user_text)
            if outlet_color is not None:
                for cmd in cmds:
                    if isinstance(cmd, dict) and "new_color" not in cmd:
                        cmd["new_color"] = outlet_color
            logging.info(
                "[ElecDrawCommandParser] 콘센트 %d개 직접 생성 (base=%.0f,%.0f)",
                n, bx, by,
            )
            return cmds

        # ── 기존 접지봉 삭제/교체 의도 감지 ────────────────────────────────
        is_grnd_delete  = bool(self._GRND_DELETE_RE.search(user_text))
        is_grnd_replace = bool(self._GRND_REPLACE_RE.search(user_text))
        grnd_target_count = self._parse_grnd_target_count(user_text)

        grnd_handles: list[str] = []
        grnd_cx, grnd_cy = 5000.0, 5000.0

        if is_grnd_delete or is_grnd_replace:
            if active_handles:
                # B 방식: 사용자가 AutoCAD에서 직접 선택한 핸들을 우선 사용
                grnd_handles = list(active_handles)
                xs, ys = [], []
                for h in grnd_handles:
                    e = entity_by_handle.get(str(h))
                    if e:
                        ex, ey = self._entity_center(e)
                        xs.append(ex); ys.append(ey)
                grnd_cx = sum(xs) / len(xs) if xs else 5000.0
                grnd_cy = sum(ys) / len(ys) if ys else 5000.0
                logging.info(
                    "[ElecDrawCommandParser] 사용자 선택 핸들 %d개 사용 (중심 %.0f,%.0f)",
                    len(grnd_handles), grnd_cx, grnd_cy,
                )
            else:
                # 폴백: 레이어/블록명/텍스트 패턴으로 자동 탐지
                grnd_handles, grnd_cx, grnd_cy = self._find_grnd_handles(entity_by_handle)
                logging.info(
                    "[ElecDrawCommandParser] 접지봉 자동탐지 %d개 (중심 %.0f,%.0f)",
                    len(grnd_handles), grnd_cx, grnd_cy,
                )

        # 삭제만 요청한 경우 → DELETE 명령 단일 dict 반환 (websocket 직접 삭제 경로)
        if is_grnd_delete:
            if grnd_handles:
                logging.info("[ElecDrawCommandParser] 접지봉 %d개 삭제 명령 생성", len(grnd_handles))
                return {"type": "DELETE", "target_handles": grnd_handles}
            else:
                return {"no_selection": True, "message": "도면에서 접지봉을 찾을 수 없습니다. AutoCAD에서 직접 선택 후 다시 시도하세요."}

        # "접지봉을 2개로 수정"처럼 기존 수량을 줄이는 요청은 제안 구름이 아니라 실제 삭제/텍스트 수정으로 처리한다.
        if is_grnd_replace and grnd_target_count is not None:
            rod_groups = self._find_grnd_rod_groups(entity_by_handle, grnd_handles)
            if not rod_groups and grnd_handles:
                rod_groups = self._find_grnd_rod_groups(entity_by_handle)
            current_count = len(rod_groups)
            if current_count > grnd_target_count:
                delete_handles = self._handles_for_grnd_count_reduction(
                    rod_groups,
                    grnd_target_count,
                    active_handles,
                    entity_by_handle,
                )
                if delete_handles:
                    msg = (
                        f"접지봉 {current_count}개 중 {current_count - grnd_target_count}개를 삭제해 "
                        f"{grnd_target_count}개로 맞췄습니다."
                    )
                    delete_cmd = {
                        "type": "DELETE",
                        "target_handles": delete_handles,
                        "direct_apply": True,
                        "message": msg,
                    }
                    text_updates = self._build_grnd_count_text_updates(
                        entity_by_handle,
                        grnd_target_count,
                        rod_groups,
                    )
                    logging.info(
                        "[ElecDrawCommandParser] 접지봉 수량 축소: rods=%d target=%d delete=%d text_updates=%d",
                        current_count,
                        grnd_target_count,
                        len(delete_handles),
                        len(text_updates),
                    )
                    return [delete_cmd] + text_updates if text_updates else delete_cmd

        # 교체 요청인데 자동탐지도 실패한 경우에만 안내
        if is_grnd_replace and not grnd_handles:
            return {"no_selection": True, "message": "도면에서 접지봉을 찾을 수 없습니다. AutoCAD에서 직접 선택 후 다시 시도하세요."}

        # ── 배치 기준 좌표 결정 ──────────────────────────────────────────────
        # 우선순위: 1) 기존 접지봉 중심(교체 시)  2) 뷰포트 중심  3) 선택 객체
        #           4) 도면 엔티티 무게중심  5) 기본값
        if is_grnd_replace and grnd_handles:
            base_x, base_y = grnd_cx, grnd_cy
        elif view_center is not None:
            base_x, base_y = view_center
        else:
            base_x, base_y = self._drawing_center(entity_by_handle)
            for h in active_handles:
                e = entity_by_handle.get(str(h))
                if e:
                    base_x, base_y = self._entity_center(e)
                    break

        # ── 기존 접지봉 스케일 감지 (REPLACE 시 선택 핸들에서 추정) ────────
        scale_hint = ""
        if is_grnd_replace and grnd_handles:
            scale = self._detect_grnd_scale(grnd_handles, entity_by_handle)
            if scale:
                Ro  = scale["R_outer"]
                Rm  = scale["R_mid"]
                Ri  = scale["R_inner"]
                S   = scale["S_draw"]
                h86 = int(round(S * 0.866))
                h_lbl = max(int(round(Ro * 0.75)), 50)
                scale_hint = (
                    f"\n\n[기존 접지봉 감지 결과 — 반드시 이 값만 사용하세요. 기본값 무시]\n"
                    f"R_outer={Ro}, R_mid={Rm}, R_inner={Ri} (새 접지봉 원 반지름)\n"
                    f"S_draw={S} (새 봉간 간격)\n"
                    f"기존 봉 수={scale['n_rods']}개\n"
                    f"\n[배치 좌표 공식 — 위 값으로 계산된 실제 값]\n"
                    f"N=2: Rod1=(base_x, base_y), Rod2=(base_x+{S}, base_y)\n"
                    f"     인출선: (base_x, base_y-{Ro}) → (base_x, base_y-{Ro*3})\n"
                    f"N=3: Rod1=(base_x, base_y), Rod2=(base_x+{S}, base_y),\n"
                    f"     Rod3=(base_x+{S//2}, base_y-{h86})\n"
                    f"     인출선: (base_x+{S//2}, base_y-{h86}-{Ro}) → (base_x+{S//2}, base_y-{h86}-{Ro*3})\n"
                    f"N=4: Rod1=(base_x,base_y), Rod2=(base_x+{S},base_y),\n"
                    f"     Rod3=(base_x,base_y-{S}), Rod4=(base_x+{S},base_y-{S})\n"
                    f"     인출선: (base_x+{S}, base_y-{S}-{int(Ro*1.5)}) → (base_x+{S}, base_y-{S}-{Ro*4})\n"
                    f"레이블 new_height={h_lbl}\n"
                    f"⚠️ 기본값(200,130,60) 또는 S_draw=1000 절대 사용 금지."
                )
                logging.info(
                    "[ElecDrawCommandParser] 기존 접지봉 스케일 감지: R=%d/%d/%d S=%d 봉=%d개",
                    Ro, Rm, Ri, S, scale["n_rods"],
                )

        system = (
            _ELEC_BATCH_SYSTEM
            + f"\n\n[배치 기준 좌표] base_x={base_x:.0f}, base_y={base_y:.0f}"
            + scale_hint
        )
        try:
            result = await generate_answer(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user_text},
                ],
                response_format={"type": "json_object"},
                temperature=0.0,
            )
            # LLM이 {"objects": [...]} 또는 직접 [...] 반환 모두 허용
            if isinstance(result, dict):
                objects = result.get("objects") or result.get("items") or []
                if not objects:
                    for v in result.values():
                        if isinstance(v, list):
                            objects = v
                            break
            elif isinstance(result, list):
                objects = result
            elif isinstance(result, str):
                parsed = json.loads(result)
                objects = parsed if isinstance(parsed, list) else (parsed.get("objects") or [])
            else:
                objects = []

            create_cmds = [
                o for o in objects
                if isinstance(o, dict) and o.get("type") == "CREATE_ENTITY"
            ]

            # 사용자 텍스트에 색상 키워드가 있으면 모든 CREATE 명령에 new_color 주입
            batch_color = _extract_color_from_text(user_text)
            if batch_color is not None:
                for cmd in create_cmds:
                    if "new_color" not in cmd:
                        cmd["new_color"] = batch_color

            # 교체인 경우: DELETE 단일 명령 + CREATE 명령 묶음 반환
            # (grnd_handles 비어있는 경우는 이미 LLM 호출 전에 no_selection 반환됨)
            if is_grnd_replace and grnd_handles and create_cmds:
                delete_cmd = {"type": "DELETE", "target_handles": grnd_handles}
                logging.info(
                    "[ElecDrawCommandParser] 접지봉 교체: DELETE %d개 + CREATE %d개",
                    len(grnd_handles), len(create_cmds),
                )
                return [delete_cmd] + create_cmds

            if create_cmds:
                logging.info(
                    "[ElecDrawCommandParser] 배치 생성 %d개 객체 → CAD_ACTION 전송",
                    len(create_cmds),
                )
                return create_cmds

        except Exception as exc:
            logging.warning("[ElecDrawCommandParser] 배치 LLM 실패: %s", exc)

        return None
