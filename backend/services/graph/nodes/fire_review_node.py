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
        "검토", "리뷰", "위반", "법규", "기준", "점검", "진단",
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
    active_ids = {str(x) for x in (state.get("active_object_ids") or []) if x}
    pending_fixes_in = state.get("pending_fixes") or []
    current_drawing_id = state.get("current_drawing_id") or ""

    hint = str(state.get("intent_hint") or "").strip()
    intent = "review" if hint == "review" else await _classify_intent(message)
    if has_drawing and intent == "answer" and re.search(r"(검토|위반|법규|기준|점검)", message):
        intent = "review"

    context: dict[str, Any] = {
        "org_id": org_id,
        "spec_guid": spec_guid,
        "active_object_ids": active_ids,
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
        else:
            return await _format_state(state, [{"agent": "direct", "result": tool_calls.strip()}])

    if not isinstance(tool_calls, list) or not tool_calls:
        tool_calls = [_make_fallback_call(message, has_drawing, intent, list(active_ids))]

    workflow = FireWorkflowHandler(session=context, db=db)
    workflow_results = await workflow.handle_tool_calls(tool_calls, context)
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


async def _format_state(state: AgentState, workflow_results: list[dict[str, Any]]) -> AgentState:
    violations: list[ViolationItem] = []
    suggestions: list[str] = []
    pending_fixes: list[PendingFix] = []
    referenced_laws: list[str] = []
    retrieved_laws: list[LawReference] = list(state.get("retrieved_laws") or [])
    final_message = ""
    current_step: CurrentStep = "agent_completed"
    response_meta: dict[str, Any] = {}

    for block in workflow_results or []:
        agent = block.get("agent")
        result = block.get("result")

        if agent == "query" and isinstance(result, list):
            retrieved_laws = _to_law_references(result)
            snippets = [str(r.get("content") or "").strip() for r in result if isinstance(r, dict)]
            final_message = "\n\n".join([s for s in snippets[:3] if s]) or "관련 소방 규정을 찾지 못했습니다."
            current_step = "query_completed"
            response_meta = {"answer_type": "rag_query", "used_rag": bool(result)}

        elif agent == "review" and isinstance(result, dict):
            report = result.get("report") or {}
            fixes = result.get("fixes") or []
            items = report.get("items") or report.get("results") or []
            rag_refs = result.get("rag_references") or []
            laws = _to_law_references(rag_refs)
            if laws:
                retrieved_laws = laws
            violations = _violations_from_items(items)
            pending_fixes = _build_pending_fixes(fixes, items, retrieved_laws)
            suggestions = [f["description"] for f in pending_fixes if f.get("description")]
            referenced_laws = list({v.get("legal_reference", "") for v in violations if v.get("legal_reference")})
            total = report.get("total_violations", len(violations))
            final_message = f"소방 검토 완료: 위반 {total}건을 확인했습니다. 수정 후보를 검토해 주세요."
            current_step = "pending_fix_review"
            response_meta = {"answer_type": "review", "used_rag": bool(rag_refs)}

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
            final_message = result
            current_step = "agent_completed"
            response_meta = {"answer_type": "llm_direct", "used_rag": False}

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
            "snippet": str(row.get("content") or ""),
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
