"""
File    : backend/services/graph/nodes/fire_review_node.py
Author  : 양창일
Description : 소방 도메인 LangGraph 노드.


Modification History :
    - 2026-05-12 (김지우) : RAG 수정
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.database import SessionLocal
from backend.services import llm_service
from backend.services.agents.fire.schemas import FIRE_SUB_AGENT_TOOLS
from backend.services.agents.fire.sub.mapping import MappingAgent
from backend.services.agents.fire.workflow_handler import FireWorkflowHandler
from backend.services.graph.prompt_utils import build_memory_prompt_from_state
from backend.services.graph.state import (
    AgentState,
    CurrentStep,
    LawReference,
    PendingFix,
    ReviewResult,
    ViolationItem,
)
from backend.services.payload_service import (
    CONTEXT_MODE_FULL_WITH_FOCUS,
    should_preserve_full_entities,
)


logger = logging.getLogger(__name__)

_HAS_TABLE_REF_RE = re.compile(
    r"(?:표|별표|부표|다음\s*표|이\s*표|[Tt]able)\s*[\d\-\.가-힣]*"
    r"\s*(?:에\s*따른다|참조|참고|에\s*의함|에\s*의거|와\s*같다|을\s*적용|기준|에\s*준한다|에\s*나타낸|에\s*나타낸\s*바)",
    re.IGNORECASE,
)

_FIRE_TABLE_VALUE_HINTS = (
    "NFSC", "KCS", "KS", "스프링클러", "감지기", "소화전", "소화기",
    "방화문", "방화구획", "배관", "밸브", "mm", "m", "MPa", "kPa", "L/min",
)


async def fire_review_node(state: AgentState) -> AgentState:
    async with SessionLocal() as db:
        try:
            sid = state.get("session_id") or (state.get("session_meta") or {}).get("session_id")
            logger.info("[FireGraph] domain_node(fire_review) ENTER session=%s", sid)
            out = await _run_fire(state, db)
            logger.info(
                "[FireGraph] domain_node(fire_review) EXIT session=%s step=%s",
                sid,
                out.get("current_step"),
            )
            return out
        except Exception as exc:
            logger.error("[FireNode] processing error: %s", exc, exc_info=True)
            error_msg = f"소방 에이전트 처리 중 오류가 발생했습니다: {exc}"
            error_result: ReviewResult = {
                "is_violation": False,
                "violations": [],
                "suggestions": [],
                "referenced_laws": [],
                "final_message": error_msg,
            }
            return {
                **state,
                "review_result": error_result,
                "current_step": "error",
                "assistant_response": error_msg,
            }


async def _classify_intent(user_message: str) -> str:
    msg = (user_message or "").strip().lower()

    action_keywords = (
        "수정", "변경", "옮겨", "이동", "교체", "삭제", "적용",
        "위로", "아래로", "올려", "내려", "좌측", "우측", "왼쪽", "오른쪽",
        "레이어", "도면층", "바꿔",
        "modify", "move", "delete", "replace", "apply",
    )
    if any(k in msg for k in action_keywords):
        return "action"

    fix_keywords = (
        "수정안", "수정 방법", "고치는 방법", "어떻게 고쳐", "fix suggestion",
    )
    if any(k in msg for k in fix_keywords):
        return "fix_suggestion"

    review_keywords = (
        "검토", "리뷰", "위반", "점검", "진단",
        "review", "compliance",
    )
    if any(k in msg for k in review_keywords):
        return "review"

    answer_keywords = (
        "안녕", "고마워", "감사", "뭐야", "설명", "hello", "hi", "thanks",
    )
    if any(k in msg for k in answer_keywords):
        return "answer"

    system_prompt = """당신은 소방 설비 AI 라우터입니다.
사용자 요청을 answer, review, fix_suggestion, action 중 하나로 분류하세요.
- answer: 일반 질의, 법규 설명, 인사
- review: 도면 검토, 위반 탐지, 기준 점검
- fix_suggestion: 이미 발견된 위반 또는 특정 항목의 수정 방법 제안
- action: 수정 지시, 변경, 이동, 교체
반드시 JSON {\"intent\": \"answer\" | \"review\" | \"fix_suggestion\" | \"action\"} 형식으로만 답하세요."""
    try:
        res = await llm_service.generate_answer(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            response_format={"type": "json_object"},
        )
        if isinstance(res, dict):
            intent = str(res.get("intent") or "answer")
            if intent not in ("answer", "review", "fix_suggestion", "action"):
                return "answer"
            return intent
    except Exception:
        pass
    return "answer"


_DRAWING_REVIEW_RE = re.compile(
    r"(검토해|봐줘|이 도면|도면에서|도면 기준|위반 여부|찾아줘)"
)
# "확인해줘" 단독은 제외 — "기준에 맞는지 확인해줘"처럼 도면 참조 없는 문장도 매칭되기 때문


def _should_override_answer_to_review(message: str, has_drawing: bool) -> bool:
    """_classify_intent()가 "answer"를 반환한 뒤 보정용으로만 호출한다.
    도면이 로드된 상태에서 명시적 도면 검토 의도가 있으면 True를 반환한다."""
    if not has_drawing:
        return False
    return bool(_DRAWING_REVIEW_RE.search(message))


def _filter_entities_by_selection(drawing_data: dict[str, Any], active_ids: set[str]) -> dict[str, Any]:
    if not active_ids:
        return drawing_data
    entities = drawing_data.get("entities") or drawing_data.get("elements") or []
    filtered = [
        e for e in entities
        if str(e.get("handle") or "") in active_ids or str(e.get("id") or "") in active_ids
    ]
    if not filtered:
        return drawing_data
    return {**drawing_data, "entities": filtered}


def _build_system_prompt(context: dict[str, Any], memory_text: str) -> str:
    drawing_loaded = context.get("drawing_loaded", False)
    is_mapped = context.get("is_mapped", False)
    pending = context.get("pending_fixes") or []
    intent = context.get("intent") or "answer"
    term_map = (context.get("mapping_table") or {}).get("term_map", {})

    equipment_hint = ""
    if term_map:
        sample = list(term_map.items())[:10]
        lines = "\n".join(f"- {k}: {v}" for k, v in sample)
        equipment_hint = f"\n[도면 표준화 샘플]\n{lines}"

    focus_hint = ""
    drawing_data = context.get("drawing_data") or {}
    if drawing_data.get("context_mode") == CONTEXT_MODE_FULL_WITH_FOCUS:
        focus = drawing_data.get("focus_extraction") or {}
        focus_hint = (
            f"\n[검토 범위]\n전체 도면이 유지되며 focus_extraction이 우선 범위입니다. "
            f"focus entities={len(focus.get('entities') or [])}"
        )

    selected_count = len(context.get("active_object_ids") or [])
    return (
        "당신은 소방 도면 검토 오케스트레이터입니다.\n"
        f"- intent={intent}\n"
        f"- drawing_loaded={drawing_loaded}\n"
        f"- is_mapped={is_mapped}\n"
        f"- pending_fixes={len(pending)}\n"
        f"- selected_objects={selected_count}\n"
        "- call_query_agent: NFSC/시방서/기준 질의\n"
        "- call_review_agent: 도면 검토 및 위반 탐지\n"
        "- call_action_agent: 선택 객체 수정안 생성\n"
        "- 사용자가 객체를 선택한 상태에서 이동/수정/변경/교체를 요청하면 반드시 call_action_agent를 선택하세요.\n"
        "- selected_objects가 1개 이상이면, 일반 설명으로 되묻지 말고 먼저 수정 도구를 호출하세요.\n"
        "- 도구 없이 직접 답변하는 것은 일반 질의/설명 요청일 때만 허용됩니다.\n"
        f"{equipment_hint}{focus_hint}\n\n"
        f"[메모리]\n{memory_text}"
    )


async def _run_fire(state: AgentState, db: AsyncSession) -> AgentState:
    message = state.get("user_request") or ""
    memory_text = build_memory_prompt_from_state(state)

    drawing_data = state.get("drawing_data") or {}
    has_drawing = bool(drawing_data.get("entities") or drawing_data.get("elements"))
    org_id = state.get("org_id")
    runtime_meta = state.get("runtime_meta") or {}
    session_extra = state.get("session_extra") or {}
    spec_guid = state.get("spec_guid") or runtime_meta.get("spec_guid") or session_extra.get("spec_guid")
    active_ids_list = [str(x) for x in (state.get("active_object_ids") or []) if x]
    active_ids = set(active_ids_list)
    pending_fixes_in = state.get("pending_fixes") or []
    current_drawing_id = state.get("current_drawing_id") or ""

    hint = str(state.get("intent_hint") or "").strip()
    intent = "review" if hint == "review" else await _classify_intent(message)
    if intent == "answer" and _should_override_answer_to_review(message, has_drawing):
        intent = "review"

    context: dict[str, Any] = {
        "org_id": org_id,
        "spec_guid": spec_guid,
        "active_object_ids": active_ids,
        "active_object_ids_ordered": active_ids_list,
        "retrieved_laws": list(state.get("retrieved_laws") or []),
        "current_drawing_id": current_drawing_id,
        "drawing_loaded": has_drawing,
        "pending_fixes": pending_fixes_in,
        "intent": intent,
        "user_request": message,
        "drawing_data": drawing_data,
    }

    raw_layout: str | dict[str, Any] = "{}"
    if has_drawing and intent != "answer":
        context_mode = drawing_data.get("context_mode")
        focus_entities = (drawing_data.get("focus_extraction") or {}).get("entities")
        if context_mode == CONTEXT_MODE_FULL_WITH_FOCUS and focus_entities:
            focus_drawing = dict(drawing_data)
            focus_drawing["entities"] = focus_entities
            raw_layout = focus_drawing
        elif active_ids and not should_preserve_full_entities(drawing_data):
            raw_layout = _filter_entities_by_selection(drawing_data, active_ids)
        else:
            raw_layout = drawing_data

    context["raw_layout_data"] = raw_layout

    if has_drawing and intent in ("review", "fix_suggestion"):
        mapper = MappingAgent.get_instance(org_id=org_id)
        mapping_result = await mapper.execute_async(drawing_data)
        context["mapping_table"] = mapping_result
        context["style_map"] = mapping_result.get("style_map", {})
        context["entity_type_map"] = mapping_result.get("entity_type_map", {})
        context["is_mapped"] = True
    elif has_drawing and intent == "action":
        context["mapping_table"] = {}
        context["style_map"] = {}
        context["entity_type_map"] = {}
        context["is_mapped"] = False

    system_prompt = _build_system_prompt(context, memory_text)
    tool_calls = await llm_service.generate_answer(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": message},
        ],
        tools=FIRE_SUB_AGENT_TOOLS,
        tool_choice="auto",
    )

    if isinstance(tool_calls, str) and tool_calls.strip():
        if intent == "action" and has_drawing and active_ids:
            tool_calls = [_make_fallback_call(message, has_drawing, intent, list(active_ids))]
        elif intent == "answer" and not _is_casual_message(message):
            logger.info("[FireNode] answer 의도 직답 감지 -> call_query_agent 강제 라우팅")
            tool_calls = [_make_fallback_call(message, has_drawing, "answer", list(active_ids))]
        else:
            return await _format_state(state, [{"agent": "direct", "result": tool_calls.strip()}])

    if not isinstance(tool_calls, list) or not tool_calls:
        tool_calls = [_make_fallback_call(message, has_drawing, intent, list(active_ids))]

    for tc in tool_calls:
        if tc.get("function", {}).get("name") != "call_query_agent":
            continue
        args_str = tc["function"].get("arguments", "{}")
        try:
            args = json.loads(args_str) if isinstance(args_str, str) else dict(args_str or {})
            display_query = str(args.get("display_query") or args.get("query") or message or "").strip()
            retrieval_query = _expand_retrieval_query(display_query)
            args["display_query"] = display_query
            args["retrieval_query"] = retrieval_query
            args["query"] = retrieval_query
            args["limit"] = 15 if _is_broad_rag_query(display_query) else int(args.get("limit") or 8)
            tc["function"]["arguments"] = json.dumps(args, ensure_ascii=False)
            logger.info("[FireNode RAG] display_query=%r retrieval_query=%r limit=%s", display_query[:80], retrieval_query[:120], args["limit"])
        except Exception as exc:
            logger.debug("[FireNode] query anchor injection skipped: %s", exc)

    tool_names = [c["function"]["name"] for c in tool_calls if isinstance(c, dict) and c.get("function")]
    workflow = FireWorkflowHandler(session=context, db=db)
    workflow_results = await workflow.handle_tool_calls(tool_calls, context)
    if "call_query_agent" in tool_names:
        try:
            workflow_results = await _expand_fire_table_neighbors(workflow_results, db)
        except Exception as exc:
            logger.warning("[FireNode] table neighbor expand 실패: %s", exc)
    return await _format_state(state, workflow_results)


def _make_fallback_call(
    message: str,
    has_drawing: bool = False,
    intent: str = "answer",
    active_ids: list[str] | None = None,
) -> dict[str, Any]:
    ids = [str(x) for x in (active_ids or []) if x]
    if intent == "answer":
        return {
            "function": {
                "name": "call_query_agent",
                "arguments": json.dumps({"query": message or "소방 법규 질의"}, ensure_ascii=False),
            }
        }
    if has_drawing and intent in ("review", "fix_suggestion"):
        target_id = ids[0] if len(ids) == 1 else "ALL"
        return {
            "function": {
                "name": "call_review_agent",
                "arguments": json.dumps({"target_id": target_id}, ensure_ascii=False),
            }
        }
    if has_drawing and intent == "action":
        return {
            "function": {
                "name": "call_action_agent",
                "arguments": json.dumps({}, ensure_ascii=False),
            }
        }
    return {
        "function": {
            "name": "call_query_agent",
            "arguments": json.dumps({"query": message or "소방 기준 질의"}, ensure_ascii=False),
        }
    }


_CASUAL_RE = re.compile(
    r"^[\s!?.]*"
    r"(안녕|하이|hi|hello|반가워|반갑|고마워|감사|ㅎㅇ|ㅋ+|ㅠ+|ㅎ+|헬로|잘있어|bye|바이|"
    r"괜찮아|잘부탁|잘 부탁|맞아|그래|응|네|아니|알겠|좋아|오케이|ok|okay|수고)"
    r"[\s!?.]*$",
    re.IGNORECASE,
)
_DOMAIN_PREFIX_RE = re.compile(
    r"^(?:fire|firefighting|소방|elec|pipe|arch|electric|piping|architecture)_",
    re.IGNORECASE,
)
_DATE_SUFFIX_RE = re.compile(r"_\d{8}$")
_BAD_TABLE_HEADER_RE = re.compile(r"^열\d+$")
_EMPTY_TABLE_VALUES = {"", "-", "–", "—", "1", "0", "None", "none", "NULL", "null", "?", "□", "■", "�"}
_BROKEN_GLYPH_RE = re.compile(r"[�■-□▣-▩▯▱▲▼-]")
_NOISE_TOKEN_RE = re.compile(r"^(?:[○●◎◯]\s*)+$|(?:[○●◎◯]\s*\d+\)?)|(?:dun\s*){2,}", re.IGNORECASE)


def _is_casual_message(message: str) -> bool:
    text = (message or "").strip()
    return bool(_CASUAL_RE.match(text)) and len(text) < 30


def _strip_html(text: str) -> str:
    if not text:
        return text
    text = re.sub(r"<br\s*/?>|</?p>|</?div>|</?li>|</?tr>|</?td>|</?th>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
        .replace("&quot;", '"')
    )
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _pretty_doc_name(doc_name: str, category: str = "") -> str:
    name = (doc_name or "").strip()
    if not name or name == "—":
        return name
    name = _DOMAIN_PREFIX_RE.sub("", name)
    name = _DATE_SUFFIX_RE.sub("", name)
    name = name.replace("_", " ")
    cat = (category or "").strip()
    if cat and not name.startswith(cat):
        name = f"{cat} {name}"
    return name.strip()


def _chunk_to_meta_row(row: dict[str, Any]) -> dict[str, Any]:
    source = str(row.get("source") or "permanent")
    raw_doc_name = str(row.get("doc_name") or "").strip() or "—"
    category = str(row.get("category") or "")
    return {
        "source": source,
        "source_label": "영구_시방_DB" if source == "permanent" else "임시_시방_DB",
        "doc_name": raw_doc_name,
        "display_name": _pretty_doc_name(raw_doc_name, category),
        "document_id": str(row.get("document_id") or ""),
        "chunk_index": row.get("chunk_index"),
        "domain": str(row.get("domain") or ""),
        "category": category,
    }


def _citation_line_from_rows(rows: list[dict[str, Any]], *, max_show: int = 3) -> str:
    if not rows:
        return ""
    show = rows[:max_show]
    parts: list[str] = []
    for meta in show:
        name = (meta.get("display_name") or meta.get("doc_name") or "—")[:60]
        parts.append(f"«{name}»#{meta.get('chunk_index', '-')}")
    more = f" +{len(rows) - len(show)}" if len(rows) > len(show) else ""
    return " · ".join(parts) + more


def _retrieval_block_compact(meta_rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "n_chunks": len(meta_rows),
        "summary": _citation_line_from_rows(meta_rows, max_show=3) + (f" (총{len(meta_rows)}건)" if meta_rows else ""),
        "refs": [
            {
                "doc": (m.get("display_name") or m.get("doc_name") or "")[:80],
                "i": m.get("chunk_index"),
                "db": m.get("source_label", ""),
            }
            for m in meta_rows[:4]
        ],
    }


def _format_rag_footer(rows: list[dict[str, Any]], *, n_chunks: int) -> str:
    if n_chunks <= 0:
        return "\n\n---\n[출처] RAG: 일치 시방 청크 없음."
    line = _citation_line_from_rows(rows, max_show=3).strip()
    if not line:
        return f"\n\n---\n[출처] 시방RAG (총{n_chunks}건, 문서 메타 생략)."
    return f"\n\n---\n[출처] 시방RAG {line} (총{n_chunks}건)."


def _format_direct_footer() -> str:
    return "\n\n---\n[출처] 벡터DB 미검색·LLM 직답."


def _format_review_rag_footer(rag_refs: list[Any]) -> str:
    rows = [_chunk_to_meta_row(x) for x in (rag_refs or []) if isinstance(x, dict)]
    if not rows:
        return "\n\n---\n[출처] 시방RAG: 참고 청크 없음"
    return f"\n\n---\n[출처] 검토 참고 {_citation_line_from_rows(rows, max_show=3)} (총{len(rows)}건)"


def _lines_to_bullet_block(text: str) -> str:
    text = text.strip()
    if not text:
        return text
    out: list[str] = []
    for line in re.split(r"\r?\n", text):
        item = line.strip()
        if not item:
            out.append("")
            continue
        if item.startswith(("-", "*", "•")):
            out.append("- " + item[1:].strip())
            continue
        m = re.match(r"^(\(?\d{1,2}\)?|[가-힣])[\).:]\s+(.+)$", item)
        if m:
            out.append(f"- **{m.group(1)}.** {m.group(2).strip()}")
            continue
        out.append(item)
    return "\n".join(out)


def _spec_text_to_readable_markdown(text: str) -> str:
    if not text or not text.strip():
        return text
    text = text.strip()
    parts = re.split(r"(?:\r?\n)\s*---\s*(?:\r?\n)?|\n\s*---\s*\n|\s+---\s+|\n---\n", text)
    parts = [p.strip() for p in parts if p.strip()]
    if not parts:
        return text
    if len(parts) == 1:
        return _lines_to_bullet_block(parts[0])
    return "\n\n".join(f"### {i} · 요약\n\n{_lines_to_bullet_block(p)}" for i, p in enumerate(parts, 1))


def _detect_answer_style(user_request: str) -> str:
    q = (user_request or "").strip()
    q_lower = q.lower()
    if any(k in q for k in ("자동 판정 조건", "자동판정", "CAD 판정", "자동 검토", "rule JSON", "JSON으로 변환")):
        return "auto_review_report"
    if any(k in q for k in ("도면", "검토", "위반", "판정", "표로", "표 형식", "테이블")) or "cad" in q_lower:
        return "review_table"
    if any(k in q for k in ("정리", "요약", "한눈에", "정돈", "레포트", "보고서", "리포트")):
        return "structured_summary"
    if any(k in q for k in ("간격", "거리", "수치", "얼마", "기준값", "규격", "압력", "유량", "구경")):
        return "focused_detail"
    if any(k in q for k in ("설명", "알려", "뭐야", "무엇", "대해")):
        return "simple_explain"
    return "structured_summary"


def _style_instruction(answer_style: str) -> str:
    if answer_style == "focused_detail":
        return (
            "[답변 형식 - focused_detail]\n"
            "표를 새로 만들지 말고 사용자가 물은 특정 항목만 간결하게 정리하세요.\n"
            "### 핵심 기준\n- 기준명/조항:\n- 적용 대상:\n\n"
            "### 세부 내용\n- 검색 결과의 실제 수치·단위·조건을 직접 적으세요.\n\n"
            "### 적용 및 주의사항\n- 예외와 추가 확인 사항만 검색 결과 기반으로 적으세요.\n"
        )
    if answer_style == "review_table":
        return (
            "[답변 형식 - cad_review]\n"
            "### 설계·시공 기준\n"
            "| 항목 | 적용 대상 | 조건 | 요구사항 | 정량 기준 | 예외/주의 |\n"
            "|---|---|---|---|---|---|\n"
            "| 검색 결과 기반 | ... | ... | 실제 값 명시 | 수치/정량값 또는 없음 | ... |\n\n"
            "### CAD 검토 포인트\n- 레이어, 블록 속성, 텍스트 주석, 색상 등 판정 가능한 요소로 변환하세요.\n\n"
            "### 자동 판정 가능 여부\n- 가능 / 부분 가능 / 불가로 구분하세요.\n\n"
            "### 한계\n- 도면만으로 판단 불가한 정보 부족 사항을 적으세요.\n"
        )
    if answer_style == "auto_review_report":
        return (
            "[답변 형식 - auto_validation]\n"
            "JSON, dict, 배열은 출력하지 마세요.\n"
            "### 자동 검토 가능 항목\n- CAD로 확인 가능한 항목을 나열하세요.\n\n"
            "### CAD 판정 방식\n- 레이어, 블록명, 속성값, 텍스트 주석 등 판정 방식을 적으세요.\n\n"
            "### 자동 판정 가능 여부\n| 항목 | 가능 여부 |\n|---|---|\n| 항목명 | 가능 / 부분 가능 / 불가 |\n\n"
            "### 필요 CAD 데이터\n- 필요한 CAD 속성을 적으세요.\n\n"
            "### 한계\n- 검색 결과에 없는 조건은 만들지 마세요.\n"
        )
    if answer_style == "simple_explain":
        return (
            "[답변 형식 - simple_explain]\n"
            "### 개념 및 목적\n- 사용자가 물은 기준/개념을 정의하세요.\n\n"
            "### 주요 기준 및 적용\n- 검색 결과에 있는 기준만 정리하고 실제 수치가 있으면 직접 포함하세요.\n\n"
            "### CAD 검토 포인트\n- CAD에서 확인 가능한 속성을 적으세요.\n\n"
            "### 적용 시 주의사항\n- 예외와 한계를 적으세요.\n"
        )
    return (
        "[답변 형식 - structured_summary]\n"
        "### 기준 요약\n- 기준명:\n- 적용범위:\n- 핵심 원칙:\n\n"
        "### 주요 기준\n- 검색 결과의 핵심 기준과 실제 수치·단위를 항목별로 정리하세요.\n\n"
        "### CAD 검토 가능성\n- 자동 검토 가능:\n- 부분 가능:\n- 도면만으로 판단 불가:\n\n"
        "### 적용 및 주의사항\n- 예외/주의사항/추가 확인 필요 사항을 적으세요.\n"
    )


def _build_rag_answer_prompt(*, user_request: str, context_text: str, answer_style: str) -> str:
    return (
        "당신은 소방 법규·시방서·설계기준을 근거 기반으로 정리하는 엔지니어링 AI입니다.\n"
        "아래 [검색 결과]에 실제로 포함된 내용만 근거로 답하세요.\n\n"
        "[절대 규칙]\n"
        "1. section=, chunk=, type=table 같은 내부 메타를 출력하지 마세요.\n"
        "2. 검색 결과에 없는 기준명, 수치, KS/KCS/NFSC 코드, 단위를 만들지 마세요.\n"
        "3. mm, m, MPa, kPa, L/min 같은 수치와 단위는 임의 변경하지 마세요.\n"
        "4. 표 내용과 일반 설명이 충돌하면 표의 값을 우선하세요.\n"
        "5. 정상 markdown table은 유지할 수 있지만 핵심 행 값은 본문 bullet에도 직접 반영하세요.\n"
        "6. 열1/열2/열3, □, ■, �, PUA 문자가 많은 깨진 표는 렌더링하지 말고 의미 있는 셀만 bullet로 복구하세요.\n"
        "7. '표에 따른다'만 반복하지 말고 실제 표 값이 있으면 답변에 포함하세요. 표 값이 없으면 없다고 밝히세요.\n"
        "8. 문서 표지, 목차, 연락처, 위원 명단은 제외하세요.\n\n"
        f"{_style_instruction(answer_style)}\n"
        f"[검색 결과]\n{context_text}\n\n"
        f"사용자 질문: {user_request}"
    )


def _clean_followup_base(user_request: str) -> str:
    base = (user_request or "소방 기준").strip()
    remove_patterns = [
        r"\s*리포트\s*형식으로\s*정리\s*$",
        r"\s*보고서\s*형식으로\s*정리\s*$",
        r"\s*CAD\s*검토\s*기준\s*(중심으로)?\s*(설명|정리)?\s*$",
        r"\s*자동\s*검토\s*가능\s*조건\s*(으로)?\s*정리\s*$",
        r"\s*자동\s*판정\s*조건\s*(으로)?\s*변환\s*$",
        r"\s*정리\s*$",
        r"\s*요약\s*$",
        r"\s*설명\s*$",
    ]
    for pattern in remove_patterns:
        base = re.sub(pattern, "", base, flags=re.IGNORECASE).strip()
    base = re.sub(r"\s*(에\s*대해|에\s*대한|관련|기준을|기준|에\s*관해)\s*$", "", base).strip()
    return base or "소방 기준"


def _is_broad_rag_query(query: str) -> bool:
    q = (query or "").lower()
    return any(k in q for k in (
        "정리", "설명", "요약", "리포트", "보고서", "기준", "무엇", "대해",
        "검토 기준", "자동 판정", "자동 검토", "cad 판정", "rule", "규정", "시방",
        "explain", "summary", "report", "review", "validation", "criteria",
    ))


def _add_anchor_if_matched(topic: str, triggers: tuple[str, ...], anchors: list[str], bucket: list[str]) -> None:
    t = topic.lower()
    if any(k.lower() in t for k in triggers):
        bucket.extend(anchors)


def _build_search_anchor_from_topic(topic: str) -> str:
    topic = topic or ""
    anchors: list[str] = []
    _add_anchor_if_matched(topic, ("스프링클러", "sprinkler", "헤드", "살수", "간격"), ["스프링클러", "헤드 설치 간격", "살수반경", "NFSC"], anchors)
    _add_anchor_if_matched(topic, ("감지기", "detector", "연기", "열감지"), ["감지기", "설치 높이", "감지 면적", "설치 간격", "NFSC"], anchors)
    _add_anchor_if_matched(topic, ("소화전", "hydrant", "방수압", "방수량"), ["옥내소화전", "방수압력", "방수량", "이격거리", "NFSC"], anchors)
    _add_anchor_if_matched(topic, ("소화기", "extinguisher", "보행거리", "능력단위"), ["소화기", "보행거리", "능력단위", "소방시설법"], anchors)
    _add_anchor_if_matched(topic, ("펌프", "pump", "토출", "양정", "유량"), ["소방펌프", "토출압력", "양정", "유량", "NFSC"], anchors)
    _add_anchor_if_matched(topic, ("배관", "밸브", "플랜지", "구경", "관경"), ["소방 배관", "밸브", "플랜지", "구경", "KS", "KCS"], anchors)
    _add_anchor_if_matched(topic, ("방화문", "방화구획", "제연", "내화"), ["방화문", "방화구획", "제연설비", "소방시설법", "건축법"], anchors)
    anchors.extend(["NFSC", "화재안전기술기준", "KCS", "KS"])
    seen: set[str] = set()
    uniq: list[str] = []
    for anchor in anchors:
        if anchor and anchor not in seen:
            seen.add(anchor)
            uniq.append(anchor)
    return " ".join(uniq)


def _expand_retrieval_query(display_query: str) -> str:
    query = (display_query or "").strip()
    anchor = _build_search_anchor_from_topic(query)
    if anchor and anchor not in query:
        return f"{query} {anchor}".strip()
    return query


def _make_followup_query(topic: str, suffix: str) -> str:
    return f"{(topic or '소방 기준').strip()} {suffix}".strip()


def _build_suggested_queries(user_request: str, answer_style: str) -> list[dict[str, str]]:
    topic = _clean_followup_base(user_request)
    suggestions: list[dict[str, str]] = []

    def item(label: str, suffix: str) -> dict[str, str]:
        display_query = _make_followup_query(topic, suffix)
        return {
            "label": label,
            "query": display_query,
            "display_query": display_query,
            "retrieval_query": _expand_retrieval_query(display_query),
        }

    if answer_style != "structured_summary":
        suggestions.append(item("리포트 형식으로 보기", "리포트 형식으로 정리"))
    if answer_style != "review_table":
        suggestions.append(item("도면 검토 기준으로 보기", "CAD 검토 기준 중심으로 정리"))
    if answer_style != "auto_review_report":
        suggestions.append(item("자동 판정 조건으로 변환", "자동 검토 가능 조건으로 정리"))
    return suggestions[:3]


def _split_markdown_table_row(line: str) -> list[str]:
    if not line:
        return []
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _is_markdown_separator(line: str) -> bool:
    if not line or "|" not in line:
        return False
    cells = _split_markdown_table_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c.strip()) for c in cells if c.strip())


def _clean_table_cell(value: str) -> str:
    value = _strip_html(str(value or "")).strip()
    value = _BROKEN_GLYPH_RE.sub("", value)
    value = re.sub(r"[ \t]{2,}", " ", value)
    return value.strip()


def _is_empty_or_noise_cell(value: str) -> bool:
    value = _clean_table_cell(value)
    if value in _EMPTY_TABLE_VALUES:
        return True
    if _BAD_TABLE_HEADER_RE.match(value):
        return True
    if _NOISE_TOKEN_RE.search(value):
        return True
    return bool(re.fullmatch(r"[○●◎◯①-⑳\s\)\(]+", value))


def _is_bad_markdown_table(markdown: str) -> bool:
    if not markdown or "|" not in markdown:
        return False
    lines = [line.strip() for line in markdown.splitlines() if line.strip()]
    if len(lines) < 2:
        return False
    headers = _split_markdown_table_row(lines[0])
    bad_headers = sum(1 for h in headers if _BAD_TABLE_HEADER_RE.match(_clean_table_cell(h)) or _is_empty_or_noise_cell(h))
    meaningful_headers = sum(1 for h in headers if not _is_empty_or_noise_cell(h))
    data_cells: list[str] = []
    for line in lines[1:]:
        if not _is_markdown_separator(line):
            data_cells.extend(_split_markdown_table_row(line))
    empty_cells = sum(1 for c in data_cells if _is_empty_or_noise_cell(c))
    broken_ratio = len(_BROKEN_GLYPH_RE.findall(markdown)) / max(len(markdown), 1)
    return (
        bad_headers / max(len(headers), 1) >= 0.4
        or empty_cells / max(len(data_cells), 1) >= 0.55
        or broken_ratio >= 0.02
        or meaningful_headers <= 1
    )


def _clean_bad_markdown_table(markdown: str) -> str:
    markdown = _strip_html(markdown or "")
    if not markdown:
        return ""
    if not _is_bad_markdown_table(markdown):
        return markdown
    lines = [line.strip() for line in markdown.splitlines() if line.strip()]
    if len(lines) < 3:
        return markdown
    headers = [_clean_table_cell(h) for h in _split_markdown_table_row(lines[0])]
    meaningful_cols = [i for i, h in enumerate(headers) if h and not _is_empty_or_noise_cell(h)]
    out: list[str] = []
    seen: set[str] = set()
    for line in lines[1:]:
        if _is_markdown_separator(line):
            continue
        cells = [_clean_table_cell(c) for c in _split_markdown_table_row(line)]
        parts: list[str] = []
        for i in meaningful_cols:
            if i < len(cells) and not _is_empty_or_noise_cell(cells[i]):
                parts.append(f"{headers[i]}: {cells[i]}" if headers[i] else cells[i])
        if not parts:
            parts = [c for c in cells if c and not _is_empty_or_noise_cell(c)]
        if parts:
            line_text = " / ".join(parts).strip()
            if line_text and line_text not in seen:
                seen.add(line_text)
                out.append(f"- {line_text}")
    return "\n".join(out)


def _restore_collapsed_table_text(text: str) -> str:
    text = _strip_html(text or "")
    if "|" in text or len(text) < 120:
        return text
    if not any(k in text for k in ("재료명", "규격", "적용", "주의", "구분", "기준", "호칭", "관경")):
        return text
    parts = re.split(r"\s{2,}|(?<=다\.)\s+|(?<=\))\s+", text)
    rows = []
    for part in parts:
        cleaned = _clean_table_cell(part)
        if cleaned and not _is_empty_or_noise_cell(cleaned):
            rows.append(cleaned)
    return "\n".join(f"- {row}" for row in rows[:30]) if len(rows) >= 3 else text


def _get_rag_content_from_row(row: dict[str, Any]) -> str:
    if not isinstance(row, dict):
        return ""
    context_content = str(row.get("context_content") or "").strip()
    if context_content:
        return _clean_bad_markdown_table(context_content) if "|" in context_content and _is_bad_markdown_table(context_content) else _restore_collapsed_table_text(context_content)
    chunk_type = str(row.get("chunk_type") or "").strip()
    table_markdown = str(row.get("table_markdown") or "").strip()
    if chunk_type == "table" and table_markdown:
        return _clean_bad_markdown_table(table_markdown) if _is_bad_markdown_table(table_markdown) else _strip_html(table_markdown)
    for key in ("content", "raw_content"):
        content = str(row.get(key) or "").strip()
        if not content:
            continue
        if "|" in content and _is_bad_markdown_table(content):
            return _clean_bad_markdown_table(content)
        return _restore_collapsed_table_text(content)
    return ""


def _contains_table_value_hint(row: dict[str, Any]) -> bool:
    text = " ".join(str(row.get(k) or "") for k in ("content", "raw_content", "table_markdown"))
    return any(hint in text for hint in _FIRE_TABLE_VALUE_HINTS)


def _table_priority(row: dict[str, Any]) -> tuple[int, int, int, int]:
    if not isinstance(row, dict):
        return (9, 9, 9, 999999)
    chunk_type = str(row.get("chunk_type") or "")
    has_table = bool(str(row.get("table_markdown") or "").strip())
    try:
        idx = int(row.get("chunk_index") or 999999)
    except (TypeError, ValueError):
        idx = 999999
    return (
        0 if chunk_type == "table" or has_table else 1,
        0 if row.get("_neighbor_expanded") else 1,
        0 if _contains_table_value_hint(row) else 1,
        idx,
    )


def _rag_chunks_to_readable_markdown(chunks: list[dict[str, Any]], *, max_chunks: int = 8) -> str:
    out: list[str] = []
    for idx, row in enumerate(sorted([r for r in chunks if isinstance(r, dict)], key=_table_priority), start=1):
        if len(out) >= max_chunks:
            break
        raw_table = str(row.get("table_markdown") or "").strip()
        content = _get_rag_content_from_row(row)
        if not content:
            continue
        name = _pretty_doc_name(row.get("doc_name") or f"시방 발췌 {idx}", row.get("category") or "")
        if len(name) > 100:
            name = name[:97] + "..."
        chunk_type = str(row.get("chunk_type") or "").strip()
        tags = (" · table" if chunk_type == "table" else "") + (" · nearby" if row.get("_neighbor_expanded") else "")
        if chunk_type == "table" and raw_table and not _is_bad_markdown_table(raw_table):
            inner = content
        else:
            inner = _spec_text_to_readable_markdown(content) if "---" in content else _lines_to_bullet_block(content)
        out.append(f"#### {name}{tags}\n\n{inner}")
    return "\n\n".join(out) if out else "관련 시방 조항을 찾지 못했습니다."


async def _expand_fire_table_neighbors(
    workflow_results: list[dict[str, Any]],
    db: Any,
    *,
    window: int = 3,
    max_added_per_block: int = 8,
) -> list[dict[str, Any]]:
    from sqlalchemy import cast as _sa_cast, select as _sa_select
    from sqlalchemy.dialects.postgresql import UUID as _PG_UUID
    from backend.models.schema import DocumentChunk as _DocumentChunk

    expanded: list[dict[str, Any]] = []
    for block in workflow_results or []:
        if block.get("agent") != "query" or not isinstance(block.get("result"), list):
            expanded.append(block)
            continue
        result = block["result"]
        existing = {
            (str(row.get("document_id") or ""), str(row.get("chunk_index") or ""))
            for row in result
            if isinstance(row, dict)
        }
        refs: list[dict[str, int | str]] = []
        for row in result:
            if not isinstance(row, dict) or row.get("chunk_type") == "table":
                continue
            content = str(row.get("content") or row.get("raw_content") or "")
            if not _HAS_TABLE_REF_RE.search(content):
                continue
            doc_id = str(row.get("document_id") or "")
            try:
                chunk_idx = int(row.get("chunk_index") or 0)
            except (TypeError, ValueError):
                continue
            if doc_id:
                refs.append({"doc_id": doc_id, "chunk_idx": chunk_idx})
        if not refs:
            expanded.append(block)
            continue

        new_chunks: list[dict[str, Any]] = []
        for ref in refs:
            try:
                stmt = (
                    _sa_select(_DocumentChunk)
                    .where(_DocumentChunk.document_id == _sa_cast(str(ref["doc_id"]), _PG_UUID(as_uuid=False)))
                    .where(_DocumentChunk.chunk_index >= int(ref["chunk_idx"]) - window)
                    .where(_DocumentChunk.chunk_index <= int(ref["chunk_idx"]) + window)
                    .where(_DocumentChunk.chunk_type == "table")
                )
                rows = await db.execute(stmt)
                neighbors = rows.scalars().all()
            except Exception as exc:
                logger.warning("[FireNode TABLE EXPAND] DB 조회 실패 doc=%s idx=%s err=%s", ref["doc_id"], ref["chunk_idx"], exc)
                try:
                    await db.rollback()
                except Exception:
                    pass
                continue
            for nb in neighbors:
                key = (str(nb.document_id), str(nb.chunk_index))
                if key in existing:
                    continue
                existing.add(key)
                table_markdown = str(nb.table_markdown or "")
                new_chunks.append({
                    "source": "permanent",
                    "id": nb.id,
                    "document_id": str(nb.document_id),
                    "chunk_index": nb.chunk_index,
                    "content": table_markdown if table_markdown else str(nb.content or ""),
                    "raw_content": str(nb.content or ""),
                    "table_markdown": table_markdown or None,
                    "chunk_type": nb.chunk_type,
                    "domain": nb.domain,
                    "category": nb.category,
                    "doc_name": nb.doc_name,
                    "section_id": nb.section_id,
                    "page_number": None,
                    "_neighbor_expanded": True,
                })
                if len(new_chunks) >= max_added_per_block:
                    break
            if len(new_chunks) >= max_added_per_block:
                break
        expanded.append({**block, "result": list(result) + new_chunks})
    return expanded


async def _format_state(state: AgentState, workflow_results: list[dict[str, Any]]) -> AgentState:
    violations: list[ViolationItem] = []
    suggestions: list[str] = []
    pending_fixes: list[PendingFix] = []
    referenced_laws: list[str] = []
    retrieved_laws: list[LawReference] = list(state.get("retrieved_laws") or [])
    final_message = ""
    current_step: CurrentStep = "agent_completed"
    response_meta: dict[str, Any] = {}
    user_request = str(state.get("user_request") or "").strip()

    for block in workflow_results or []:
        agent = block.get("agent")
        result = block.get("result")

        if agent == "query" and isinstance(result, list):
            retrieved_laws = _to_law_references(result)
            ch_list = [
                row for row in result
                if isinstance(row, dict)
                and (
                    str(row.get("content") or "").strip()
                    or str(row.get("table_markdown") or "").strip()
                    or str(row.get("raw_content") or "").strip()
                    or str(row.get("context_content") or "").strip()
                )
            ]
            ch_list = sorted(ch_list, key=_table_priority)
            answer_style = _detect_answer_style(user_request)
            if ch_list:
                context_text = _rag_chunks_to_readable_markdown(ch_list)
                prompt = _build_rag_answer_prompt(
                    user_request=user_request,
                    context_text=context_text,
                    answer_style=answer_style,
                )
                try:
                    summary = await llm_service.generate_answer([{"role": "user", "content": prompt}])
                    final_message = summary if isinstance(summary, str) and summary.strip() else context_text
                except Exception as exc:
                    logger.warning("[FireNode] RAG summary generation failed: %s", exc, exc_info=True)
                    final_message = context_text
            else:
                final_message = (
                    "관련 소방 법령 근거 문서를 찾지 못했습니다. "
                    "질문을 좀 더 구체적으로 입력하시거나, "
                    "NFSC 또는 소방시설법 관련 문서를 업로드해 주세요."
                )
            current_step = "query_completed"
            meta_rows = [_chunk_to_meta_row(row) for row in result if isinstance(row, dict)]
            final_message = _strip_html(final_message)
            final_message += _format_rag_footer(meta_rows, n_chunks=len(result))
            response_meta = {
                "answer_type": "rag_query",
                "used_rag": bool(result),
                "answer_style": answer_style,
                "suggested_queries": _build_suggested_queries(user_request, answer_style) if ch_list else [],
                "retrieval": _retrieval_block_compact(meta_rows),
            }

        elif agent == "review" and isinstance(result, dict):
            report = result.get("report") or {}
            fixes = result.get("fixes") or []
            items = report.get("items") or report.get("results") or []
            rag_refs = result.get("rag_references") or []
            det_items = result.get("deterministic_violations") or []
            laws = _to_law_references(rag_refs)
            if laws:
                retrieved_laws = laws
            violations = _violations_from_items(items)
            pending_fixes = _build_pending_fixes(fixes, items, retrieved_laws)
            suggestions = [f["description"] for f in pending_fixes if f.get("description")]
            referenced_laws = list({v.get("legal_reference", "") for v in violations if v.get("legal_reference")})
            total = report.get("total_violations", len(violations))
            final_message = (
                f"소방 검토 완료: 위반 {total}건을 확인했습니다. 수정 후보를 검토해 주세요."
                + _format_review_rag_footer(rag_refs)
            )
            current_step = "pending_fix_review"
            rmeta = [_chunk_to_meta_row(x) for x in rag_refs if isinstance(x, dict)]
            response_meta = {
                "answer_type": "review",
                "used_rag": bool(rmeta),
                "retrieval": _retrieval_block_compact(rmeta),
                "review_categories": {
                    "rag_reference_count": len(rag_refs),
                    "deterministic_count": len(det_items),
                },
                # KPI(recall_lower_bound) 산출용 — 결정론적 검사가 추가되면 자동으로 활성화.
                # 현재 fire 도메인은 deterministic_checker 미구현이라 빈 리스트.
                "deterministic_equipment_ids": sorted({
                    str(v.get("object_id") or v.get("equipment_id") or v.get("handle") or "")
                    for v in det_items
                    if isinstance(v, dict)
                    and (v.get("object_id") or v.get("equipment_id") or v.get("handle"))
                }),
                "sllm_durations_ms": list(result.get("sllm_durations_ms") or []),
            }

        elif agent == "action" and isinstance(result, dict):
            action_fixes = result.get("fixes") or []
            pending_fixes = _pending_from_action_fixes(action_fixes)
            violations = _violations_from_pending_fixes(pending_fixes)
            suggestions = [f["description"] for f in pending_fixes if f.get("description")]
            final_message = str(result.get("message") or "선택 객체 수정안을 생성했습니다.")
            current_step = "pending_fix_review"
            response_meta = {"answer_type": "action_suggestion", "used_rag": False}

        elif agent == "cad_info":
            final_message = f"CAD 객체 정보 조회 결과:\n{result}"
            current_step = "agent_completed"
            response_meta = {"answer_type": "cad_entity_lookup", "used_rag": False}

        elif agent == "direct" and isinstance(result, str):
            final_message = _spec_text_to_readable_markdown(result) + _format_direct_footer()
            current_step = "agent_completed"
            response_meta = {"answer_type": "llm_direct", "used_rag": False, "note": "RAG 미사용·직답"}

    if not final_message:
        final_message = "처리가 완료되었습니다."
        if not response_meta:
            response_meta = {"answer_type": "empty", "used_rag": False}

    invoked = [
        block.get("agent")
        for block in (workflow_results or [])
        if isinstance(block, dict) and block.get("agent")
    ]
    if invoked:
        response_meta = {**response_meta, "invoked_workflow": invoked}

    if not violations and pending_fixes:
        violations = _violations_from_pending_fixes(pending_fixes)

    final_message = _strip_html(final_message)

    review_result: ReviewResult = {
        "is_violation": len(violations) > 0,
        "violations": violations,
        "suggestions": suggestions,
        "referenced_laws": referenced_laws,
        "final_message": final_message,
    }

    return {
        **state,
        "review_result": review_result,
        "current_step": current_step,
        "assistant_response": final_message,
        "retrieved_laws": retrieved_laws,
        "pending_fixes": pending_fixes,
        "response_meta": response_meta,
    }


def _to_law_references(query_result: list[dict[str, Any]]) -> list[LawReference]:
    refs: list[LawReference] = []
    for row in query_result or []:
        if not isinstance(row, dict):
            continue
        rid = row.get("id")
        ref: LawReference = {
            "chunk_id": str(rid) if rid is not None else str(row.get("section_id") or row.get("chunk_index") or ""),
            "document_id": str(row.get("document_id") or ""),
            "legal_reference": str(row.get("section_id") or row.get("doc_name") or ""),
            "snippet": str(_get_rag_content_from_row(row) or ""),
            "score": float(row.get("score") or 0.0),
            "source_type": str(row.get("source") or "permanent"),
        }
        if isinstance(rid, int):
            ref["document_chunk_id"] = rid
        refs.append(ref)
    return refs


def _violations_from_items(items: list[dict[str, Any]]) -> list[ViolationItem]:
    out: list[ViolationItem] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "object_id": str(item.get("handle") or item.get("equipment_id") or ""),
                "violation_type": str(item.get("violation_type") or ""),
                "reason": str(item.get("reason") or ""),
                "legal_reference": str(item.get("reference_rule") or ""),
                "suggestion": str(item.get("required_value") or item.get("reason") or ""),
                "current_value": str(item.get("current_value") or ""),
                "required_value": str(item.get("required_value") or ""),
            }
        )
    return out


def _violations_from_pending_fixes(pending: list[PendingFix]) -> list[ViolationItem]:
    out: list[ViolationItem] = []
    for row in pending or []:
        out.append(
            {
                "object_id": str(row.get("equipment_id") or ""),
                "violation_type": str(row.get("violation_type") or ""),
                "reason": str(row.get("description") or ""),
                "legal_reference": "",
                "suggestion": str(row.get("description") or ""),
                "current_value": "",
                "required_value": "",
            }
        )
    return out


def _ref_chunk_id_for_violation(violation: dict[str, Any], laws: list[LawReference]) -> int | None:
    ref = str(violation.get("reference_rule") or "").strip()
    if not ref:
        return None
    for law in laws or []:
        law_ref = str(law.get("legal_reference") or "").strip()
        if not law_ref or (law_ref not in ref and ref not in law_ref):
            continue
        doc_chunk_id = law.get("document_chunk_id")
        if isinstance(doc_chunk_id, int):
            return doc_chunk_id
        chunk_id = law.get("chunk_id")
        if isinstance(chunk_id, str) and chunk_id.isdigit():
            return int(chunk_id)
    return None


def _build_pending_fixes(
    fixes: list[dict[str, Any]],
    violation_items: list[dict[str, Any]],
    retrieved_laws: list[LawReference] | None = None,
) -> list[PendingFix]:
    laws = retrieved_laws or []
    violation_map = {item.get("equipment_id"): item for item in violation_items if isinstance(item, dict)}
    result: list[PendingFix] = []
    for fix in fixes or []:
        if not isinstance(fix, dict):
            continue
        equipment_id = fix.get("equipment_id", "")
        proposed = fix.get("proposed_fix") or {}
        proposed = dict(proposed)
        if not proposed.get("_entity_bbox") and str(proposed.get("action") or "").upper() == "CREATE_BLOCK":
            try:
                bx = float(proposed.get("base_x"))
                by = float(proposed.get("base_y"))
                pad = 1_000.0
                proposed["_entity_bbox"] = {
                    "x1": bx - pad,
                    "y1": by - pad,
                    "x2": bx + pad,
                    "y2": by + pad,
                }
            except (TypeError, ValueError):
                pass
        violation = violation_map.get(equipment_id, {})
        row: PendingFix = {
            "fix_id": str(uuid.uuid4()),
            "equipment_id": str(equipment_id or ""),
            "violation_type": str(violation.get("violation_type") or ""),
            "action": str(proposed.get("action") or ""),
            "description": str(violation.get("reason") or proposed.get("reason") or ""),
            "proposed_fix": proposed,
        }
        ref_chunk_id = _ref_chunk_id_for_violation(violation, laws)
        if ref_chunk_id is not None:
            row["reference_chunk_id"] = ref_chunk_id
        result.append(row)
    return result


def _pending_from_action_fixes(fixes: list[dict[str, Any]]) -> list[PendingFix]:
    out: list[PendingFix] = []
    for fix in fixes or []:
        if not isinstance(fix, dict):
            continue
        auto_fix = dict(fix.get("auto_fix") or {})
        out.append(
            {
                "fix_id": str(uuid.uuid4()),
                "equipment_id": str(fix.get("handle") or ""),
                "violation_type": str(fix.get("action") or "ACTION_REQUIRED"),
                "action": str(fix.get("action") or auto_fix.get("type") or ""),
                "description": str(fix.get("reason") or ""),
                "proposed_fix": auto_fix,
            }
        )
    return out
