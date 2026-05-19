"""
File    : backend/services/graph/nodes/elec_review_node.py
Author  : 송주엽, 김지우
Create  : 2026-04-15
Description : 전기 도메인 LangGraph 노드.
              AgentState를 입력받아 매핑 → tool 선택 → 서브 에이전트 실행 후
              review_result / current_step / assistant_response 를 채워 반환합니다.
              메모리 저장은 후속 memory_summary_node 가 자동 처리합니다.

Modification History :
    - 2026-04-15 (송주엽) : LangGraph 아키텍처 전환 — ElectricAgent 클래스 → 노드 함수
                            AgentState 기반 입출력, build_memory_prompt_from_state 연동,
                            QueryAgent 결과 → LawReference 변환 추가
    - 2026-04-19 (김지우) : cad_info 툴 실행 결과 데이터 포맷팅 보강
    - 2026-04-23 : pending_fixes 는 있는데 violations 가 비는 경우(경로 불일치) → 합성 ViolationItem 으로 정합
    - 2026-04-28 : [환각 방지] 도메인 필터링 및 유령 텍스트 제거 (GIGO 원천 차단), 디버그 로깅 강화
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid

# ── [긴급] Langfuse 에러로 인한 서버 종료 방지 (Tracing 비활성화) ──
os.environ["LANGFUSE_PUBLIC_KEY"] = ""
os.environ["LANGFUSE_SECRET_KEY"] = ""
os.environ["LANGFUSE_HOST"] = ""
# ──────────────────────────────────────────────────────────────────
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from backend.core.database import SessionLocal
from backend.services import llm_service
from backend.services.agents.common.object_mapping_utils import (
    run_object_mapping,
    ELEC_LAYER_BONUS,
)
from backend.services.agents.elec.schemas import ELEC_SUB_AGENT_TOOLS
from backend.services.agents.elec.sub.mapping import MappingAgent
from backend.services.payload_service import should_preserve_full_entities

from backend.services.agents.elec.entity_role_classifier import (
    classify_all_entities,
    build_scope_log,
    ELECTRIC_CORE,
    ELECTRIC_CONTEXT,
    ARCH_REFERENCE,
    DRAWING_FORM,
    NOISE,
)
from backend.services.agents.elec.workflow_handler import ElecWorkflowHandler
from backend.services.graph.prompt_utils import build_memory_prompt_from_state
from backend.services.cad_progress import emit_pipeline_step
from backend.services.payload_service import CONTEXT_MODE_FULL_WITH_FOCUS
from backend.services.graph.state import (
    AgentState,
    CurrentStep,
    LawReference,
    PendingFix,
    ReviewResult,
    ViolationItem,
)

logger = logging.getLogger(__name__)

# ── 표 참조 문구 감지 (인접 table chunk 자동 보완 트리거) ────────────────────────
_HAS_TABLE_REF_RE = re.compile(
    r"(?:표|별표|부표|다음\s*표|이\s*표|[Tt]able)\s*[\d\-\.가-힣]*"
    r"\s*(?:에\s*따른다|참조|참고|에\s*의함|에\s*의거|와\s*같다|을\s*적용|기준|에\s*준한다|에\s*나타낸|에\s*나타낸\s*바)",
    re.IGNORECASE,
)

# ── 질문에 표 내용을 요구하는 키워드 ─────────────────────────────────────────────
_TABLE_CONTENT_KEYWORDS = frozenset(
    "표 색상표 허용전류표 규격표 기준표 선정표 이격거리표 KS IEC L1 L2 L3 "
    "N선 PE 보호도체 중성선 갈색 흑색 회색 청색 녹황 허용전류 도체단면적 전선굵기 "
    "허용전압강하 전압강하 차단용량 정격전류 접지저항 이격거리".split()
)


# ── LangGraph 노드 진입점 ─────────────────────────────────────────────────────

async def elec_review_node(state: AgentState) -> AgentState:
    """
    전기 도메인 LangGraph 노드.

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
            logging.info("[ElecGraph] domain_node(electric_review) ENTER session=%s", _sid)
            out = await _run_electric(state, db)
            logging.info(
                "[ElecGraph] domain_node(electric_review) EXIT session=%s step=%s",
                _sid,
                out.get("current_step"),
            )
            return out
        except Exception as exc:
            logging.error("[ElectricNode] 처리 중 오류: %s", exc, exc_info=True)
            error_msg = f"전기 에이전트 처리 중 오류가 발생했습니다: {exc}"
            error_result: ReviewResult = {
                "is_violation":   False,
                "violations":     [],
                "suggestions":    [],
                "referenced_laws":[],
                "final_message":  error_msg,
            }
            return {
                **state,
                "review_result":      error_result,
                "current_step":       "error",
                "assistant_response": error_msg,
            }


# ── 내부 로직 ────────────────────────────────────────────────────────────────

async def _classify_intent(user_message: str) -> str:
    """사용자 메시지를 분석하여 4대 갈래(answer, review, fix_suggestion, action)로 분류합니다.
    1단계: 명확한 키워드 → 즉시 반환 (LLM 호출 없음)
    2단계: 모호한 경우만 LLM 분류
    """
    msg = user_message.strip().lower()

    # ── 1단계: 키워드 기반 사전 분류 (LLM 불필요) ─────────────────────────
    # action 키워드 (그리기·생성·삭제·이동 등 직접 실행 명령)
    _ACTION_KEYWORDS = (
        "삭제", "지워", "이동", "옮겨", "수정해", "바꿔", "교체", "변경해줘",
        "그려", "그려줘", "추가해", "추가해줘", "만들어", "만들어줘", "생성해줘",
        "추가하", "넣어줘", "그려줘", "삽입해",
        "delete", "move", "modify", "fix it", "apply", "draw", "create",
    )
    if any(k in msg for k in _ACTION_KEYWORDS):
        return "action"

    # review 키워드
    _REVIEW_KEYWORDS = (
        "전수 검토", "전수검토", "도면 검토", "위반 검토", "규정 검토",
        "전체 검토", "위반 사항", "위반사항", "검토해줘", "분석해줘",
        "review", "compliance check",
    )
    if any(k in msg for k in _REVIEW_KEYWORDS):
        return "review"

    # answer 키워드 (인사·간단 질문 — LLM 완전 패스)
    _ANSWER_KEYWORDS = (
        "안녕", "고마워", "감사", "잘 부탁", "응", "네", "알겠", "좋아",
        "뭐야", "어떻게", "알려줘", "뭔지", "설명해", "hello", "hi", "thanks",
    )
    if any(k in msg for k in _ANSWER_KEYWORDS):
        return "answer"

    # ── 2단계: 모호한 경우만 LLM 호출 ────────────────────────────────────
    system_prompt = """당신은 전기 설비 AI 라우터입니다. 사용자의 요청을 다음 4가지 중 하나로 분류하세요:
- answer: 일반적인 질문, 인사, 시방서 조회, 법규 검색 등 (도면 객체 수정/검토가 아닌 경우)
- review: 도면 전체에 대한 규정 위반 검토, 전수 조사 요청
- fix_suggestion: 특정 위반 항목에 대한 수정 방법 제안 요청 (승인 전 검토)
- action: 특정 객체에 대한 수정, 변경, 이동, 삭제, 그리기, 추가 등 즉각적인 실행 지시

응답은 반드시 JSON 형식으로만 하세요: {"intent": "answer" | "review" | "fix_suggestion" | "action"}"""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message}
    ]
    try:
        res = await llm_service.generate_answer(messages=messages, response_format={"type": "json_object"})
        if isinstance(res, dict):
            intent = res.get("intent", "answer")
            # 유효하지 않은 값 방어
            if intent not in ("answer", "review", "fix_suggestion", "action"):
                intent = "answer"
            return intent
        return "answer"
    except Exception:
        return "answer"


async def _run_electric(state: AgentState, db: AsyncSession) -> AgentState:
    import time as _time
    t0 = _time.time()
    t0m = _time.monotonic()
    w0 = _time.time()
    progress_last = t0m
    progress_session_id = str(
        state.get("session_id")
        or (state.get("session_meta") or {}).get("session_id")
        or ""
    )

    async def _progress(stage: str, message_: str) -> None:
        nonlocal progress_last
        progress_last = await emit_pipeline_step(
            session_id=progress_session_id or None,
            stage=stage,
            message=message_,
            t0_monotonic=t0m,
            wall_start_ts=w0,
            last_t=progress_last,
        )

    def _lap(label: str, since: float) -> float:
        now = _time.time()
        print(f"[ElectricNode TRACK]  {label:<30} {now - since:5.1f}s  (누적 {now - t0:5.1f}s)")
        return now

    await _progress("elec_review_enter", "전기 review 노드 진입 — 도면/대화 컨텍스트 확인")

    message = state.get("user_request") or ""
    history = state.get("recent_chat") or []
    memory_text = build_memory_prompt_from_state(state)

    drawing_data = state.get("drawing_data") or {}
    _fe = drawing_data.get("focus_extraction") or {}
    _n_full = len(drawing_data.get("entities") or drawing_data.get("elements") or [])
    _n_focus = len(_fe.get("entities") or _fe.get("elements") or [])
    logging.info(
        "[ElecGraph] domain_node DWG context_mode=%s full_entities=%s focus_entities=%s",
        drawing_data.get("context_mode"),
        _n_full,
        _n_focus,
    )
    has_drawing = bool(drawing_data.get("entities") or drawing_data.get("elements"))
    current_drawing_id = state.get("current_drawing_id") or ""
    org_id = state.get("org_id")
    rm = state.get("runtime_meta") or {}
    se = state.get("session_extra") or {}
    spec_guid = (
        state.get("spec_guid")
        or rm.get("spec_guid")
        or se.get("spec_guid")
    )
    active_ids = set(state.get("active_object_ids") or [])
    pending_fixes_in = state.get("pending_fixes") or []

    # ── 1. 의도 분석 (Router) ──────────────────────────────────────────
    hint = (str(state.get("intent_hint") or "")).strip()
    if hint == "review":
        intent = "review"
        print("[ElectricNode ROUTE] intent_hint=review → 도면검토(/agent/start) 경로 고정")
    else:
        intent = await _classify_intent(message)
        # 기본 user 메시지("전수/부분 검토") — LLM이 answer로만 주는 경우 보정
        if has_drawing and intent == "answer" and message and (
            "전수 검토" in message
            or "전수검토" in message
            or ("도면" in message and "위반" in message and "검토" in message)
            or ("선택" in message and "검토" in message and "객체" in message)
        ):
            intent = "review"
            print(f"[ElectricNode ROUTE] 키워드 보정 → review (LLM was answer)")
    print(f"[ElectricNode ROUTE] 의도 분류 결과: {intent} (매핑={'스킵(Fast Path)' if intent == 'action' else '실행'})")
    await _progress("elec_intent", f"의도 분석 완료: {intent}")
    lap_t = _lap("1. 의도 분석", t0)

    # ── 2. 갈래별 컨텍스트 준비 ───────────────────────────────────────
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
    }

    raw_layout = "{}"
    if has_drawing and intent != "answer":
        context_mode = drawing_data.get("context_mode")
        focus_entities = (drawing_data.get("focus_extraction") or {}).get("entities")

        if context_mode == CONTEXT_MODE_FULL_WITH_FOCUS and focus_entities:
            focus_drawing = dict(drawing_data)
            focus_drawing["entities"] = focus_entities
            raw_layout = json.dumps(focus_drawing, ensure_ascii=False)
        elif active_ids:
            sel_entities = [e for e in (drawing_data.get("entities") or []) if e.get("handle") in active_ids]
            if sel_entities:
                sel_drawing = dict(drawing_data)
                sel_drawing["entities"] = sel_entities
                raw_layout = json.dumps(sel_drawing, ensure_ascii=False)
            else:
                raw_layout = json.dumps(drawing_data, ensure_ascii=False)
        else:
            raw_layout = json.dumps(drawing_data, ensure_ascii=False)

    context["raw_layout_data"] = raw_layout
    context["drawing_data"] = drawing_data

    # ── 3. 매핑 로직 — review/fix_suggestion만 전체 매핑, action은 Fast Path 스킵 ────
    if has_drawing and intent in ("review", "fix_suggestion"):
        import asyncio as _asyncio

        _mapper = MappingAgent.get_instance(org_id=org_id)

        mapping_result, obj_mappings = await _asyncio.gather(
            _mapper.execute_async(drawing_data),
            run_object_mapping(
                drawing_data,
                domain_hint="전기",
                log_prefix="[ElectricNode]",
                layer_bonus_config=ELEC_LAYER_BONUS,
                max_distance_mm=50_000.0,  # 평면도는 텍스트-블록이 수 미터 떨어져 있음
                ambiguity_threshold=100.0,
            ),
        )

        context["mapping_table"]   = mapping_result
        context["style_map"]       = mapping_result.get("style_map", {})
        context["entity_type_map"] = mapping_result.get("entity_type_map", {})
        context["is_mapped"]       = True
        context["object_mapping"]  = obj_mappings

        # ── [디버그] 매핑 데이터 저장 (어떤 텍스트가 어떤 블록에 묶였는지 확인용) ──
        try:
            import os
            debug_map_dir = r"C:\Users\Playdata\Desktop\SKN23-FINAL-2TEAM\SKN23-FINAL\SKN23-FINAL-2TEAM\backend\services\agents\elec"
            os.makedirs(debug_map_dir, exist_ok=True)
            debug_map_file = os.path.join(debug_map_dir, "debug_mapping_result.json")
            with open(debug_map_file, "w", encoding="utf-8") as f:
                json.dump(obj_mappings, f, ensure_ascii=False, indent=2)
            print(f"[ElectricNode DEBUG] 매핑 데이터 저장 완료: {debug_map_file}")
        except Exception as e:
            print(f"[ElectricNode DEBUG] 매핑 데이터 저장 실패: {e}")
        # ────────────────────────────────────────────────────────────────────────
        
        # ── 도메인별 역할 5분류 (score 기반, CAD 원본 유지) ─────────────────────
        # drawing_data["entities"] 수정 없음
        # LLM raw_layout_data = CORE + ELECTRIC_CONTEXT (ARCH/FORM/NOISE 제외)
        domain_tags  = mapping_result.get("domain_tags", {})
        all_elements = drawing_data.get("entities", [])
        if obj_mappings and all_elements:
            label_by_handle: dict[str, list[str]] = {}
            for pair in obj_mappings:
                bh = str(pair.get("block_handle") or "")
                label = str(pair.get("label") or "").strip()
                if bh and label:
                    label_by_handle.setdefault(bh, []).append(label)
            for el in all_elements:
                h = str(el.get("handle") or el.get("id") or "")
                labels = label_by_handle.get(h)
                if labels and not el.get("name"):
                    el["name"] = " / ".join(labels[:3])

        scope_buckets, signal_counts = classify_all_entities(all_elements, domain_tags)

        n_core    = len(scope_buckets[ELECTRIC_CORE])
        n_ectx    = len(scope_buckets[ELECTRIC_CONTEXT])
        n_arch    = len(scope_buckets[ARCH_REFERENCE])
        n_form    = len(scope_buckets[DRAWING_FORM])
        n_noise   = len(scope_buckets[NOISE])

        # LLM에 전달: CORE + ELECTRIC_CONTEXT (ARCH는 공간 참조용으로 별도 보존)
        llm_drawing = dict(drawing_data)
        llm_drawing["entities"] = scope_buckets[ELECTRIC_CORE] + scope_buckets[ELECTRIC_CONTEXT]
        context["raw_layout_data"]       = json.dumps(llm_drawing, ensure_ascii=False)
        context["arch_reference_entities"] = scope_buckets[ARCH_REFERENCE]  # 거리/공간 판단용
        context["electric_scope_counts"] = {
            ELECTRIC_CORE:    n_core,
            ELECTRIC_CONTEXT: n_ectx,
            ARCH_REFERENCE:   n_arch,
            DRAWING_FORM:     n_form,
            NOISE:            n_noise,
        }

        print(build_scope_log(scope_buckets, signal_counts, len(all_elements)))
        logging.info(
            "[ElectricNode] scope split total=%d core=%d context=%d arch=%d form=%d noise=%d cad_deleted=0",
            len(all_elements), n_core, n_ectx, n_arch, n_form, n_noise,
        )
        await _progress(
            "elec_layout_split",
            f"도면 엔티티 역할 분류 완료 (전체 {len(all_elements)}개, 핵심 {n_core}개, 컨텍스트 {n_ectx}개)",
        )

        # ── [디버그] 역할 분류 결과 저장 ──────────────────────────────────────────
        try:
            debug_dir = r"C:\Users\Playdata\Desktop\SKN23-FINAL-2TEAM\SKN23-FINAL\SKN23-FINAL-2TEAM\backend\services\agents\elec"
            os.makedirs(debug_dir, exist_ok=True)
            debug_file = os.path.join(debug_dir, "debug_entities.json")
            debug_data = {
                "summary": {
                    "total_raw":        len(all_elements),
                    "electric_core":    n_core,
                    "electric_context": n_ectx,
                    "arch_reference":   n_arch,
                    "drawing_form":     n_form,
                    "noise_removed":    n_noise,
                    "cad_deleted":      0,
                    "timestamp_unix":   t0,
                },
                "signal_breakdown": signal_counts,
                "electric_core_entities":    scope_buckets[ELECTRIC_CORE],
                "electric_context_entities": scope_buckets[ELECTRIC_CONTEXT],
                "arch_reference_entities":   scope_buckets[ARCH_REFERENCE],
                "form_entities":             scope_buckets[DRAWING_FORM],
                "noise_entities":            scope_buckets[NOISE],
            }
            with open(debug_file, "w", encoding="utf-8") as f:
                json.dump(debug_data, f, ensure_ascii=False, indent=2)
            print(f"[ElectricNode DEBUG] 역할 분류 결과 저장: {debug_file}")
        except Exception as e:
            print(f"[ElectricNode DEBUG] 파일 저장 실패: {e}")
        # ────────────────────────────────────────────────────────────────────────

        lap_t = _lap("3+4b. 이름/위치 매핑 병렬", lap_t)

        auto_cnt = sum(1 for m in obj_mappings if m.get("method") == "auto")
        llm_cnt = sum(1 for m in obj_mappings if m.get("method") == "llm_fallback")
        print(f"[ElectricNode MAP]  객체 매핑 결과: 총={len(obj_mappings)}쌍 (자동={auto_cnt}, LLM={llm_cnt})")
        await _progress(
            "elec_mapping",
            f"전기 이름·위치 매핑 완료 (총 {len(obj_mappings)}쌍, 자동={auto_cnt}, LLM={llm_cnt})",
        )
        for pair in obj_mappings[:3]:
            print(f"[ElectricNode MAP]    {pair.get('label','?')}: text={pair.get('text_handle')} → block={pair.get('block_handle')} ({pair.get('method')}, score={pair.get('score',0):.1f})")

    elif has_drawing and intent == "action":
        # ── Fast Path: action 명령은 매핑 전체 스킵 (MappingAgent LLM 호출 없음) ──
        context["mapping_table"]   = {}
        context["style_map"]       = {}
        context["entity_type_map"] = {}
        context["is_mapped"]       = False
        context["object_mapping"]  = []
        print("[ElectricNode MAP]  action 의도 → 전체 매핑 스킵 (Fast Path, ~0s)")
        lap_t = _lap("3. 매핑 스킵(action Fast Path)", t0)

    # 5. LLM tool 선택 (메모리 포함 시스템 프롬프트)
    system_prompt = _build_system_prompt(context, memory_text)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": message},
    ]
    tool_calls = await llm_service.generate_answer(
        messages=messages,
        tools=ELEC_SUB_AGENT_TOOLS,
        tool_choice="auto",
    )
    lap_t = _lap("5. LLM 도구 선택", lap_t)

    if isinstance(tool_calls, str) and tool_calls.strip():
        logging.info(
            "[ElectricDebug] direct text answer (no tool) intent=%s chars=%s",
            intent,
            len(tool_calls.strip()),
        )
        # ── [RAG 강제 라우팅]
        # LLM이 tool_choice=auto 상황에서 법규/시방서 질문을 직접 답변해버리는 것을 방지합니다.
        # 인사·단순 응답만 direct로 허용하고, 나머지 answer 질의는 call_query_agent로 강제 라우팅합니다.
        if intent == "answer":
            _GREET_MSGS = (
                "안녕", "고마워", "감사", "반가", "수고", "잘 부탁",
                "hello", "hi", "thanks", "bye", "응", "네", "알겠",
            )
            _msg_l = message.strip().lower()
            _is_greeting = any(k in _msg_l for k in _GREET_MSGS) and len(_msg_l) <= 30
            if not _is_greeting:
                logging.info("[ElectricNode] answer 의도 직답 감지 → call_query_agent 강제 라우팅")
                print(f"[ElectricNode ROUTE] 직답 차단 → call_query_agent 강제 라우팅 (msg={message[:40]}...)")
                tool_calls = [_make_fallback_call(message, has_drawing, "answer", list(active_ids))]
            else:
                return await _format_state(state, [{"agent": "direct", "result": tool_calls.strip()}])
        else:
            return await _format_state(state, [{"agent": "direct", "result": tool_calls.strip()}])

    if not isinstance(tool_calls, list) or not tool_calls:
        logging.warning(
            "[ElectricNode] LLM tool 미선택, fallback 적용 (drawing=%s intent=%s)",
            has_drawing,
            intent,
        )
        tool_calls = [_make_fallback_call(message, has_drawing, intent, list(active_ids))]

    # [RAG 안정화] UI 표시 질의와 내부 검색 질의를 분리합니다.
    # - suggested_queries/query: 사용자가 보는 짧은 질의
    # - call_query_agent query: 내부 retrieval 전용 확장 질의(anchor 포함)
    for tc in tool_calls:
        if tc.get("function", {}).get("name") == "call_query_agent":
            args_str = tc["function"].get("arguments", "{}")
            try:
                args = json.loads(args_str)
                display_query = str(args.get("display_query") or args.get("query") or message or "").strip()
                retrieval_query = _expand_retrieval_query(display_query)
                args["display_query"] = display_query
                args["retrieval_query"] = retrieval_query
                # 기존 query_agent 호환성을 위해 실제 검색 query에는 확장 질의를 넣습니다.
                args["query"] = retrieval_query
                args["limit"] = 15 if _is_broad_rag_query(display_query) else int(args.get("limit") or 5)
                tc["function"]["arguments"] = json.dumps(args, ensure_ascii=False)
                print(f"[ElectricNode RAG] display_query={display_query[:80]} | retrieval_query={retrieval_query[:120]} | limit={args['limit']}")
            except Exception as exc:
                logging.debug("[ElectricNode] query anchor injection skipped: %s", exc)

    tool_names = [c["function"]["name"] for c in tool_calls]
    print(f"[ElectricNode TRACK]  선택된 tool: {tool_names}")
    await _progress("elec_tool_select", f"전기 도구 선택: {', '.join(tool_names)}")

    # 6. Tool 실행 (WorkflowHandler)
    context["session_id"] = progress_session_id
    context["_progress_t0m"] = t0m
    context["_progress_w0"] = w0
    workflow        = ElecWorkflowHandler(session=context, db=db)
    workflow_results= await workflow.handle_tool_calls(tool_calls, context)
    for _b in workflow_results or []:
        _an = _b.get("agent")
        logging.info(
            "[ElectricDebug] workflow block agent=%s (intent=%s has_drawing=%s)",
            _an,
            intent,
            has_drawing,
        )
    await _progress("elec_tool_run", f"전기 서브 에이전트 실행 완료 ({', '.join(tool_names)})")
    lap_t = _lap(f"6. Tool 실행 ({','.join(tool_names)})", lap_t)

    # 7a. [TABLE EXPAND] query 결과에 표 참조가 있으면 인접 table chunk 자동 보완
    if "call_query_agent" in tool_names:
        try:
            workflow_results = await _expand_elec_table_neighbors(workflow_results, db)
            lap_t = _lap("7a. Table neighbor expand", lap_t)
        except Exception as _exp_exc:
            logger.warning("[ElecNode] table neighbor expand 실패: %s", _exp_exc)

    # 7b. 결과 → AgentState 변환 후 반환
    result = await _format_state(state, workflow_results)
    print(f"[ElectricNode TRACK] ■ elec_review_node 총 {_time.time() - t0:.1f}s")
    return result


# ── 시스템 프롬프트 ──────────────────────────────────────────────────────────

def _build_system_prompt(context: dict[str, Any], memory_text: str) -> str:
    drawing_loaded = context.get("drawing_loaded", False)
    is_mapped      = context.get("is_mapped", False)
    pending        = context.get("pending_fixes") or []
    drawing_id     = context.get("current_drawing_id") or ""
    term_map       = (context.get("mapping_table") or {}).get("term_map", {})

    drawing_status = "도면 로드됨" if drawing_loaded else "도면 없음"
    mapping_status = "매핑 완료"   if is_mapped      else "매핑 미완료"
    pending_status = f"수정 대기 {len(pending)}건" if pending else "수정 대기 없음"

    # 도면 설비 목록 힌트
    equipment_hint = ""
    if drawing_loaded and term_map:
        sample = list(term_map.items())[:10]
        lines  = "\n".join(f"  - {k}: {v}" for k, v in sample)
        more   = f"\n  ... 외 {len(term_map) - 10}건" if len(term_map) > 10 else ""
        equipment_hint = f"\n\n[도면 설비 목록 (매핑 후, 최대 10건)]\n{lines}{more}"

    # 수정 대기 항목 힌트
    pending_hint = ""
    if pending:
        lines = "\n".join(
            f"  - {f.get('equipment_id', '?')}: "
            f"{f.get('violation_type', '?')} / {f.get('action', '?')}"
            for f in pending[:5]
        )
        more  = f"\n  ... 외 {len(pending) - 5}건" if len(pending) > 5 else ""
        pending_hint = f"\n\n[수정 대기 항목]\n{lines}{more}"

    drawing_id_hint = f"\n도면 ID: {drawing_id}" if drawing_id else ""

    # 도면 컨텍스트 및 포커스 정보
    dd = context.get("drawing_data") or {}
    focus_ctx = ""
    if dd.get("context_mode") == "full_with_focus": # 기존 상수 사용 시 해당 상수로 변경
        fe = dd.get("focus_extraction") or {}
        n = int(fe.get("entity_count") or 0)
        focus_ctx = (
            f"\n\n[도면 컨텍스트] JSON의 `entities`는 전체 도면, `focus_extraction`은 사용자 선택 구간 스냅샷({n}개 객체)입니다. "
            f"진단 시 active_object_ids와 focus_extraction을 우선 대조하고, 연결 관계는 전체 entities에서 확인하세요."
        )

    # 라우터 의도에 따른 지시문 + 도구 선택 기준
    intent = context.get("intent") or "answer"
    active_count = len(context.get("active_object_ids") or [])

    if intent == "review":
        intent_line = (
            "사용자 의도: 도면 검토(review). "
            "call_review_agent 하나만 호출하세요. "
            "call_action_agent는 이 경로에서 절대 호출하지 마세요."
        )
        tool_guide = (
            f"- call_query_agent : 법규·시방서 수치 조회가 선행 필요한 경우에만.\n"
            f"- call_review_agent : 도면 위반 전수 분석. ← 이 경로의 기본 도구.\n"
            f"  ※ target_id: 설비 ID 또는 'ALL'.\n"
            f"- call_action_agent : 사용 금지 (review 경로). 사용자가 수정을 명시적으로 요청할 때만 별도 호출됩니다.\n"
        )
    elif intent == "action":
        intent_line = (
            "사용자 의도: 도면 수정(action). "
            "call_action_agent 하나만 호출하세요. "
            "call_review_agent는 이 경로에서 호출하지 마세요."
        )
        tool_guide = (
            f"- call_action_agent : 수정/변경/교체/이동/추가 지시 실행. 선택된 객체({active_count}개) 즉시 처리.\n"
            f"- call_review_agent : 사용 금지 (action 경로).\n"
        )
    elif intent == "answer":
        intent_line = (
            "사용자 의도: 일반 질의 및 규정 조회. "
            "일상 대화는 직접 답하되, 법규·시방서·설계 기준 질문은 call_query_agent를 사용하세요."
        )
        tool_guide = (
            f"- call_query_agent : 진단에 활용 가능한 수치·강제 조항 추출.\n"
            f"- call_review_agent : 도면 전체 위반 분석 요청 시에만.\n"
            f"- call_action_agent : 사용자가 수정을 명시적으로 요청할 때만.\n"
        )
    else:
        intent_line = f"사용자 의도: {intent}."
        tool_guide = (
            f"- call_query_agent : 시방서 조회.\n"
            f"- call_review_agent : 도면 위반 분석.\n"
            f"- call_action_agent : 수정 지시 실행. 선택된 객체({active_count}개) 처리.\n"
        )

    return (
        f"당신은 60년 경력의 전기 전문 엔지니어 AI 에이전트입니다.\n"
        f"{intent_line}\n"
        f"현재 상태: {drawing_status} | {mapping_status} | {pending_status}"
        f"{drawing_id_hint}"
        f"{focus_ctx}"
        f"{equipment_hint}"
        f"{pending_hint}"
        f"\n\n[대화 메모리]\n{memory_text}"
        f"\n\n[도구 선택 기준]\n"
        f"반드시 올바른 JSON 형식을 유지하고 하나의 도구만 선택하세요.\n"
        f"{tool_guide}"
        f"\n[주의사항 - 환각 방지]\n"
        f"1. 도면 틀(Title Block)이나 범례의 일반 정보는 무시하되, '설계 주석(Note)'이나 '시공 지침'에 명시된 기술적 수치는 반드시 분석에 포함하세요.\n"
        f"2. 전선 속성이 없는 단순 선을 보고 '굵기 0'으로 판단하는 것은 환각입니다. 명확한 속성(Spec)이 부여된 객체만 검토하세요.\n"
        f"3. 모든 진단은 추측이 아닌 call_query_agent를 통해 확인된 실제 시방서/법규의 수치를 근거로 수행하세요.\n"
        f"4. 동일 회로 내 인접한 위반 사항들은 하나로 통합하여 요약 보고하세요.\n\n"
        f"[답변 형식]\n"
        f"도구 없이 직접 답할 때는 마크다운(### 소제목, - 목록, **강조**)을 사용하세요. "
        f"규정 조항 등은 나열하지 말고 소제목으로 구분하여 가독성을 높여 답변하세요.\n\n"
        f"반드시 하나의 도구를 선택하세요."
    )


# ── RAG/직접응답 출처 (사용자 답변·API 메타) ────────────────────────────────

_DOMAIN_PREFIX_RE = re.compile(
    r"^(?:elec|pipe|arch|electric|piping|architecture|mech|mechanical)_",
    re.IGNORECASE,
)
_DATE_SUFFIX_RE = re.compile(r"_\d{8}$")


def _pretty_doc_name(doc_name: str, category: str = "") -> str:
    """doc_name을 사용자 표시용으로 정제합니다.

    elec_KCS_322510 간선 및 배선설비공사_20240822
    → 시방서 KCS 322510 간선 및 배선설비공사
    """
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


def _chunk_to_meta_row(r: dict) -> dict[str, Any]:
    src = (r.get("source") or "permanent") or "permanent"
    raw_doc_name = str(r.get("doc_name") or "").strip() or "—"
    category = str(r.get("category") or "")
    return {
        "source": str(src),
        "source_label": "영구_시방_DB" if src == "permanent" else "임시_시방_DB",
        "doc_name": raw_doc_name,
        "display_name": _pretty_doc_name(raw_doc_name, category),
        "document_id": str(r.get("document_id") or ""),
        "chunk_index": r.get("chunk_index"),
        "domain": str(r.get("domain") or ""),
        "category": category,
    }


def _citation_line_from_rows(rows: list[dict], *, max_show: int = 3) -> str:
    if not rows: return ""
    show = rows[:max_show]
    parts: list[str] = []
    for m in show:
        name = (m.get("display_name") or m.get("doc_name") or "—")[:60]
        idx = m.get("chunk_index", "-")
        parts.append(f"«{name}»#{idx}")
    more = f" +{len(rows) - len(show)}" if len(rows) > len(show) else ""
    return " · ".join(parts) + more


def _retrieval_block_compact(meta_rows: list[dict]) -> dict[str, Any]:
    n = len(meta_rows)
    first = meta_rows[:4]
    return {
        "n_chunks": n,
        "summary": _citation_line_from_rows(meta_rows, max_show=3) + (f" (총{n}건)" if n else ""),
        "refs": [
            {
                "doc": (m.get("display_name") or m.get("doc_name", ""))[:80],
                "i": m.get("chunk_index"),
                "db": m.get("source_label", ""),
            }
            for m in first
        ],
    }


def _format_rag_footer(rows: list[dict], *, n_chunks: int) -> str:
    if n_chunks <= 0:
        return "\n\n---\n[출처] RAG: 일치 시방 청크 없음(요약/직답일 수 있음)."
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


def _lines_to_bullet_block(p: str) -> str:
    p = p.strip()
    if not p: return p
    lines = re.split(r"\r?\n", p)
    out: list[str] = []
    for line in lines:
        t = line.strip()
        if not t:
            out.append("")
            continue
        if t.startswith(("-", "*", "•")):
            t = t.replace("•", "-", 1) if t.startswith("•") else t
            if not t.startswith("-"):
                t = f"- {t[1:].strip()}" if t[:1] in "*•" else t
            out.append(t)
            continue
        m = re.match(r"^([가-힣])[\.\:]\s+(.+)$", t)
        if m and len(m.group(1)) == 1:
            out.append(f"- **{m.group(1)}.** {m.group(2).strip()}")
            continue
        m2 = re.match(r"^\((\d+)\)\s*(.+)$", t)
        if m2:
            out.append(f"- **({m2.group(1)})** {m2.group(2).strip()}")
            continue
        m3 = re.match(r"^(\d{1,2})[\).]\s+(.+)$", t)
        if m3:
            out.append(f"- **{m3.group(1)}.** {m3.group(2).strip()}")
            continue
        m4 = re.match(r"^-\s*\((\d+)\)\s*(.+)$", t)
        if m4:
            out.append(f"- **({m4.group(1)})** {m4.group(2).strip()}")
            continue
        m5 = re.match(r"^-\s+(\d+[\).])\s*(.+)$", t)
        if m5:
            out.append(f"- **{m5.group(1)}** {m5.group(2).strip()}")
            continue
        out.append(t)
    return "\n".join(out)


def _spec_text_to_readable_markdown(s: str) -> str:
    if not s or not s.strip(): return s
    s = s.strip()
    parts = re.split(r"(?:\r?\n)\s*---\s*(?:\r?\n)?|\n\s*---\s*\n|\s+---\s+|\n---\n", s)
    parts = [p.strip() for p in parts if p.strip()]
    if not parts: return s
    if len(parts) == 1: return _lines_to_bullet_block(parts[0])
    blocks: list[str] = [f"### {i} · 요약\n\n{_lines_to_bullet_block(p)}" for i, p in enumerate(parts, 1)]
    return "\n\n".join(blocks)



def _strip_html(s: str) -> str:
    """HTML 태그(<br>, <p>, <div> 등)를 제거하고 줄바꿈/공백을 정리합니다."""
    if not s:
        return s
    s = re.sub(r"<br\s*/?>|</?p>|</?div>|</?li>|</?tr>|</?td>|</?th>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", "", s)
    s = (
        s.replace("&nbsp;", " ")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
        .replace("&quot;", '"')
    )
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _detect_answer_style(user_request: str) -> str:
    """사용자 질문에 맞춰 RAG 답변 형식을 분기합니다."""
    q = (user_request or "").strip()
    q_lower = q.lower()
    if any(k in q for k in (
        "rule JSON", "rule json", "rule_json",
        "자동 검토 rule", "JSON으로 변환", "json으로 변환",
        "자동 판정 조건", "자동판정", "CAD 판정",
        "자동 검토 가능", "자동검토 가능", "자동 검토 rule 기준",
    )):
        return "auto_review_report"
    if any(k in q for k in ("도면", "검토", "위반", "판정", "자동검토", "표로", "표 형식", "테이블")) or "cad" in q_lower:
        return "review_table"
    if any(k in q for k in ("정리", "요약", "한눈에", "정돈", "레포트", "보고서", "리포트")):
        return "structured_summary"
    if any(k in q for k in ("색상", "생상", "상별", "이격거리", "거리", "수치", "얼마", "기준값", "규격")):
        return "focused_detail"
    if any(k in q for k in ("설명", "알려", "뭐야", "무엇", "대해")):
        return "simple_explain"
    return "structured_summary"


def _style_instruction(answer_style: str) -> str:
    if answer_style == "focused_detail":
        return (
            "[답변 형식 - focused_detail]\n"
            "표를 만들지 마세요. 사용자가 물은 특정 항목만 간결하게 정리하세요.\n"
            "반드시 다음 섹션을 빠짐없이 포함하세요:\n\n"
            "### 핵심 기준\n"
            "- 기준명/조항: 검색 결과에 있는 명칭과 조항만 작성\n"
            "- 적용 대상: 검색 결과에 있는 범위만 작성\n\n"
            "### 세부 내용\n"
            "- 필요한 값을 항목별 bullet로 작성\n"
            "- 색상/수치/거리 등은 한 줄씩 분리. 실제 수치/색상을 본문에 직접 명시하세요. '표 참조' 같은 회피성 문구 금지.\n\n"
            "### 적용 및 주의사항\n"
            "- 예외, 기존 전선 허용, 표시 방법 등 검색 결과에 있는 내용만 작성\n"
        )
    if answer_style == "structured_summary":
        return (
            "[답변 형식 - structured_summary]\n"
            "섹션과 bullet 중심의 리포트 형식으로 정리하세요.\n"
            "반드시 아래의 4개 섹션을 정확한 이름으로 빠짐없이 포함하세요:\n\n"
            "### 기준 요약\n"
            "- 기준명:\n"
            "- 적용범위:\n"
            "- 핵심 원칙:\n\n"
            "### 주요 기준\n"
            "- 검색 결과의 핵심 기준을 항목별로 상세 정리\n"
            "- 실제 색상/수치/기준값이 검색 결과에 있으면 반드시 답변 본문에 직접 포함하고, '표 참조'만 쓰지 마세요.\n\n"
            "### CAD 검토 가능성\n"
            "- 자동 검토 가능:\n"
            "- 부분 가능:\n"
            "- 도면만으로 판단 불가:\n\n"
            "### 적용 및 주의사항\n"
            "- 예외/주의사항/추가 확인 필요 사항\n"
        )
    if answer_style == "review_table":
        return (
            "[답변 형식 - cad_review]\n"
            "CAD 검토에 쓰기 위한 표 형식으로 작성하세요.\n"
            "반드시 아래의 4개 섹션을 정확한 이름으로 빠짐없이 포함하세요:\n\n"
            "### 설계·시공 기준\n"
            "- 요약된 원칙을 짧게 적고, 아래 표를 작성하세요.\n"
            "| 항목 | 적용 대상 | 조건 | 요구사항 | 정량 기준 | 예외/주의 |\n"
            "|---|---|---|---|---|---|\n"
            "| 검색 결과 기반 | ... | ... | 실제 값 명시 | 수치/정량값 또는 없음 | ... |\n\n"
            "### CAD 검토 포인트\n"
            "- CAD 판정을 위해 추출해야 할 속성(레이어, 블록, 텍스트, 색상 등)과 판정 로직을 명시하세요.\n\n"
            "### 자동 판정 가능 여부\n"
            "- 가능 / 부분 가능 / 불가 로 구분하여 항목별로 작성\n\n"
            "### 한계\n"
            "- 도면만으로는 파악할 수 없는 현장 시공적 한계나 정보 부족 사항을 반드시 적으세요.\n"
        )
    if answer_style == "auto_review_report":
        return (
            "[답변 형식 - auto_validation]\n"
            "설계자/CAD 검토자가 읽는 '자동 검토 가능 조건 리포트'를 작성하세요.\n"
            "JSON, dict, 배열, 내부 데이터 구조를 절대 출력하지 마세요.\n"
            "반드시 아래의 5개 섹션을 정확한 이름으로 빠짐없이 포함하세요:\n\n"
            "### 자동 검토 가능 항목\n"
            "- 검색 결과에서 CAD로 자동 확인 가능한 항목을 bullet로 나열\n\n"
            "### CAD 판정 방식\n"
            "- 각 항목을 어떤 CAD 속성(Color Index, RGB, Layer Name, Block Attribute, Text Annotation 등)으로 판정하는지 구체적으로 작성\n\n"
            "### 자동 판정 가능 여부\n"
            "| 항목 | 가능 여부 |\n"
            "|---|---|\n"
            "| 항목명 | 가능 / 부분 가능 / 불가 |\n\n"
            "### 필요 CAD 데이터\n"
            "- 판정에 필요한 CAD 객체 속성 목록\n\n"
            "### 한계\n"
            "- CAD 속성만으로 판단 불가한 사항을 반드시 작성\n"
        )
    return (
        "[답변 형식 - simple_explain]\n"
        "표를 만들지 마세요. 설명형으로 답하되, 반드시 아래의 4개 섹션을 정확한 이름으로 빠짐없이 포함하세요:\n\n"
        "### 개념 및 목적\n"
        "- 사용자가 물은 기준/개념이 무엇인지 먼저 정의하고, 왜 필요한지 설명하세요.\n"
        "- 전기설비 안전성, 유지보수, 시공 일관성과 연결해 설명하세요.\n\n"
        "### 주요 기준 및 적용\n"
        "- 검색 결과에 있는 기준만 bullet로 정리하세요.\n"
        "- 실제 색상/수치/기준값이 검색 결과에 있으면 답변에 직접 명시하세요.\n"
        "- 정상 markdown table이 검색 결과에 있으면 표를 유지해도 됩니다. 단, 핵심 row 값은 본문에서도 간단히 설명하세요.\n\n"
        "### CAD 검토 포인트\n"
        "- CAD에서 검토 가능한 속성(레이어, 색상, 텍스트, 블록 속성, 선종류 등)을 정리하세요.\n"
        "- 자동 판정 가능/부분 가능/도면만으로 불가한 항목을 구분하세요.\n\n"
        "### 적용 시 주의사항\n"
        "- 예외 또는 추가 확인 사항을 작성하세요.\n"
        "- 검색 결과에 없는 값은 추정하지 마세요.\n"
    )


def _build_rag_answer_prompt(*, user_request: str, context_text: str, answer_style: str) -> str:
    return (
        "당신은 전기설비 법규·시방서·설계기준을 근거 기반으로 정리하는 최고 수준의 엔지니어링 AI입니다.\n"
        "아래 [검색 결과]에 실제로 포함된 내용만 근거로 사용자의 질문에 답하되, 반드시 주어진 답변 형식과 섹션을 준수하세요.\n\n"
        "[절대 규칙 - Synthesis Consistency]\n"
        "1. 지정된 섹션 제목(###)은 하나라도 누락해서는 안 됩니다.\n"
        "2. 검색 결과에 실제 데이터가 있으면 '표 참조', '관련 규정을 확인하세요' 같은 회피성 문구 대신 본문에 직접 통합하세요.\n"
        "3. 검색 결과에 없는 수치, 예외, 조항, 기준명, 표 내용을 창작하지 마세요.\n"
        "4. 표/별표/조항 번호를 제시할 때는 검색 결과에 포함된 실제 기준값을 함께 제시하세요.\n"
        "5. 문서 표지, 목차, 작성기관, 연락처, 위원 명단은 질문과 무관하므로 제외하세요.\n"
        "6. '충분히', '적절히', '고려한다' 같은 추상 표현은 판정 기준처럼 쓰지 마세요.\n"
        "7. HTML 태그(<br>, <p>, <div>, <span> 등) 금지. 마크다운만 사용하세요.\n"
        "8. [표 추출 필수] 검색 결과에 정상 markdown table(|---|---|)이 있으면, '표를 참조하세요' 또는 '표에 따른다'만 쓰지 말고 "
        "표의 각 행에서 실제 값(항목명, 수치, 색상명 등)을 답변 본문에 직접 포함하세요. "
        "특히 L1/L2/L3/N/PE, 색상, 수치, 거리, 전류, 접지값 같은 행은 반드시 누락하지 마세요.\n"
        "9. [정상 표 유지] 정상 markdown table은 그대로 유지해도 됩니다. 단, 중요한 row 값은 표 아래 또는 위의 bullet에도 함께 설명하세요.\n"
        "10. [깨진 표 처리] 표 헤더가 '열1', '열2', '열3'이거나 셀 내용이 대부분 빈칸·기호·깨진 문자이면 "
        "해당 표를 렌더링하지 말고, 의미 있는 셀 값만 bullet로 나열하세요. 의미 있는 값이 없으면 표를 언급하지 마세요.\n"
        "11. '다음 표에 따른다', '표 N.N-M에 따른다' 표현만 있고 실제 표 내용이 검색 결과에 없으면 "
        "표 번호만 반복하지 마세요. 실제 표 내용이 없다고 간단히 밝히고 임의로 값을 생성하지 마세요.\n"
        "12. 검색 결과에 실제 표가 존재하는데 답변에 표 row 값이 하나도 포함되지 않으면 실패입니다.\n\n"
        f"{_style_instruction(answer_style)}\n"
        f"[검색 결과]\n{context_text}\n\n"
        f"사용자 질문: {user_request}"
    )


def _clean_followup_base(user_request: str) -> str:
    """후속 버튼 질의의 기준 주제를 원문에서 최대한 보존해서 추출합니다."""
    base = (user_request or "전기설비 기준").strip()
    remove_patterns = [
        r"\s*리포트\s*형식으로\s*정리\s*$",
        r"\s*보고서\s*형식으로\s*정리\s*$",
        r"\s*CAD\s*검토\s*기준\s*(중심으로)?\s*(설명|정리)?\s*$",
        r"\s*도면\s*검토\s*기준\s*(으로)?\s*(설명|정리)?\s*$",
        r"\s*자동\s*검토\s*rule\s*(기준으로)?\s*변환\s*$",
        r"\s*자동\s*판정\s*조건\s*(으로)?\s*변환\s*$",
        r"\s*자동\s*검토\s*가능\s*조건\s*(으로)?\s*정리\s*$",
        r"\s*rule\s*JSON\s*(으로)?\s*변환\s*$",
        r"\s*JSON\s*(으로)?\s*변환\s*$",
        r"\s*정리\s*$",
        r"\s*요약\s*$",
        r"\s*설명\s*$",
    ]
    for pat in remove_patterns:
        base = re.sub(pat, "", base, flags=re.IGNORECASE).strip()
    base = re.sub(r"\s*(에\s*대해|에\s*대한|관련|기준을|기준|에\s*관해)\s*$", "", base).strip()
    return base or (user_request or "전기설비 기준").strip() or "전기설비 기준"


def _is_broad_rag_query(query: str) -> bool:
    """설명/정리/검토 기준처럼 넓게 묻는 질의는 더 많은 chunk를 가져옵니다."""
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
    """주제별 검색 anchor를 동적으로 붙입니다.

    주제를 특정 문구로 대체하지 않고, 내부 검색 정확도를 높이기 위한 보조 키워드만 반환합니다.
    완전히 매칭되지 않는 주제는 빈 문자열을 반환하여 semantic retrieval을 그대로 사용합니다.
    """
    topic = topic or ""
    anchors: list[str] = []

    _add_anchor_if_matched(topic,
        ("전선식별", "전선 식별", "상별", "색상", "생상", "보호도체", "중성선", "phase", "conductor", "color", "colour", "컬러", "도체"),
        ["KEC 121.2", "표 121.2-1", "KS C IEC 60445", "전선 색상", "상선", "중성선", "보호도체", "L1", "L2", "L3", "N", "PE"], anchors)
    _add_anchor_if_matched(topic,
        ("이격", "거리", "간격", "분리", "clearance", "spacing", "separation"),
        ["이격거리", "최소 이격거리", "전선 간 이격", "설치 간격", "안전거리"], anchors)
    _add_anchor_if_matched(topic,
        ("내진", "지진", "정착", "앵커", "재현주기", "성능목표", "seismic", "anchor"),
        ["내진성능목표", "재현주기", "기능수행", "인명보호", "설비등급", "시설물 관리등급", "정착부"], anchors)
    _add_anchor_if_matched(topic,
        ("접지", "보호접지", "등전위", "접지선", "ground", "grounding", "earthing", "bonding"),
        ["접지", "보호도체", "PE", "등전위", "접지저항", "접지선"], anchors)
    _add_anchor_if_matched(topic,
        ("배선", "전로", "케이블", "전선관", "배관", "트레이", "tray", "cable", "conduit", "raceway"),
        ["배선", "전로", "케이블", "전선관", "케이블 트레이", "시공 기준"], anchors)
    _add_anchor_if_matched(topic,
        ("변압기", "트랜스", "transformer", "용량 선정", "수변전"),
        ["변압기", "정격용량", "부하율", "수변전설비", "변압기 용량", "보호장치"], anchors)
    _add_anchor_if_matched(topic,
        ("mccb", "차단기", "배선용 차단기", "차단 용량", "차단전류", "보호협조", "breaker", "breaking capacity", "icu", "ics"),
        ["MCCB", "차단기", "차단용량", "정격전류", "단락전류", "Icu", "Ics", "보호협조"], anchors)
    _add_anchor_if_matched(topic,
        ("허용전류", "전류용량", "ampacity", "도체 단면적", "전선 굵기", "케이블 굵기"),
        ["허용전류", "전선 굵기", "도체 단면적", "케이블 허용전류", "보정계수", "온도 보정"], anchors)
    _add_anchor_if_matched(topic,
        ("전압강하", "전압 강하", "voltage drop"),
        ["전압강하", "전압 강하율", "전선 길이", "부하전류", "허용 전압강하"], anchors)
    _add_anchor_if_matched(topic,
        ("피뢰", "피뢰기", "서지", "spd", "surge", "lightning"),
        ["피뢰기", "서지보호장치", "SPD", "접지", "뇌보호", "서지"], anchors)
    _add_anchor_if_matched(topic,
        ("ups", "무정전", "비상전원", "예비전원", "발전기", "generator", "emergency power"),
        ["UPS", "무정전전원장치", "비상전원", "예비전원", "발전기", "축전지", "절체"], anchors)
    _add_anchor_if_matched(topic,
        ("조도", "조명", "illumination", "lighting", "lux"),
        ["조도", "조명설비", "조도 기준", "lux", "비상조명", "등기구"], anchors)
    _add_anchor_if_matched(topic,
        ("방폭", "폭발위험", "위험장소", "explosion", "hazardous"),
        ["방폭", "폭발위험장소", "위험장소", "방폭전기설비", "방폭 등급"], anchors)
    _add_anchor_if_matched(topic,
        ("전력품질", "고조파", "역률", "무효전력", "power quality", "harmonic", "power factor"),
        ["전력품질", "고조파", "역률", "무효전력", "전압변동", "플리커"], anchors)

    seen: set[str] = set()
    uniq: list[str] = []
    for a in anchors:
        if a and a not in seen:
            seen.add(a)
            uniq.append(a)
    return " ".join(uniq)


def _expand_retrieval_query(display_query: str) -> str:
    """사용자에게 보이는 질의는 보존하고, 내부 검색 query에만 anchor를 붙입니다."""
    q = (display_query or "").strip()
    anchor = _build_search_anchor_from_topic(q)
    if anchor and anchor not in q:
        return f"{q} {anchor}".strip()
    return q


def _make_followup_query(topic: str, intent_suffix: str) -> str:
    """버튼에 표시/전송되는 질의는 짧고 자연스럽게 유지합니다.

    검색 anchor는 _run_electric()에서 내부 retrieval_query로만 확장합니다.
    """
    topic = (topic or "전기설비 기준").strip()
    return f"{topic} {intent_suffix}".strip()


def _build_suggested_queries(user_request: str, answer_style: str) -> list[dict[str, str]]:
    """프론트에서 버튼/칩으로 렌더링할 추천 질의 목록을 생성합니다.

    - query: 사용자가 보게 될 짧은 후속 질의
    - retrieval_query: 내부 검색용 확장 질의(프론트가 무시해도 백엔드에서 재확장 가능)
    """
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

def _format_suggested_queries_block(suggested_queries: list[dict[str, str]]) -> str:
    """프론트 버튼 미구현 상태에서도 사용자가 클릭형 작업을 인지할 수 있도록 하단 섹션을 붙입니다."""
    if not suggested_queries:
        return ""
    lines = ["\n\n### 관련 작업"]
    for item in suggested_queries:
        label = str(item.get("label") or "").strip()
        query = str(item.get("query") or "").strip()
        if label and query:
            lines.append(f"- [{label}](chat-action://send?query={query})")
    return "\n".join(lines)

# ── Table Markdown 정제 유틸 ────────────────────────────────────────────────
# PDF table parser가 헤더를 읽지 못하면 | 열1 | 열2 | 열3 | ... 형태가 생깁니다.
# 이 값은 RAG 답변 품질을 떨어뜨리므로, LLM context 생성 직전에만 정제합니다.
_BAD_TABLE_HEADER_RE = re.compile(r"^열\d+$")
_EMPTY_TABLE_VALUES = {"", "-", "–", "—", "1", "0", "None", "none", "NULL", "null", "?", "□", "■", "�"}
_BROKEN_GLYPH_RE = re.compile(r"[�■-□▣-▩▯▱▲▼-]")
_NOISE_TOKEN_RE = re.compile(r"^(?:[○●◎◯]\s*)+$|(?:[○●◎◯]\s*\d+\)?)|(?:dun\s*){2,}", re.IGNORECASE)


def _split_markdown_table_row(line: str) -> list[str]:
    """markdown table 1개 row를 cell list로 분해합니다."""
    if not line:
        return []
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _is_markdown_separator(line: str) -> bool:
    """|---|---| 형태의 markdown table 구분선 여부."""
    if not line or "|" not in line:
        return False
    cells = _split_markdown_table_row(line)
    if not cells:
        return False
    return all(re.fullmatch(r":?-{2,}:?", c.strip()) for c in cells if c.strip())

def _clean_table_cell(value: str) -> str:
    """표 셀의 깨진 글리프/HTML/반복 기호 노이즈를 정리합니다."""
    v = _strip_html(str(value or "")).strip()
    v = _BROKEN_GLYPH_RE.sub("", v)
    v = re.sub(r"[ 	]{2,}", " ", v)
    return v.strip()


def _is_empty_or_noise_cell(value: str) -> bool:
    v = _clean_table_cell(value)
    if v in _EMPTY_TABLE_VALUES:
        return True
    if _BAD_TABLE_HEADER_RE.match(v):
        return True
    if _NOISE_TOKEN_RE.search(v):
        return True
    # 원형 숫자/기호만 반복되면 값으로 보지 않음
    if re.fullmatch(r"[○●◎◯①-⑳\s\)\(]+", v):
        return True
    return False


def _table_quality_stats(md: str) -> dict[str, float]:
    """markdown table 품질을 판단하기 위한 placeholder/broken/empty 비율 계산."""
    lines = [line.strip() for line in (md or "").splitlines() if line.strip()]
    headers = _split_markdown_table_row(lines[0]) if lines else []
    bad_header_count = sum(1 for h in headers if _BAD_TABLE_HEADER_RE.match(_clean_table_cell(h)) or _is_empty_or_noise_cell(h))
    meaningful_header_count = sum(1 for h in headers if not _is_empty_or_noise_cell(h))
    data_lines = [line for line in lines[1:] if not _is_markdown_separator(line)]
    cells: list[str] = []
    for line in data_lines:
        cells.extend(_split_markdown_table_row(line))
    empty_count = sum(1 for c in cells if _is_empty_or_noise_cell(c))
    broken_count = len(_BROKEN_GLYPH_RE.findall(md or ""))
    return {
        "header_bad_ratio": bad_header_count / max(len(headers), 1),
        "meaningful_header_count": float(meaningful_header_count),
        "empty_ratio": empty_count / max(len(cells), 1),
        "broken_ratio": broken_count / max(len(md or ""), 1),
    }


def _is_bad_markdown_table(md: str) -> bool:
    """
    markdown table이 LLM/사용자 응답에 그대로 노출되기 어려운지 판단합니다.

    깨진 표 기준:
    - 열1/열2/열3 placeholder header가 많음
    - 빈값/기호/깨진 문자 셀이 과다함
    - 의미 있는 header가 거의 없음
    """
    if not md or "|" not in md:
        return False

    lines = [line.strip() for line in md.splitlines() if line.strip()]
    if len(lines) < 2:
        return False

    stats = _table_quality_stats(md)
    return (
        stats["header_bad_ratio"] >= 0.4
        or stats["empty_ratio"] >= 0.55
        or stats["broken_ratio"] >= 0.02
        or stats["meaningful_header_count"] <= 1
    )

def _clean_bad_markdown_table(md: str) -> str:
    """
    열1/열2/열3 placeholder 컬럼이 많은 깨진 markdown table을
    의미 있는 텍스트 중심 row-wise 문장으로 변환합니다.
    """
    if not md:
        return ""
    md = _strip_html(md)
    if not _is_bad_markdown_table(md):
        return md

    lines = [line.strip() for line in md.splitlines() if line.strip()]
    if len(lines) < 3:
        return md

    headers = [_clean_table_cell(h) for h in _split_markdown_table_row(lines[0])]
    data_lines = [line for line in lines[1:] if not _is_markdown_separator(line)]

    meaningful_col_indices = [
        i for i, h in enumerate(headers)
        if h and not _is_empty_or_noise_cell(h)
    ]

    out: list[str] = []
    seen_lines: set[str] = set()

    for line in data_lines:
        cells = [_clean_table_cell(c) for c in _split_markdown_table_row(line)]
        if not cells:
            continue

        parts: list[str] = []

        for i in meaningful_col_indices:
            if i >= len(cells):
                continue
            header = headers[i].strip()
            value = cells[i].strip()
            if _is_empty_or_noise_cell(value):
                continue
            parts.append(f"{header}: {value}" if header else value)

        if not parts:
            parts = [
                c for c in cells
                if c and not _is_empty_or_noise_cell(c)
            ]

        if parts:
            line_text = " / ".join(parts).strip()
            if line_text and line_text not in seen_lines:
                seen_lines.add(line_text)
                out.append(f"- {line_text}")

    return "\n".join(out) if out else ""

def _get_rag_content_from_row(r: dict) -> str:
    """
    RAG 결과 dict에서 LLM에게 전달할 본문을 선택합니다.

    우선순위:
    1. context_content
    2. table chunk + table_markdown
    3. content
    4. raw_content
    """
    if not isinstance(r, dict):
        return ""

    context_content = str(r.get("context_content") or "").strip()
    if context_content:
        if "|" in context_content and _is_bad_markdown_table(context_content):
            return _clean_bad_markdown_table(context_content)
        return _strip_html(context_content)

    chunk_type = str(r.get("chunk_type") or "").strip()
    table_markdown = str(r.get("table_markdown") or "").strip()

    if chunk_type == "table" and table_markdown:
        if _is_bad_markdown_table(table_markdown):
            return _clean_bad_markdown_table(table_markdown)
        # 정상 표는 markdown 구조를 그대로 유지합니다.
        return _strip_html(table_markdown)

    for key in ("content", "raw_content"):
        content = str(r.get(key) or "").strip()
        if not content:
            continue
        if "|" in content and _is_bad_markdown_table(content):
            return _clean_bad_markdown_table(content)
        return _strip_html(content)

    return ""


def _table_priority(r: dict) -> tuple[int, int, int, int]:
    """RAG context에서 table/neighbor chunk가 text chunk보다 먼저 들어가도록 정렬합니다."""
    if not isinstance(r, dict):
        return (9, 9, 9, 999999)
    chunk_type = str(r.get("chunk_type") or "")
    has_table = bool(str(r.get("table_markdown") or "").strip())
    neighbor = bool(r.get("_neighbor_expanded"))
    try:
        idx = int(r.get("chunk_index") or 999999)
    except (TypeError, ValueError):
        idx = 999999
    return (
        0 if chunk_type == "table" or has_table else 1,
        0 if neighbor else 1,
        0 if _contains_table_value_hint(r) else 1,
        idx,
    )


def _contains_table_value_hint(r: dict) -> bool:
    """전기 표에서 값으로 의미 있는 토큰이 포함되어 있는지 간단히 확인합니다."""
    text = " ".join(str(r.get(k) or "") for k in ("content", "raw_content", "table_markdown"))
    if not text:
        return False
    hints = ("L1", "L2", "L3", "PE", "N", "중성선", "보호도체", "갈색", "흑색", "회색", "청색", "녹", "황")
    return any(h in text for h in hints)

def _rag_chunks_to_readable_markdown(chunks: list[dict], *, max_chunks: int = 8) -> str:
    """RAG 청크 리스트 → LLM synthesis용 context.

    - 정상 table_markdown은 markdown 표 구조를 보존
    - 깨진 table은 의미 있는 텍스트 bullet로 fallback
    - table/neighbor chunk를 먼저 배치하여 LLM이 실제 표 값을 볼 확률을 높임
    """
    out_parts: list[str] = []
    n = 0

    sorted_chunks = sorted(
        [r for r in chunks if isinstance(r, dict)],
        key=_table_priority,
    )

    for r in sorted_chunks:
        raw_table_markdown = str(r.get("table_markdown") or "").strip()
        content = _get_rag_content_from_row(r)
        if not content:
            continue

        n += 1
        if n > max_chunks:
            break

        name = _pretty_doc_name(
            r.get("doc_name") or f"시방 발췌 {n}",
            r.get("category") or "",
        ).strip()
        if len(name) > 100:
            name = name[:97] + "…"

        chunk_type = str(r.get("chunk_type") or "").strip()
        table_tag = " · table" if chunk_type == "table" else ""
        neighbor_tag = " · nearby" if r.get("_neighbor_expanded") else ""

        if (
            chunk_type == "table"
            and raw_table_markdown
            and not _is_bad_markdown_table(raw_table_markdown)
        ):
            inner = content
        else:
            inner = (
                _spec_text_to_readable_markdown(content)
                if ("---" in content)
                else _lines_to_bullet_block(content)
            )

        out_parts.append(f"#### {name}{table_tag}{neighbor_tag}\n\n{inner}")

    if not out_parts:
        return "관련 시방 조항을 찾지 못했습니다."

    return "\n\n".join(out_parts)



# ── AgentState 변환 ──────────────────────────────────────────────────────────

async def _format_state(state: AgentState, workflow_results: list) -> AgentState:
    violations: list[ViolationItem]   = []
    suggestions: list[str]            = []
    pending_fixes: list[PendingFix]   = []
    referenced_laws: list[str]        = []
    retrieved_laws: list[LawReference]= list(state.get("retrieved_laws") or [])
    final_message                     = ""
    current_step: CurrentStep         = "agent_completed"
    response_meta: dict[str, Any]     = {}
    # 사용자 원문 질문: RAG 답변 스타일 분기/추천 질의 생성에 사용
    user_request: str = str(state.get("user_request") or "").strip()

    for block in (workflow_results if isinstance(workflow_results, list) else []):
        agent  = block.get("agent")
        result = block.get("result")

        # ── query 결과 ────────────────────────────────────────────────────
        if agent == "query" and isinstance(result, list):
            # ── [TABLE DEBUG] 청크 메타 로깅 ──────────────────────────────
            n_table = n_text = n_neighbor = 0
            for _r in result:
                if not isinstance(_r, dict):
                    continue
                _ctype = _r.get("chunk_type") or "text"
                _tlen  = len(_r.get("table_markdown") or "")
                _is_nb = _r.get("_neighbor_expanded", False)
                logger.info(
                    "[TABLE DEBUG] chunk_idx=%-4s type=%-6s section=%-30s table_len=%-6s doc=%-30s neighbor=%s",
                    _r.get("chunk_index"),
                    _ctype,
                    (_r.get("section_id") or "")[:30],
                    _tlen,
                    (_r.get("doc_name") or "")[:30],
                    _is_nb,
                )
                if _ctype == "table":
                    n_table += 1
                    if _is_nb:
                        n_neighbor += 1
                else:
                    n_text += 1
            logger.info(
                "[TABLE DEBUG] 집계: total=%d text=%d table=%d (neighbor_added=%d)",
                len(result), n_text, n_table, n_neighbor,
            )
            # ─────────────────────────────────────────────────────────────
            ch_list = [
                r for r in result
                if isinstance(r, dict)
                and (
                    str(r.get("content") or "").strip()
                    or str(r.get("table_markdown") or "").strip()
                    or str(r.get("raw_content") or "").strip()
                    or str(r.get("context_content") or "").strip()
                )
            ]
            ch_list = sorted(ch_list, key=_table_priority)
            if ch_list:
                context_text = _rag_chunks_to_readable_markdown(ch_list)
                answer_style = _detect_answer_style(user_request)
                print(f"[RAG DEBUG] answer_style: {answer_style}")
                prompt = _build_rag_answer_prompt(
                    user_request=user_request,
                    context_text=context_text,
                    answer_style=answer_style,
                )
                try:
                    summary = await llm_service.generate_answer([{"role": "user", "content": prompt}])
                    final_message = summary if isinstance(summary, str) and summary.strip() else context_text
                except Exception as exc:
                    logging.warning("[ElectricNode] RAG summary generation failed: %s", exc, exc_info=True)
                    final_message = context_text
            else:
                answer_style = _detect_answer_style(user_request)
                final_message = "관련 시방 조항을 찾지 못했습니다."
            
            current_step   = "query_completed"
            retrieved_laws = _to_law_references(result)
            meta_rows = [_chunk_to_meta_row(r) for r in result if isinstance(r, dict)]
            answer_style = _detect_answer_style(user_request)
            suggested_queries = _build_suggested_queries(user_request, answer_style) if ch_list else []
            final_message = _strip_html(final_message)
            # suggested_queries는 response_meta로만 전달하고, 본문에는 markdown 링크를 붙이지 않습니다.
            final_message += _format_rag_footer(meta_rows, n_chunks=len(result))
            response_meta = {
                "answer_type": "rag_query",
                "used_rag": bool(result),
                "answer_style": answer_style,
                "suggested_queries": suggested_queries,
                "retrieval": _retrieval_block_compact(meta_rows),
            }

        # ── review 결과 ───────────────────────────────────────────────────
        elif agent == "review" and isinstance(result, dict):
            report = result.get("report") or {}
            fixes  = result.get("fixes")  or []
            items  = report.get("items")  or []
            rag_refs = result.get("rag_references") or []
            det_items = result.get("deterministic_violations") or []

            violations    = _violations_from_items(items)
            pending_fixes = _build_pending_fixes(fixes, items, retrieved_laws)
            suggestions   = [f["description"] for f in pending_fixes if f.get("description")]
            referenced_laws = list({
                v.get("legal_reference", "")
                for v in violations if v.get("legal_reference")
            })
            total         = report.get("total_violations", len(violations))
            final_message = (
                f"전기 검토 완료: 위반 {total}건. "
                "수정 항목을 확인하고 적용할 항목을 선택하세요."
            ) + _format_review_rag_footer(rag_refs)
            current_step  = "pending_fix_review"
            rmeta = [_chunk_to_meta_row(x) for x in rag_refs if isinstance(x, dict)]
            response_meta = {
                "answer_type": "review",
                "used_rag": bool(rmeta),
                "retrieval": _retrieval_block_compact(rmeta),
                "review_categories": {
                    "rag_reference_count": len(rag_refs),
                    "deterministic_count": len(det_items),
                },
                # KPI(recall_lower_bound) 산출용 — 결정론적으로 잡힌 위반의 object_id 집합.
                "deterministic_equipment_ids": sorted({
                    str(v.get("object_id") or v.get("equipment_id") or v.get("handle") or "")
                    for v in det_items
                    if isinstance(v, dict)
                    and (v.get("object_id") or v.get("equipment_id") or v.get("handle"))
                }),
                # KPI(sllm_latency_ms) 산출용 — 워크플로우가 채워두면 그대로, 없으면 빈 리스트.
                "sllm_durations_ms": list(result.get("sllm_durations_ms") or []),
            }

        # ── action 결과 (LLM 선택 객체 분석) ────────────────────────────────
        elif agent == "action" and isinstance(result, dict):
            action_fixes  = result.get("fixes") or []
            violations    = _violations_from_action_fixes(action_fixes)
            pending_fixes = _pending_from_action_fixes(action_fixes)
            suggestions   = [f["description"] for f in pending_fixes if f.get("description")]
            referenced_laws = []
            final_message = result.get("message", "선택 객체 수정 분석이 완료되었습니다.")
            final_message += "\n\n---\n[출처] 액션 분석(시방RAG 미호출)"
            current_step  = "pending_fix_review"
            response_meta = {
                "answer_type": "action_suggestion",
                "used_rag": False,
            }
            
        # ── cad_info 결과 ─────────────────────────────────────────────────
        elif agent == "cad_info":
            final_message = f"CAD 객체 정보 조회 결과:\n{result}"
            current_step = "agent_completed"
            response_meta = {
                "answer_type": "cad_entity_lookup",
                "used_rag": False,
            }

        # ── 직접 텍스트 응답 (일반 대화) ──────────────────────────────────
        elif agent == "direct" and isinstance(result, str):
            final_message = _spec_text_to_readable_markdown(result) + _format_direct_footer()
            current_step = "agent_completed"
            response_meta = {
                "answer_type": "llm_direct",
                "used_rag": False,
                "note": "RAG 미사용·직답",
            }

    if not final_message:
        final_message = "처리가 완료되었습니다."
        if not response_meta:
            response_meta = {"answer_type": "empty", "used_rag": False}

    wf = [
        b.get("agent")
        for b in (workflow_results or [])
        if isinstance(b, dict) and b.get("agent")
    ]
    if wf:
        response_meta = {**response_meta, "invoked_workflow": wf}

    if (not violations) and pending_fixes:
        violations = _violations_from_pending_fixes(pending_fixes)

    final_message = _strip_html(final_message)

    review_result: ReviewResult = {
        "is_violation":    len(violations) > 0,
        "violations":      violations,
        "suggestions":     suggestions,
        "referenced_laws": referenced_laws,
        "final_message":   final_message,
    }

    # ── [디버그] 최종 분석 결과 저장 (눈으로 확인용) ────────────────────
    try:
        import os
        debug_dir = r"C:\Users\Playdata\Desktop\SKN23-FINAL-2TEAM\SKN23-FINAL\SKN23-FINAL-2TEAM\backend\services\agents\elec"
        os.makedirs(debug_dir, exist_ok=True)
        debug_res_file = os.path.join(debug_dir, "debug_review_result.json")
        
        with open(debug_res_file, "w", encoding="utf-8") as f:
            json.dump(review_result, f, ensure_ascii=False, indent=2)
        print(f"[ElectricNode DEBUG] AI 최종 분석 결과를 저장했습니다: {debug_res_file}")
    except Exception as e:
        print(f"[ElectricNode DEBUG] 분석 결과 파일 저장 실패: {e}")
    # ──────────────────────────────────────────────────────────────────

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
    refs: list[LawReference] = []
    for r in query_result:
        rid = r.get("id")
        entry: LawReference = {
            "chunk_id":        str(rid) if rid is not None else str(r.get("section_id") or r.get("chunk_index") or ""),
            "document_id":     str(r.get("document_id") or ""),
            "legal_reference": str(r.get("section_id") or r.get("doc_name") or ""),
            "snippet":         str(_get_rag_content_from_row(r) or ""),
            "score":           float(r.get("score") or 0.0),
            "source_type":     str(r.get("source") or "permanent"),
        }
        if isinstance(rid, int):
            entry["document_chunk_id"] = rid
        refs.append(entry)
    return refs


def _violations_from_items(items: list) -> list[ViolationItem]:
    out: list[ViolationItem] = []
    for item in items:
        req = item.get("required_value")
        obj_id = str(item.get("handle") or item.get("equipment_id") or "")
        row: dict = {
            "object_id":       obj_id,
            "violation_type":  str(item.get("violation_type") or ""),
            "reason":          str(item.get("reason") or ""),
            "legal_reference": str(item.get("reference_rule") or ""),
            "suggestion": (
                f"suggested: {req}" if req else str(item.get("reason") or "")
            ),
            "current_value":  str(item.get("current_value") or ""),
            "required_value": str(item.get("required_value") or ""),
        }
        pa = item.get("proposed_action")
        if isinstance(pa, dict) and pa:
            row["proposed_action"] = pa 
        out.append(row)
    return out


def _violations_from_pending_fixes(pending: list) -> list[ViolationItem]:
    out: list[ViolationItem] = []
    for f in pending or []:
        if not isinstance(f, dict): continue
        eid = str(f.get("equipment_id") or "")
        vtype = str(f.get("violation_type") or "")
        desc = str(f.get("description") or "")
        out.append({
            "object_id": eid,
            "violation_type": vtype,
            "reason": desc or (f"{vtype} (수정 대기)" if vtype else "수정 대기 항목"),
            "legal_reference": "",
            "suggestion": desc,
            "current_value": "",
            "required_value": "",
        })
    return out


def _ref_chunk_id_for_violation(violation: dict, laws: list[LawReference]) -> int | None:
    ref = (violation.get("reference_rule") or "").strip()
    if not ref: return None
    for law in laws or []:
        lr = (law.get("legal_reference") or "").strip()
        if not lr or (lr not in ref and ref not in lr): continue
        dcid = law.get("document_chunk_id")
        if isinstance(dcid, int): return dcid
        ck = law.get("chunk_id")
        if isinstance(ck, str) and ck.isdigit(): return int(ck)
    return None


def _build_pending_fixes(fixes: list, violation_items: list, retrieved_laws: list[LawReference] | None = None) -> list[PendingFix]:
    laws = retrieved_laws or []
    violation_map = {item.get("equipment_id"): item for item in violation_items}
    result: list[PendingFix] = []
    for fix in fixes:
        eq_id    = fix.get("equipment_id", "")
        proposed = fix.get("proposed_fix") or {}
        action   = proposed.get("action", "")
        violation= violation_map.get(eq_id, {})
        ref_cid  = _ref_chunk_id_for_violation(violation, laws)
        row: PendingFix = {
            "fix_id":         str(uuid.uuid4()),
            "equipment_id":   eq_id,
            "violation_type": str(violation.get("violation_type", "")),
            "action":         str(action.value if hasattr(action, "value") else action),
            "description":    str(violation.get("description", "") or violation.get("reason", "") or proposed.get("reason", "")),
            "proposed_fix":   proposed,
        }
        if ref_cid is not None:
            row["reference_chunk_id"] = ref_cid
        result.append(row)
    return result


# ── 2차 LLM 매핑 (규칙 기반 미처리 항목) ────────────────────────────────────

async def _llm_map_unmapped(unmapped: list[str]) -> dict[str, str]:
    if not unmapped: return {}
    names_str = "\n".join(f"- {n}" for n in unmapped)
    messages = [
        {
            "role": "system",
            "content": (
                "당신은 전기 도면 전문가입니다.\n"
                "아래 레이어명·블록명 목록을 전기 전문 용어(한국어)로 변환하세요.\n"
                "반드시 JSON 객체 {\"원본명\": \"전문용어\", ...} 형태로만 응답하세요.\n"
                "변환이 불가능한 항목은 포함하지 마세요.\n"
                "예: {\"P-PIPE\": \"전기\", \"E-SYM\": \"전기 심볼\"}"
            ),
        },
        {"role": "user", "content": f"다음 항목들을 전기 전문 용어로 변환해주세요:\n{names_str}"},
    ]
    try:
        result = await llm_service.generate_answer(
            messages=messages,
            response_format={"type": "json_object"},
        )
        if isinstance(result, dict):
            return {k: v for k, v in result.items() if isinstance(v, str)}
        if isinstance(result, str):
            parsed = json.loads(result)
            return {k: v for k, v in parsed.items() if isinstance(v, str)}
    except Exception as exc:
        logging.warning("[ElectricNode] 2차 LLM 매핑 실패: %s", exc)
    return {}


async def _expand_elec_table_neighbors(
    workflow_results: list,
    db: Any,
    *,
    window: int = 3,
    max_added_per_block: int = 8,
) -> list:
    """
    query agent 결과에서 표 참조 문구(표 N.N에 따른다 등)가 있는 text chunk의
    인접 table chunk를 DB에서 자동으로 추가합니다.

    - 수정 대상: query 결과 블록에 한정 (review/action 블록은 건드리지 않음)
    - window: chunk_index ± N 범위 탐색
    - max_added_per_block: 블록당 추가 table chunk 최대 개수

    [수정 이력]
    - document_id 컬럼이 DB에서 UUID 타입이므로 text()로 명시적 ::uuid 캐스트 사용.
      asyncpg는 Python str을 VARCHAR($1::VARCHAR)로 바인딩하는데,
      PostgreSQL은 uuid = varchar 비교를 암묵적으로 허용하지 않음.
    - 첫 번째 쿼리 실패 시 트랜잭션이 aborted 상태가 되므로
      예외 후 db.rollback()으로 세션을 정상 상태로 복구한 뒤 계속 진행.
    """
    from sqlalchemy import select as _sa_select, cast as _sa_cast
    from sqlalchemy.dialects.postgresql import UUID as _PG_UUID
    from backend.models.schema import DocumentChunk as _DC

    expanded: list = []

    for block in workflow_results:
        if block.get("agent") != "query":
            expanded.append(block)
            continue

        result = block.get("result")
        if not isinstance(result, list):
            expanded.append(block)
            continue

        existing_keys: set[tuple[str, str]] = {
            (str(r.get("document_id") or ""), str(r.get("chunk_index") or ""))
            for r in result
            if isinstance(r, dict)
        }

        table_ref_chunks: list[dict] = []
        for r in result:
            if not isinstance(r, dict):
                continue
            if r.get("chunk_type") == "table":
                continue
            content_text = str(r.get("content") or r.get("raw_content") or "")
            if not _HAS_TABLE_REF_RE.search(content_text):
                continue
            doc_id = str(r.get("document_id") or "")
            try:
                chunk_idx = int(r.get("chunk_index") or 0)
            except (TypeError, ValueError):
                continue
            if doc_id:
                table_ref_chunks.append({"doc_id": doc_id, "chunk_idx": chunk_idx})

        if not table_ref_chunks:
            expanded.append(block)
            continue

        new_chunks: list[dict] = []
        for ref in table_ref_chunks:
            doc_id = ref["doc_id"]
            base_idx = ref["chunk_idx"]
            try:
                # document_id 컬럼은 DB에서 UUID 타입이지만 ORM은 String으로 선언.
                # asyncpg는 Python str을 VARCHAR($1::VARCHAR)로 바인딩하므로
                # uuid = varchar 연산자가 없어 오류가 발생한다.
                # cast()를 쓰면 CAST($1 AS UUID)가 생성되어 asyncpg와 호환된다.
                stmt = (
                    _sa_select(_DC)
                    .where(_DC.document_id == _sa_cast(doc_id, _PG_UUID(as_uuid=False)))
                    .where(_DC.chunk_index >= base_idx - window)
                    .where(_DC.chunk_index <= base_idx + window)
                    .where(_DC.chunk_type == "table")
                )
                rows = await db.execute(stmt)
                neighbors = rows.scalars().all()
            except Exception as exc:
                logger.warning(
                    "[ELEC TABLE EXPAND] DB 조회 실패 doc=%s idx=%s err=%s",
                    doc_id, base_idx, exc,
                )
                # 트랜잭션 aborted 상태 복구 — 이후 쿼리가 InFailedSQLTransactionError 로
                # 연쇄 실패하지 않도록 롤백 후 계속 진행.
                try:
                    await db.rollback()
                except Exception:
                    pass
                continue

            for nb in neighbors:
                key = (str(nb.document_id), str(nb.chunk_index))
                if key in existing_keys:
                    continue
                existing_keys.add(key)
                tm = str(nb.table_markdown or "")
                content_for_llm = tm if tm else str(nb.content or "")
                new_chunks.append({
                    "source": "permanent",
                    "id": nb.id,
                    "document_id": str(nb.document_id),
                    "chunk_index": nb.chunk_index,
                    "content": content_for_llm,
                    "raw_content": str(nb.content or ""),
                    "table_markdown": tm or None,
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

        if new_chunks:
            logger.info(
                "[ELEC TABLE EXPAND] table chunk %d개 추가 (table_ref 감지 %d개 청크)",
                len(new_chunks),
                len(table_ref_chunks),
            )

        new_result = list(result) + new_chunks
        expanded.append({**block, "result": new_result})

    return expanded


def _make_fallback_call(message: str, has_drawing: bool = False, intent: str = "answer", active_ids: list[str] | None = None) -> dict:
    ids = [str(x) for x in (active_ids or []) if x]
    if intent == "answer":
        return {
            "function": {
                "name": "call_query_agent",
                "arguments": json.dumps({"query": message or "전기 시방/규정 질의"}, ensure_ascii=False),
            }
        }
    if has_drawing and intent == "review":
        tid = ids[0] if len(ids) == 1 else "ALL"
        return {
            "function": {
                "name": "call_review_agent",
                "arguments": json.dumps({"target_id": tid}, ensure_ascii=False),
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
            "arguments": json.dumps({"query": message or "전기 규정 검토"}, ensure_ascii=False),
        }
    }


# ── ActionAgent 결과 변환 헬퍼 ───────────────────────────────────────────────

def _violations_from_action_fixes(fixes: list) -> list[ViolationItem]:
    return [
        {
            "object_id":       f.get("handle", ""),
            "violation_type":  f.get("action", "ACTION_REQUIRED"),
            "reason":          f.get("reason", ""),
            "legal_reference": "",
            "suggestion":      f.get("reason", ""),
            "current_value":   f.get("layer", ""),
            "required_value":  (f.get("auto_fix") or {}).get("new_layer") or "",
        }
        for f in fixes
    ]


def _pending_from_action_fixes(fixes: list) -> list[PendingFix]:
    return [
        {
            "fix_id":         str(uuid.uuid4()),
            "equipment_id":   f.get("handle", ""),
            "violation_type": f.get("action", "ACTION_REQUIRED"),
            "action":         f.get("action", ""),
            "description":    f.get("reason", ""),
            "proposed_fix":   f.get("auto_fix") or {},
        }
        for f in fixes
    ]
