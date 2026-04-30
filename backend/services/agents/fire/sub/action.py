"""
Fire action agent for selected-object modification requests.
"""

import json
import logging
import re

from backend.services.agents.fire.sub.review.parser import ParserAgent
from backend.services.llm_service import generate_answer


class ActionAgent:
    def __init__(self):
        self.parser = ParserAgent()

    async def analyze_and_fix(self, context: dict, domain: str = "fire") -> dict:
        active_ids = set(context.get("active_object_ids") or [])
        raw_layout = context.get("raw_layout_data") or "{}"
        mapping_table = context.get("mapping_table")

        parsed_result = self.parser.parse(raw_layout, mapping_table=mapping_table)
        all_elements = parsed_result.get("elements") or []

        drawing_data = context.get("drawing_data") or {}
        is_focus_mode = drawing_data.get("context_mode") == "full_with_focus"

        if active_ids:
            selected = [
                element
                for element in all_elements
                if element.get("handle") in active_ids or element.get("id") in active_ids
            ]
            selected_keys = {element.get("handle") for element in selected} | {
                element.get("id") for element in selected
            }
            surrounding = [
                element
                for element in all_elements
                if element.get("handle") not in selected_keys
                and element.get("id") not in selected_keys
            ]
        elif is_focus_mode:
            selected = all_elements
            surrounding = []
        else:
            selected = all_elements
            surrounding = []

        if not selected:
            logging.info(
                "[FireActionAgent] no selected objects (active_ids=%s, count=%d)",
                active_ids,
                len(all_elements),
            )
            return {
                "analysis": "분석 대상 선택 객체가 없습니다.",
                "fixes": [],
                "message": "수정할 객체를 AutoCAD에서 먼저 선택한 뒤 다시 시도해 주세요.",
            }

        user_request = (context.get("user_request") or "").strip()
        move_delta = _extract_move_delta(user_request)
        if move_delta is not None:
            delta_x, delta_y = move_delta
            fixes = [
                {
                    "handle": element.get("handle") or element.get("id") or "",
                    "type": element.get("type") or element.get("raw_type") or "UNKNOWN",
                    "reason": f"사용자 이동 요청: {user_request}",
                    "action": "MOVE",
                    "auto_fix": {
                        "type": "MOVE",
                        "delta_x": delta_x,
                        "delta_y": delta_y,
                    },
                }
                for element in selected
                if element.get("handle") or element.get("id")
            ]
            logging.info(
                "[FireActionAgent] deterministic move selected=%d fixes=%d dx=%s dy=%s",
                len(selected),
                len(fixes),
                delta_x,
                delta_y,
            )
            return {
                "analysis": f"선택 객체 {len(selected)}개를 이동 요청에 따라 일괄 이동합니다.",
                "fixes": fixes,
                "message": f"선택 객체 {len(fixes)}개를 X {delta_x}, Y {delta_y}만큼 이동합니다.",
            }

        retrieved_laws = context.get("retrieved_laws") or []
        law_text = "\n".join(
            item.get("content", "") or item.get("snippet", "")
            for item in retrieved_laws[:5]
        ) or "법규 데이터 없음"

        system_prompt = """당신은 소방 설비 CAD 수정 명령 생성기다.
선택된 객체와 사용자 요청을 보고 실제로 적용 가능한 JSON만 출력하라.

규칙:
- 반드시 JSON 객체 하나만 출력한다. 코드펜스, 설명문, 주석을 넣지 마라.
- handle 값은 입력 객체의 값을 그대로 사용한다.
- 수정이 불가능하거나 근거가 부족한 객체는 fixes에 넣지 않는다.
- 사용자가 "위로 50", "아래로 50", "좌측", "우측"처럼 이동을 요청하면 auto_fix.type은 반드시 MOVE를 사용한다.
- MOVE 형식:
  {"type": "MOVE", "delta_x": 숫자, "delta_y": 숫자}
- C# DrawingPatcher에서 허용되는 auto_fix.type 예시:
  LAYER, MOVE, ATTRIBUTE, TEXT_CONTENT, TEXT_HEIGHT, COLOR, ROTATE, SCALE, DELETE, LINETYPE, LINEWEIGHT, GEOMETRY

출력 스키마:
{
  "analysis": "짧은 분석 요약",
  "fixes": [
    {
      "handle": "원본 handle",
      "type": "객체 타입",
      "reason": "수정 이유",
      "action": "MOVE",
      "auto_fix": {"type": "MOVE", "delta_x": 0, "delta_y": 50}
    }
  ],
  "message": "사용자 요약 메시지"
}"""

        user_block = (
            f"[사용자 요청]\n{user_request}\n\n"
            if user_request
            else ""
        )
        user_prompt = f"""{user_block}[선택 객체 {len(selected)}개]
{json.dumps(selected, ensure_ascii=False, indent=2)}

[주변 컨텍스트]
{json.dumps(surrounding[:10], ensure_ascii=False, indent=2) if surrounding else "없음"}

[관련 법규]
{law_text}

선택 객체에 대해 필요한 수정 명령을 JSON으로만 출력하라."""

        try:
            result = await generate_answer(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
            )
            parsed = _parse_llm_json(result)
            fixes = parsed.get("fixes") or []
            for fix in fixes:
                auto_fix = fix.get("auto_fix")
                if isinstance(auto_fix, dict) and auto_fix.get("type") == "MOVE":
                    auto_fix["delta_x"] = _safe_float(auto_fix.get("delta_x", 0))
                    auto_fix["delta_y"] = _safe_float(auto_fix.get("delta_y", 0))

            parsed["fixes"] = fixes
            parsed.setdefault("analysis", "분석 완료")
            parsed.setdefault(
                "message",
                f"선택 객체 {len(selected)}개 분석 완료. {len(fixes)}개 수정 후보를 생성했습니다.",
            )
            logging.info(
                "[FireActionAgent] completed selected=%d fixes=%d",
                len(selected),
                len(fixes),
            )
            return parsed
        except Exception as exc:
            logging.error("[FireActionAgent] LLM 분석 실패: %s", exc)
            return {
                "analysis": "분석 중 오류 발생",
                "fixes": [],
                "message": f"수정 분석 중 오류가 발생했습니다: {exc}",
            }


def _parse_llm_json(result) -> dict:
    if isinstance(result, dict):
        return result
    if not isinstance(result, str):
        return {}

    text = result.strip()
    if not text:
        return {}

    candidates = [text]

    if text.startswith("```"):
        unfenced = "\n".join(
            line for line in text.splitlines() if not line.strip().startswith("```")
        ).strip()
        if unfenced:
            candidates.append(unfenced)

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(text[start : end + 1])

    seen = set()
    for candidate in candidates:
        normalized = candidate.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        try:
            return json.loads(normalized)
        except json.JSONDecodeError:
            continue

    logging.warning("[FireActionAgent] JSON parse failed. raw preview=%s", text[:500])
    raise json.JSONDecodeError("Unable to parse LLM JSON response", text, 0)


def _safe_float(value) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0


def _extract_move_delta(user_request: str):
    text = (user_request or "").strip()
    if not text:
        return None

    number_match = re.search(r"(-?\d+(?:\.\d+)?)", text)
    amount = float(number_match.group(1)) if number_match else None

    if amount is None:
        return None

    if "위" in text or "올려" in text or "상향" in text:
        return 0.0, abs(amount)
    if "아래" in text or "내려" in text or "하향" in text:
        return 0.0, -abs(amount)
    if "오른" in text or "우측" in text or "오른쪽" in text:
        return abs(amount), 0.0
    if "왼" in text or "좌측" in text or "왼쪽" in text:
        return -abs(amount), 0.0
    return None
