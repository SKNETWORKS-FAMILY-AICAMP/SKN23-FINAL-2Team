"""
File    : backend/services/graph/nodes/arch_review_node.py
Author  : 김다빈
Create  : 2026-04-25
Description : 건축 도메인 LangGraph 노드.
              AgentState를 입력받아 의도 분류 → 매핑 → tool 선택 → 서브 에이전트 실행 후
              review_result / current_step / assistant_response 를 채워 반환합니다.
              배관(pipe_review_node.py) 패턴과 동일하게 AgentState TypedDict를 완전히 반환합니다.
              건축은 arch 레이어가 1차 검토 대상이므로 레이어 분리(arch_pipe_layer_split)를 사용하지 않습니다.

Modification History :
    - 2026-04-25 (김다빈) : 신규 생성 — 공통 템플릿(review_agent_template.py)에서 분리.
                            의도 분류, intent_hint 빠른 경로, 이름/위치 병렬 매핑,
                            AgentState 완전 반환, RevCloud handle 정합 포함.
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
from backend.services.agents.arch.arch_agent import ArchAgent
from backend.services.agents.arch.schemas import ARCH_SUB_AGENT_TOOLS
from backend.services.agents.arch.sub.mapping import MappingAgent
from backend.services.agents.arch.workflow_handler import ArchWorkflowHandler
from backend.services.agents.common.object_mapping_utils import run_object_mapping
from backend.services.graph.prompt_utils import build_memory_prompt_from_state
from backend.services.graph.state import (
    AgentState,
    CurrentStep,
    LawReference,
    PendingFix,
    ReviewResult,
    ViolationItem,
)


# ── LangGraph 노드 진입점 ─────────────────────────────────────────────────────

async def arch_review_node(state: AgentState) -> AgentState:
    """
    건축 도메인 LangGraph 노드.

    [의무 반환 키]
      - review_result    : ReviewResult TypedDict
      - current_step     : "review_completed" | "query_completed" |
                           "pending_fix_review" | "action_ready" | "error"
      - assistant_response: 사용자에게 보여줄 최종 텍스트

    [자동 처리]
      - 메모리(summary_text, recent_chat) 저장은 후속 memory_summary_node 담당
    """
    async with SessionLocal() as db:
        try:
            _sid = state.get("session_id") or (state.get("session_meta") or {}).get("session_id")
            logging.info("[ArchGraph] domain_node(arch_review) ENTER session=%s", _sid)
            out = await _run_arch(state, db)
            logging.info(
                "[ArchGraph] domain_node(arch_review) EXIT session=%s step=%s",
                _sid,
                out.get("current_step"),
            )
            return out
        except Exception as exc:
            logging.error("[ArchNode] 처리 중 오류: %s", exc, exc_info=True)
            error_msg = f"건축 에이전트 처리 중 오류가 발생했습니다: {exc}"
            error_result: ReviewResult = {
                "is_violation":    False,
                "violations":      [],
                "suggestions":     [],
                "referenced_laws": [],
                "final_message":   error_msg,
            }
            return {
                **state,
                "review_result":      error_result,
                "current_step":       "error",
                "assistant_response": error_msg,
            }


# ── 내부 로직 ────────────────────────────────────────────────────────────────

async def _classify_intent(user_message: str) -> str:
    """
    사용자 메시지를 LLM으로 분류하여 3대 처리 갈래 중 하나를 반환합니다.

    Returns
    -------
    str
        "answer" : 일반 질문, 건축법 조회, 인사 등 — call_query_agent 또는 직답
        "review" : 도면 전체 위반 검토 — call_review_agent
        "action" : 특정 객체 수정/이동/삭제 — call_action_agent

    Notes
    -----
    - LLM 오류 또는 파싱 실패 시 "answer"로 fallback (최소한의 응답 보장).
    - intent_hint="review"이면 이 함수를 호출하지 않고 즉시 "review"로 고정.
      (agent/start 경로에서 LLM 분류 비용을 아끼기 위한 빠른 경로)
    - "answer"로 분류되어도 has_drawing=True + 건축 키워드 포함 시
      _run_arch()에서 "review"로 보정. (LLM 오분류 방어)
    """
    system_prompt = """당신은 건축 설계 AI 라우터입니다. 사용자의 요청을 다음 3가지 중 하나로 분류하세요:
- answer: 일반적인 질문, 인사, 건축법 조회, 법규 검색 등 (도면 객체 수정/검토가 아닌 경우)
- review: 도면 전체에 대한 건축법 위반 검토, 방화구획·복도폭·계단·피난거리 전수 조사
- action: 특정 객체에 대한 수정, 변경, 이동, 삭제 등 직접적인 액션 지시

응답은 반드시 JSON 형식으로만 하세요: {"intent": "answer" | "review" | "action"}"""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]
    try:
        res = await llm_service.generate_answer(messages=messages, response_format={"type": "json_object"})
        if isinstance(res, dict):
            return res.get("intent", "answer")
        return "answer"
    except Exception:
        return "answer"


async def _run_arch(state: AgentState, db: AsyncSession) -> AgentState:
    """
    건축 도메인 에이전트 핵심 실행 로직 (arch_review_node의 내부 진입점).

    처리 단계:
        1. 의도 분석  : intent_hint=review이면 즉시 고정, 아니면 _classify_intent() LLM 호출.
                        has_drawing+건축 키워드 조합으로 LLM 오분류 보정.
        2. 컨텍스트   : drawing_data를 레이어 분리 없이 그대로 context["raw_layout_data"]에 전달.
                        (건축은 arch 레이어가 1차 검토 대상 — pipe처럼 arch/non-arch 분리 불필요)
        3. 매핑       : answer 의도가 아닐 때만 이름 매핑(MappingAgent) + 위치 매핑(run_object_mapping)
                        을 asyncio.gather로 병렬 실행.
        4. 빠른 경로  : intent==answer이고 _is_casual_message() 판정이면 RAG 없이 LLM 직답.
        5. LLM tool   : ARCH_SUB_AGENT_TOOLS 스키마로 tool_choice="auto" 호출.
                        tool 미선택 시 _make_fallback_call()로 intent 기반 기본 tool 선택.
        6. Tool 실행  : ArchWorkflowHandler.handle_tool_calls() — query/review/action 세 갈래.
        7. 결과 변환  : _format_state()로 AgentState 필수 키(review_result 등) 채워 반환.

    Parameters
    ----------
    state : AgentState
        LangGraph 공유 상태. user_request, drawing_data, intent_hint, spec_guid 등 포함.
    db : AsyncSession
        비동기 DB 세션. QueryAgent·WorkflowHandler에 전달.

    Returns
    -------
    AgentState
        review_result, current_step, assistant_response, pending_fixes 등이 채워진 상태.
    """
    import time as _time
    t0 = _time.time()
    def _lap(label: str, since: float) -> float:
        now = _time.time()
        print(f"[ArchNode TRACK]  {label:<30} {now - since:5.1f}s  (누적 {now - t0:5.1f}s)")
        return now

    message     = state.get("user_request") or ""
    memory_text = build_memory_prompt_from_state(state)

    drawing_data = state.get("drawing_data") or {}
    has_drawing  = bool(drawing_data.get("entities") or drawing_data.get("elements"))
    logging.info(
        "[ArchGraph] domain_node DWG entities=%s",
        len(drawing_data.get("entities") or drawing_data.get("elements") or []),
    )

    current_drawing_id = state.get("current_drawing_id") or ""
    org_id    = state.get("org_id")
    rm        = state.get("runtime_meta") or {}
    se        = state.get("session_extra") or {}
    spec_guid = state.get("spec_guid") or rm.get("spec_guid") or se.get("spec_guid")
    active_ids       = set(state.get("active_object_ids") or [])
    pending_fixes_in = state.get("pending_fixes") or []

    # ── 1. 의도 분석 ──────────────────────────────────────────────────────────
    hint = (str(state.get("intent_hint") or "")).strip()
    if hint == "review":
        intent = "review"
        print("[ArchNode ROUTE] intent_hint=review → 도면검토(/agent/start) 경로 고정")
    else:
        intent = await _classify_intent(message)
        # LLM이 answer로 오분류할 때 건축 키워드로 보정
        if has_drawing and intent == "answer" and message and (
            "전수 검토" in message
            or "전수검토" in message
            or "위반" in message
            or "방화구획" in message
            or ("계단" in message and "검토" in message)
            or ("복도" in message and "검토" in message)
            or ("피난" in message and "검토" in message)
            or ("도면" in message and "검토" in message)
        ):
            intent = "review"
            print(f"[ArchNode ROUTE] 키워드 보정 → review (LLM was answer)")
    print(f"[ArchNode ROUTE] 의도 분류 결과: {intent}")
    lap_t = _lap("1. 의도 분석", t0)

    # ── 2. 컨텍스트 준비 ──────────────────────────────────────────────────────
    # 건축은 arch 레이어가 1차 검토 대상 — 레이어 분리 없이 전체 도면 전달
    context: dict[str, Any] = {
        "org_id":             org_id,
        "spec_guid":          spec_guid,
        "active_object_ids":  active_ids,
        "retrieved_laws":     list(state.get("retrieved_laws") or []),
        "current_drawing_id": current_drawing_id,
        "drawing_loaded":     has_drawing,
        "pending_fixes":      pending_fixes_in,
        "intent":             intent,
        "user_request":       message,
        "drawing_data":       drawing_data,
        "raw_layout_data":    drawing_data,   # arch는 레이어 분리 불필요 — 전체 전달
        "mapping_table":      {},
        "object_mapping":     [],
        "is_mapped":          False,
        "layer_role_stats":   {},
    }

    # ── 3. 매핑 (answer면 생략) — 이름 매핑과 위치 매핑 병렬 실행 ──────────────
    if has_drawing and intent != "answer":
        import asyncio as _asyncio

        # [최적화] execute_async: rule 매핑 → LLM 배치 병렬 폴백 (미분류 항목만)
        # get_instance로 인스턴스 캐싱 + execute_async로 직접 await (thread wrapping 불필요)
        _mapper = MappingAgent.get_instance(org_id=org_id)

        mapping_result, obj_mappings = await _asyncio.gather(
            _mapper.execute_async(drawing_data),
            run_object_mapping(
                drawing_data,
                domain_hint="건축",
                log_prefix="[ArchNode]",
            ),
        )

        context["mapping_table"]  = mapping_result
        context["is_mapped"]      = True
        context["object_mapping"] = obj_mappings
        lap_t = _lap("3. 이름/위치 매핑 병렬", lap_t)

        auto_cnt = sum(1 for m in obj_mappings if m.get("method") == "auto")
        llm_cnt  = sum(1 for m in obj_mappings if m.get("method") == "llm_fallback")
        print(f"[ArchNode MAP]  객체 매핑 결과: 총={len(obj_mappings)}쌍 (자동={auto_cnt}, LLM={llm_cnt})")

    # ── 4. 일반 인사/단순 대화 빠른 경로 ─────────────────────────────────────
    if intent == "answer" and _is_casual_message(message):
        logging.info("[ArchNode] casual message detected, direct LLM answer (no RAG)")
        direct = await llm_service.generate_answer(
            messages=[
                {"role": "system", "content": "당신은 친절한 건축법 전문 AI 어시스턴트입니다. 짧고 자연스럽게 대화하세요."},
                {"role": "user", "content": message},
            ],
        )
        if isinstance(direct, str) and direct.strip():
            return await _format_state(state, [{"agent": "direct", "result": direct.strip()}])

    # ── 5. LLM tool 선택 ──────────────────────────────────────────────────────
    system_prompt = _build_system_prompt(context, memory_text)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": message},
    ]
    tool_calls = await llm_service.generate_answer(
        messages=messages,
        tools=ARCH_SUB_AGENT_TOOLS,
        tool_choice="auto",
    )
    lap_t = _lap("5. LLM 도구 선택", lap_t)

    # LLM이 도구 없이 텍스트로 직접 응답한 경우
    if isinstance(tool_calls, str) and tool_calls.strip():
        logging.info("[ArchNode] direct text answer (no tool) intent=%s chars=%s", intent, len(tool_calls.strip()))
        return await _format_state(state, [{"agent": "direct", "result": tool_calls.strip()}])

    if not isinstance(tool_calls, list) or not tool_calls:
        logging.warning("[ArchNode] LLM tool 미선택, fallback 적용 (drawing=%s intent=%s)", has_drawing, intent)
        tool_calls = [_make_fallback_call(message, has_drawing, intent)]

    tool_names = [c["function"]["name"] for c in tool_calls]
    print(f"[ArchNode TRACK]  선택된 tool: {tool_names}")

    # ── 6. Tool 실행 (WorkflowHandler) ────────────────────────────────────────
    workflow         = ArchWorkflowHandler(session=context, db=db)
    workflow_results = await workflow.handle_tool_calls(tool_calls, context)
    lap_t = _lap(f"6. Tool 실행 ({','.join(tool_names)})", lap_t)

    # ── 7. 결과 → AgentState 변환 후 반환 ─────────────────────────────────────
    result = await _format_state(state, workflow_results)
    print(f"[ArchNode TRACK] ■ arch_review_node 총 {_time.time() - t0:.1f}s")
    return result


# ── 시스템 프롬프트 ──────────────────────────────────────────────────────────

def _build_system_prompt(context: dict[str, Any], memory_text: str) -> str:
    """
    LLM tool 선택 단계에 사용할 시스템 프롬프트를 구성합니다.

    context에서 도면 상태(로드 여부, 매핑 완료 여부, 수정 대기 건수), 의도(intent),
    도면 ID, 매핑 테이블 샘플, pending_fixes 미리보기를 조합합니다.
    intent='answer'이면 call_review_agent 호출을 억제하는 문구를 포함합니다.
    intent='review'/'action'이면 해당 tool 사용을 권장합니다.
    """
    drawing_loaded = context.get("drawing_loaded", False)
    is_mapped      = context.get("is_mapped", False)
    pending        = context.get("pending_fixes") or []
    drawing_id     = context.get("current_drawing_id") or ""
    term_map       = (context.get("mapping_table") or {}).get("term_map", {})

    drawing_status = "도면 로드됨" if drawing_loaded else "도면 없음"
    mapping_status = "매핑 완료"   if is_mapped      else "매핑 미완료"
    pending_status = f"수정 대기 {len(pending)}건" if pending else "수정 대기 없음"

    equipment_hint = ""
    if drawing_loaded and term_map:
        sample = list(term_map.items())[:10]
        lines  = "\n".join(f"  - {k}: {v}" for k, v in sample)
        more   = f"\n  ... 외 {len(term_map) - 10}건" if len(term_map) > 10 else ""
        equipment_hint = f"\n\n[도면 구조 목록 (매핑 후, 최대 10건)]\n{lines}{more}"

    pending_hint = ""
    if pending:
        lines = "\n".join(
            f"  - {f.get('handle', f.get('equipment_id', '?'))}: "
            f"{f.get('violation_type', '?')} / {f.get('action', '?')}"
            for f in pending[:5]
        )
        more  = f"\n  ... 외 {len(pending) - 5}건" if len(pending) > 5 else ""
        pending_hint = f"\n\n[수정 대기 항목]\n{lines}{more}"

    drawing_id_hint = f"\n도면 ID: {drawing_id}" if drawing_id else ""

    intent = context.get("intent") or "answer"
    intent_line = (
        "사용자 의도(라우터): 일반 Q&A/인사/건축법 조회 — "
        "인사나 일상 대화에는 도구 없이 직접 짧은 텍스트로만 답하세요. "
        "건축법·기준 조항 등 기술 질문이면 call_query_agent를 쓰세요. "
        "call_review_agent(도면 검토)는 '도면 검토/위반/전수'를 명시할 때만 쓰세요."
        if intent == "answer"
        else
        f"사용자 의도(라우터): {intent} — review면 건축법 위반 전수 분석, action이면 call_action_agent로 선택 객체 수정 검토."
    )

    return (
        f"당신은 20년 경력의 건축법 전문 AI 에이전트입니다.\n"
        f"{intent_line}\n"
        f"현재 상태: {drawing_status} | {mapping_status} | {pending_status}"
        f"{drawing_id_hint}"
        f"{equipment_hint}"
        f"{pending_hint}"
        f"\n\n[대화 메모리]\n{memory_text}"
        f"\n\n[도구 선택 기준]\n"
        f"- call_query_agent  : 건축법 시행령·기준 조항 조회 요청\n"
        f"- call_review_agent : 도면 전체 건축법 위반 검토. focus_area 생략 시 전체 검토.\n"
        f"- call_action_agent : 수정 지시 실행. pending_fixes 목록 기반으로 C# DrawingPatcher 명령 생성.\n\n"
        f"[답변 형식] 도구 없이 직접 답할 때는 마크다운(### 소제목, - 글머리, **강조**)으로 정리하세요.\n\n"
        f"반드시 하나의 도구를 선택하세요."
    )


# ── AgentState 변환 ──────────────────────────────────────────────────────────

async def _format_state(state: AgentState, workflow_results: list) -> AgentState:
    """
    WorkflowHandler 결과를 AgentState 필수 키로 변환합니다.

    ArchAgent의 정적 헬퍼(_violations_from_report_items, _build_pending_fixes)를 재사용하므로
    handle → object_id 매핑과 equipment_id 양방 정합이 보장됩니다.
    """
    violations:       list[ViolationItem]    = []
    suggestions:      list[str]              = []
    pending_fixes:    list[PendingFix]       = []
    referenced_laws:  list[str]              = []
    retrieved_laws:   list[LawReference]     = list(state.get("retrieved_laws") or [])
    final_message                            = ""
    current_step:     CurrentStep            = "agent_completed"
    response_meta:    dict[str, Any]         = {}

    for block in (workflow_results if isinstance(workflow_results, list) else []):
        agent  = block.get("agent")
        result = block.get("result")

        # ── query 결과 ─────────────────────────────────────────────────────────
        if agent == "query" and isinstance(result, list):
            ch_list = [r for r in result if isinstance(r, dict) and (r.get("content") or "").strip()]
            if ch_list:
                context_text = "\n\n---\n\n".join(r["content"] for r in ch_list[:5])
                prompt = (
                    f"다음은 검색된 건축법 및 규정 내용입니다. 이 정보를 바탕으로 사용자의 질문에 자연스럽고 친절하게 요약된 답변을 작성해주세요.\n"
                    f"답변은 Markdown 형식을 사용하여 보기 좋게 정리해주세요.\n\n"
                    f"[검색 결과]\n{context_text}\n\n"
                    f"사용자 질문: {state.get('user_request')}"
                )
                from backend.services import llm_service as _llm
                summary = await _llm.generate_answer([{"role": "user", "content": prompt}])
                final_message = summary if isinstance(summary, str) else context_text
            else:
                final_message = "관련 건축법 조항을 찾지 못했습니다."

            current_step = "query_completed"
            refs = [
                f"«{r.get('doc_name') or '건축법'}»#{r.get('chunk_index', '-')}"
                for r in result[:3]
                if isinstance(r, dict)
            ]
            if refs:
                final_message += f"\n\n---\n[출처] 시방RAG {' · '.join(refs)} (총{len(result)}건)."
            retrieved_laws = _to_law_references(result)
            response_meta = {"answer_type": "rag_query", "used_rag": bool(result)}

        # ── review 결과 ────────────────────────────────────────────────────────
        elif agent == "review" and isinstance(result, dict):
            report   = result.get("report") or {}
            fixes    = result.get("fixes") or []
            rag_refs = result.get("rag_references") or []

            # ArchAgent 정적 헬퍼 재사용:
            #   _violations_from_report_items: item.get("handle") → object_id (RevCloud 위치 결정)
            #   _build_pending_fixes: handle + equipment_id 양쪽 채움 (C#/API 호환)
            violations    = ArchAgent._violations_from_report_items(report.get("items") or [])
            pending_fixes = ArchAgent._build_pending_fixes(fixes)
            suggestions   = [f["description"] for f in pending_fixes if f.get("description")]
            referenced_laws = list({
                v.get("legal_reference", "")
                for v in violations
                if v.get("legal_reference")
            })

            total         = report.get("total_violations", len(violations))
            final_message = (
                f"건축 검토 완료: 위반 {total}건. "
                "수정 항목을 확인하고 적용할 항목을 선택하세요."
            )
            if rag_refs:
                rag_cite = " · ".join(
                    f"«{r.get('doc_name', '건축법')}»#{r.get('chunk_index', '-')}"
                    for r in rag_refs[:3]
                    if isinstance(r, dict)
                )
                final_message += f"\n\n---\n[출처] 검토 참고 {rag_cite} (총{len(rag_refs)}건)"

            current_step  = "pending_fix_review"
            response_meta = {"answer_type": "review", "used_rag": bool(rag_refs)}

        # ── action 결과 ────────────────────────────────────────────────────────
        elif agent == "action":
            final_message = (
                json.dumps(result, ensure_ascii=False)
                if not isinstance(result, str)
                else result
            )
            final_message += "\n\n---\n[출처] 액션 분석(DrawingPatcher 명령 생성)"
            current_step  = "action_ready"
            response_meta = {"answer_type": "action_command", "used_rag": False}

        # ── 직접 텍스트 응답 (일반 대화) ───────────────────────────────────────
        elif agent == "direct" and isinstance(result, str):
            final_message = result + "\n\n---\n[출처] 벡터DB 미검색·LLM 직답."
            current_step  = "agent_completed"
            response_meta = {"answer_type": "llm_direct", "used_rag": False}

    if not final_message:
        final_message = "처리가 완료되었습니다."

    # pending_fixes만 있고 violations가 비는 경우 정합
    if (not violations) and pending_fixes:
        violations = _violations_from_pending_fixes(pending_fixes)

    review_result: ReviewResult = {
        "is_violation":    len(violations) > 0,
        "violations":      violations,
        "suggestions":     suggestions,
        "referenced_laws": referenced_laws,
        "final_message":   final_message,
    }

    return {
        **state,
        "review_result":      review_result,
        "current_step":       current_step,
        "assistant_response": final_message,
        "retrieved_laws":     retrieved_laws,
        "pending_fixes":      pending_fixes,
        "response_meta":      response_meta,
    }


# ── 변환 헬퍼 ────────────────────────────────────────────────────────────────

def _to_law_references(query_result: list[dict]) -> list[LawReference]:
    """
    QueryAgent 결과(list[dict])를 AgentState.retrieved_laws 형식(list[LawReference])으로 변환합니다.

    LawReference는 chunk_id, document_id, legal_reference, snippet, score, source_type 필드를 가집니다.
    document_chunk_id는 PK가 int인 영구 시방서 청크에만 추가됩니다.
    """
    refs: list[LawReference] = []
    for r in query_result:
        rid = r.get("id")
        entry: LawReference = {
            "chunk_id":        str(rid) if rid is not None else str(r.get("section_id") or r.get("chunk_index") or ""),
            "document_id":     str(r.get("document_id") or ""),
            "legal_reference": str(r.get("section_id") or r.get("doc_name") or ""),
            "snippet":         str(r.get("content") or ""),
            "score":           float(r.get("score") or 0.0),
            "source_type":     str(r.get("source") or "permanent"),
        }
        if isinstance(rid, int):
            entry["document_chunk_id"] = rid
        refs.append(entry)
    return refs


def _violations_from_pending_fixes(pending: list) -> list[ViolationItem]:
    """pending_fixes만 있고 violations가 비는 경우 UI/CAD 정합용 합성."""
    out: list[ViolationItem] = []
    for f in pending or []:
        if not isinstance(f, dict):
            continue
        handle = str(f.get("handle") or f.get("equipment_id") or "")
        vtype  = str(f.get("violation_type") or "")
        desc   = str(f.get("description") or "")
        out.append({
            "object_id":       handle,
            "violation_type":  vtype,
            "reason":          desc or (f"{vtype} (수정 대기)" if vtype else "수정 대기 항목"),
            "legal_reference": "",
            "suggestion":      desc,
            "current_value":   "",
            "required_value":  "",
        })
    return out


_CASUAL_RE = re.compile(
    r"^[\s!?.]*"
    r"(안녕|하이|hi|hello|반가워|반갑|고마워|감사|ㅎㅇ|ㅋ+|ㅠ+|ㅎ+|헬로|잘있어|bye|바이|"
    r"괜찮아|잘부탁|잘 부탁|맞아|그래|응|네|아니|알겠|좋아|오케이|ok|okay|"
    r"수고|수고해|잠깐|잠시만|이봐|여보세요|뭐야|뭐임|뭐에요|어|오|아)"
    r"[\s!?.]*$",
    re.IGNORECASE,
)


def _is_casual_message(message: str) -> bool:
    """
    메시지가 인사·단순 감탄사 등 일상 대화인지 판별합니다.

    _CASUAL_RE 정규식과 30자 길이 제한을 모두 만족해야 True를 반환합니다.
    True이면 RAG 검색 없이 LLM 직답 경로로 라우팅되어 불필요한 DB 조회를 차단합니다.
    """
    return bool(_CASUAL_RE.match((message or "").strip())) and len((message or "").strip()) < 30


def _make_fallback_call(message: str, has_drawing: bool = False, intent: str = "answer") -> dict:
    """
    LLM이 tool을 선택하지 않을 경우 intent 기반으로 기본 tool 호출을 생성합니다.

    우선순위:
        intent="answer"  → call_query_agent (법규 조회 fallback)
        intent="review" + has_drawing → call_review_agent (도면 전체 검토)
        intent="action" + has_drawing → call_action_agent (수정 명령 생성)
        그 외             → call_query_agent (최소 응답 보장)
    """
    if intent == "answer":
        return {
            "function": {
                "name": "call_query_agent",
                "arguments": json.dumps({"query": message or "건축법 조항 조회"}, ensure_ascii=False),
            }
        }
    if has_drawing and intent == "review":
        return {
            "function": {
                "name": "call_review_agent",
                "arguments": json.dumps({"focus_area": ""}, ensure_ascii=False),
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
            "arguments": json.dumps({"query": message or "건축법 검토"}, ensure_ascii=False),
        }
    }
