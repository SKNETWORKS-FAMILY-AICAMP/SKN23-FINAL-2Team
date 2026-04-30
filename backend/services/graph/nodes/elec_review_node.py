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
import math as _math
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

from backend.services.agents.elec.workflow_handler import ElecWorkflowHandler
from backend.services.graph.prompt_utils import build_memory_prompt_from_state
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

# ── 인접성 보호 임계값 (mm) ───────────────────────────────────────────────────
# 비전기 레이어에 있는 BLOCK/INSERT가 이 거리 이내에 전기 엔티티가 있으면
# 도메인 필터링에서 제외(보호)하여 AI에게 전달합니다.
_ELEC_ADJACENCY_THRESHOLD_MM: float = 500.0


def _entity_center(el: dict) -> tuple[float, float] | None:
    """엔티티의 대표 좌표(중심)를 반환합니다."""
    # BLOCK/INSERT: insert_point 우선
    ip = el.get("insert_point") or el.get("position") or el.get("center")
    if isinstance(ip, dict):
        try:
            return float(ip.get("x", 0)), float(ip.get("y", 0))
        except (TypeError, ValueError):
            pass
    # bbox 중심값 폴백
    bbox = el.get("bbox")
    if isinstance(bbox, dict):
        try:
            return (
                (float(bbox.get("x1", 0)) + float(bbox.get("x2", 0))) / 2.0,
                (float(bbox.get("y1", 0)) + float(bbox.get("y2", 0))) / 2.0,
            )
        except (TypeError, ValueError):
            pass
    return None


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
    def _lap(label: str, since: float) -> float:
        now = _time.time()
        print(f"[ElectricNode TRACK]  {label:<30} {now - since:5.1f}s  (누적 {now - t0:5.1f}s)")
        return now

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
        
        # ── [환각 방지] 도메인 필터링 및 유령 텍스트 물리적 제거 ──
        domain_tags = mapping_result.get("domain_tags", {})
        all_elements = drawing_data.get("entities", [])
        current_domain = "elec"
        filtered = []

        # ── [인접성 보호 전처리] 전기/공통/미태그 엔티티의 위치 사전 수집 ──
        # 비전기 레이어에 있는 BLOCK이 이 좌표 집합 근처면 전기 장비로 보호한다.
        safe_positions: list[tuple[float, float]] = []
        for _el in all_elements:
            _ly  = str(_el.get("layer", "")).upper()
            _bn  = str(_el.get("block_name") or _el.get("name", "")).upper()
            _tag = domain_tags.get(_ly) or domain_tags.get(_bn)
            if _tag is None or _tag == current_domain or _tag == "common":
                _pos = _entity_center(_el)
                if _pos:
                    safe_positions.append(_pos)

        for el in all_elements:
            etype = str(el.get("type", "")).upper()

            # [핵심] 유령 텍스트 제거 및 표/주석 필터링
            if etype in ("TEXT", "MTEXT"):
                txt = str(el.get("text") or el.get("attributes", {}).get("TEXT") or "").strip()
                if not txt:
                    continue  # 내용 없는 텍스트는 필터링(제외)

                # [환각 방지] 명확한 표 헤더만 제외 (데이터까지 지우지 않도록 주의)
                _txt_up = txt.upper()
                if any(k in _txt_up for k in ("DATA TABLE", "ANGLE SET DATA TABLE", "ENGINEER MODE")):
                    logging.debug("[ElectricNode PROTECT] 표 헤더 제외: %s", txt)
                    continue

            # [LINE 필터링 완화] 너무 짧은 노이즈만 제거
            if etype == "LINE":
                length = float(el.get("length") or 0)
                if length < 10: # 10mm 미만만 진짜 노이즈로 간주
                    continue

            # [POLYLINE 필터링 완화]
            if etype in ("POLYLINE", "LWPOLYLINE"):
                length = float(el.get("length") or 0)
                if length > 100000: # 도곽 테두리 수준(100m)만 제외
                    continue

            # [BLOCK 보존] 블록은 어떤 경우에도 필터링하지 않음
            if etype in ("BLOCK", "INSERT"):
                filtered.append(el)
                continue

            ly = str(el.get("layer", "")).upper()
            bn = str(el.get("block_name") or el.get("name", "")).upper()
            tag = domain_tags.get(ly) or domain_tags.get(bn)

            # 기본적으로 모두 포함하되, 다른 도메인으로 명확히 태그된 경우만 제외
            if tag is None or tag == current_domain or tag == "common":
                filtered.append(el)
            # [인접성 보호] 타 도메인으로 태그된 BLOCK도 인접 전기 엔티티가 있으면 보호
            elif etype in ("BLOCK", "INSERT") and safe_positions:
                pos = _entity_center(el)
                if pos is not None:
                    px, py = pos
                    if any(
                        _math.hypot(px - ex, py - ey) <= _ELEC_ADJACENCY_THRESHOLD_MM
                        for ex, ey in safe_positions
                    ):
                        filtered.append(el)
                        logging.debug(
                            "[ElectricNode PROTECT] 인접성 보호 BLOCK 포함: handle=%s layer=%s",
                            el.get("handle"), el.get("layer"),
                        )

        if len(filtered) < len(all_elements):
            drawing_data["entities"] = filtered
            # LLM에 전달할 레이아웃 데이터도 필터링된 결과로 갱신
            context["raw_layout_data"] = json.dumps(drawing_data, ensure_ascii=False)
            print(f"[ElectricNode FILTER] 도메인 필터링 완료: {len(all_elements)}건 -> {len(filtered)}건 (타 도메인/유령텍스트 제외, 인접 BLOCK 보호 포함)")

        # ── [디버그] 필터링이 완료된 깨끗한 엔티티 저장 (눈으로 확인용) ──
        try:
            import os
            debug_dir = r"C:\Users\Playdata\Desktop\SKN23-FINAL-2TEAM\SKN23-FINAL\SKN23-FINAL-2TEAM\backend\services\agents\elec"
            os.makedirs(debug_dir, exist_ok=True)  # 폴더가 없으면 안전하게 생성
            debug_file = os.path.join(debug_dir, "debug_entities.json")
            
            # handle 기준 중복 제거
            seen_handles: set[str] = set()
            deduped: list = []
            for _e in filtered:
                _h = str(_e.get("handle", ""))
                if _h and _h in seen_handles:
                    continue
                seen_handles.add(_h)
                deduped.append(_e)

            debug_data = {
                "summary": {
                    "total_raw": len(all_elements),
                    "filtered_clean": len(deduped),
                    "duplicates_removed": len(filtered) - len(deduped),
                    "timestamp_unix": t0
                },
                "clean_entities": deduped
            }
            
            with open(debug_file, "w", encoding="utf-8") as f:
                json.dump(debug_data, f, ensure_ascii=False, indent=2)
            print(f"[ElectricNode DEBUG] 환각 방지 필터링 완료된 엔티티 저장: {debug_file}")
        except Exception as e:
            print(f"[ElectricNode DEBUG] 파일 저장 실패: {e}")
        # ──────────────────────────────────────────────────────────────────

        lap_t = _lap("3+4b. 이름/위치 매핑 병렬", lap_t)

        auto_cnt = sum(1 for m in obj_mappings if m.get("method") == "auto")
        llm_cnt = sum(1 for m in obj_mappings if m.get("method") == "llm_fallback")
        print(f"[ElectricNode MAP]  객체 매핑 결과: 총={len(obj_mappings)}쌍 (자동={auto_cnt}, LLM={llm_cnt})")
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
        return await _format_state(state, [{"agent": "direct", "result": tool_calls.strip()}])

    if not isinstance(tool_calls, list) or not tool_calls:
        logging.warning(
            "[ElectricNode] LLM tool 미선택, fallback 적용 (drawing=%s intent=%s)",
            has_drawing,
            intent,
        )
        tool_calls = [_make_fallback_call(message, has_drawing, intent, list(active_ids))]

    tool_names = [c["function"]["name"] for c in tool_calls]
    print(f"[ElectricNode TRACK]  선택된 tool: {tool_names}")

    # 6. Tool 실행 (WorkflowHandler)
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
    lap_t = _lap(f"6. Tool 실행 ({','.join(tool_names)})", lap_t)

    # 7. 결과 → AgentState 변환 후 반환
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

    equipment_hint = ""
    if drawing_loaded and term_map:
        sample = list(term_map.items())[:10]
        lines  = "\n".join(f"  - {k}: {v}" for k, v in sample)
        more   = f"\n  ... 외 {len(term_map) - 10}건" if len(term_map) > 10 else ""
        equipment_hint = f"\n\n[도면 설비 목록 (매핑 후, 최대 10건)]\n{lines}{more}"

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

    dd = context.get("drawing_data") or {}
    focus_ctx = ""
    if dd.get("context_mode") == CONTEXT_MODE_FULL_WITH_FOCUS:
        fe = dd.get("focus_extraction") or {}
        n = int(fe.get("entity_count") or 0)
        focus_ctx = (
            f"\n\n[도면 컨텍스트] JSON에 `entities`는 전체 도면, `focus_extraction`은 사용자가 선택한 구간 스냅샷"
            f"({n}엔티티)입니다. 위반·수정 판단은 active_object_ids·focus_extraction을 우선하고, "
            f"주변·연결 관계는 전체 entities에서 확인하세요."
        )

    intent = context.get("intent") or "answer"
    intent_line = (
        "사용자 의도(라우터): 일반 Q&A/인사/규정 조회 — "
        "인사나 일상 대화(안녕, 고마워 등)에는 도구 없이 직접 짧은 텍스트로만 답하세요. "
        "전기설비 법규·시방서·설계 기준 등 기술 질문이면 call_query_agent를 쓰세요. "
        "call_review_agent(전수 검토)는 ‘도면 검토/위반/전수’를 명시할 때만 쓰세요."
        if intent == "answer"
        else
        f"사용자 의도(라우터): {intent} — review면 전수/위반 분석, action이면 call_action_agent로 선택 객체 수정 검토."
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
        f"반드시 올바른 JSON 형식을 유지해주세요."
        f"- call_query_agent  : 시방서·규정·기준 정보 조회 요청\n"
        f"- call_review_agent : 도면 검토 및 위반사항 분석 요청\n"
        f"  ※ target_id: 설비 id 하나, 또는 'ALL'. 사용자가 드로잉에서 객체만 골랐으면 "
        f"컨텍스트의 raw_layout_data가 그 엔티티만 담는 경우가 있으니 ALL로 둬도 그 범위만 검토됨.\n"
        f"- call_action_agent : 수정 지시 실행. 사용자가 원본 도면을 '수정', '변경', '교체', '옮기기', '그리기', '추가하기' 등을 요청하면 즉시 이 도구를 사용하여 수정/생성 명령을 생성하세요.\n"
        f"  * 중요: 현재 선택된 객체({len(context.get('active_object_ids') or [])}개)가 있다면 이 도구를 사용하여 즉시 처리하세요.\n\n"
        f"[주의사항 - 환각 방지]\n"
        f"1. 도면의 '표(Table)', '주석(Note)', '도면 범례'에 포함된 선(Line)이나 텍스트는 분석하지 마세요.\n"
        f"2. 전선 속성(굵기, 재질 등)이 없는 일반 선을 보고 '굵기가 0이다'라고 분석하는 것은 환각입니다. 확실한 전선 객체만 검토하세요.\n"
        f"3. 동일한 회로에서 발생하는 인접한 위반 사항들은 하나로 통합하여 요약 보고하세요.\n\n"
        f"[답변 형식] 도구 없이 직접 답할 때는 마크다운(### 소제목, - 글머리, **강조**)으로, "
        f"조항을 `---` 한 줄로만 이어 붙이지 말고 소제목·목록으로 구분하세요.\n\n"
        f"반드시 하나의 도구를 선택하세요."
    )


# ── RAG/직접응답 출처 (사용자 답변·API 메타) ────────────────────────────────

def _chunk_to_meta_row(r: dict) -> dict[str, Any]:
    src = (r.get("source") or "permanent") or "permanent"
    return {
        "source": str(src),
        "source_label": "영구_시방_DB" if src == "permanent" else "임시_시방_DB",
        "doc_name": str(r.get("doc_name") or "").strip() or "—",
        "document_id": str(r.get("document_id") or ""),
        "chunk_index": r.get("chunk_index"),
        "domain": str(r.get("domain") or ""),
        "category": str(r.get("category") or ""),
    }


def _citation_line_from_rows(rows: list[dict], *, max_show: int = 3) -> str:
    if not rows: return ""
    show = rows[:max_show]
    parts: list[str] = []
    for m in show:
        name = (m.get("doc_name") or "—")[:48]
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
                "doc": m.get("doc_name", "")[:80],
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


def _rag_chunks_to_readable_markdown(chunks: list[dict], *, max_chunks: int = 5) -> str:
    out_parts: list[str] = []
    n = 0
    for r in chunks:
        if not isinstance(r, dict): continue
        content = (r.get("content") or "").strip()
        if not content: continue
        n += 1
        if n > max_chunks: break
        name = (r.get("doc_name") or f"시방 발췌 {n}").strip()
        if len(name) > 100: name = name[:97] + "…"
        inner = _spec_text_to_readable_markdown(content) if ("---" in content) else _lines_to_bullet_block(content)
        out_parts.append(f"#### {name}\n\n{inner}")
    if not out_parts: return "관련 시방 조항을 찾지 못했습니다."
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

    for block in (workflow_results if isinstance(workflow_results, list) else []):
        agent  = block.get("agent")
        result = block.get("result")

        # ── query 결과 ────────────────────────────────────────────────────
        if agent == "query" and isinstance(result, list):
            ch_list = [r for r in result if isinstance(r, dict) and (r.get("content") or "").strip()]
            if ch_list:
                context_text = _rag_chunks_to_readable_markdown(ch_list)
                prompt = (
                    f"다음은 검색된 전기 시방서 및 규정 내용입니다. 이 정보를 바탕으로 사용자의 질문에 자연스럽고 친절하게 요약된 답변을 작성해주세요.\n"
                    f"답변은 Markdown 형식을 사용하여 보기 좋게 정리해주세요.\n\n"
                    f"[검색 결과]\n{context_text}\n\n"
                    f"사용자 질문: {state.get('user_request')}"
                )
                summary = await llm_service.generate_answer([{"role": "user", "content": prompt}])
                final_message = summary if isinstance(summary, str) else context_text
            else:
                final_message = "관련 시방 조항을 찾지 못했습니다."
            
            current_step   = "query_completed"
            retrieved_laws = _to_law_references(result)
            meta_rows = [_chunk_to_meta_row(r) for r in result if isinstance(r, dict)]
            final_message += _format_rag_footer(meta_rows, n_chunks=len(result))
            response_meta = {
                "answer_type": "rag_query",
                "used_rag": bool(result),
                "retrieval": _retrieval_block_compact(meta_rows),
            }

        # ── review 결과 ───────────────────────────────────────────────────
        elif agent == "review" and isinstance(result, dict):
            report = result.get("report") or {}
            fixes  = result.get("fixes")  or []
            items  = report.get("items")  or []
            rag_refs = result.get("rag_references") or []

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
            "snippet":         str(r.get("content") or ""),
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