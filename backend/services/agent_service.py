"""
File    : backend/services/agent_service.py
Author  : 양창일
Create  : 2026-04-07
Description : 공통 AgentService 에서 LangGraph 기반 review workflow 를 호출하는 서비스

Modification History :
    - 2026-04-07 (양창일) : 초기 구조 생성
    - 2026-04-08 (양창일) : 고정 도메인 검증 및 공통 응답 데이터 정리
    - 2026-04-08 (양창일) : 도메인별 agent 인스턴스 연결 구조로 변경
    - 2026-04-14 (양창일) : LangGraph review graph 호출 구조로 변경
    - 2026-04-14 (양창일) : db 인자와 state 기반 실행을 함께 유지하도록 충돌 해결
    - 2026-04-17 (김지우) : WebSocket 실시간 중계를 위한 run_stream 메서드 및 전기 도메인 추가
    - 2026-04-17 (김지우) : 도메인 통합
    - 2026-04-18 (김지우) : state에 retrieved_specs(하이브리드 시방서 결과) 주입 추가
    - 2026-04-19 (김지우) : 도메인별 전용 LangGraph 모델(arch, fire, pipe, elec) 라우팅 분리 적용
    - 2026-04-19 (김지우) : AgentService 내 도메인 라우팅 및 상태 관리 로직 최적화
"""

from __future__ import annotations
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

from sqlalchemy import text as _sa_text
from backend.api.schemas.agent import AGENT_DOMAINS, AgentDomain
from backend.services.graph.review_graph import (
    review_graph,
    pipe_review_graph,
    electric_review_graph,
    arch_review_graph,
    fire_review_graph,
)
from backend.services.graph.state import AgentState
from backend.core.database import SessionLocal
from backend.services.state_service import (
    append_chat_message,
    load_agent_state,
    save_agent_state,
    tool_calls_from_workflow_steps,
)
from backend.services.memory_service import load_session_memory, update_recent_chat, update_summary_text
from backend.services.graph.nodes.memory_summary_node import (
    format_turn_text, split_turns, join_turns, compress_memory,
    MAX_RECENT_TURNS,
)
from langchain_openai import ChatOpenAI
from backend.core.config import settings
from backend.services.cad_progress import emit_pipeline_step, label_for_stage

class AgentService:
    async def run(
        self,
        domain: AgentDomain,
        state: AgentState,
        payload: dict[str, Any],
        db=None,
    ) -> dict[str, Any]:
        if domain not in AGENT_DOMAINS:
            raise ValueError("Invalid domain")

        graph_state: AgentState = self._build_initial_state(domain, state, payload)
        graph = self._get_domain_graph(domain)

        t0 = time.time()
        t0m = time.monotonic()
        w0 = time.time()
        last_t_wall = t0
        last_m = t0m
        final_state = dict(graph_state)
        sid = str(
            (graph_state.get("session_meta") or {}).get("session_id")
            or graph_state.get("session_id")
            or payload.get("session_id")
            or ""
        )

        print(f"[Agent TRACK] ▶ {domain} 그래프 실행 시작")
        last_m = await emit_pipeline_step(
            session_id=sid or None,
            stage="__start__",
            message=label_for_stage("__start__"),
            t0_monotonic=t0m,
            wall_start_ts=w0,
            last_t=last_m,
        )
        async for event in graph.astream(graph_state):
            for node_name, state_values in event.items():
                now = time.time()
                node_sec = now - last_t_wall
                total_sec = now - t0
                if isinstance(state_values, dict):
                    final_state.update(state_values)
                step = state_values.get("current_step", "") if isinstance(state_values, dict) else ""
                print(f"[Agent TRACK]  {node_name:<25} {node_sec:5.1f}s  (누적 {total_sec:5.1f}s)  step={step}")
                last_t_wall = now
                last_m = await emit_pipeline_step(
                    session_id=sid or None,
                    stage=str(node_name),
                    message=label_for_stage(str(node_name)),
                    t0_monotonic=t0m,
                    wall_start_ts=w0,
                    last_t=last_m,
                )

        print(f"[Agent TRACK] ■ 완료 — 총 {time.time() - t0:.1f}s")
        await emit_pipeline_step(
            session_id=sid or None,
            stage="__done__",
            message="에이전트 파이프라인 완료",
            t0_monotonic=t0m,
            wall_start_ts=w0,
            last_t=last_m,
        )

        return self._format_result(domain, final_state, payload)

    async def run_stream(
        self,
        domain: AgentDomain,
        state: AgentState,
        payload: dict[str, Any],
        db=None,
    ):
        if domain not in AGENT_DOMAINS:
            raise ValueError("Invalid domain")

        graph_state: AgentState = self._build_initial_state(domain, state, payload)
        graph = self._get_domain_graph(domain)

        final_state = dict(graph_state)
        # 이 턴 user 요청 직전의 선택 ID (DB user 행·assistant 행 fall-back)
        active_ids_at_send = list(graph_state.get("active_object_ids") or [])

        t0m = time.monotonic()
        w0 = time.time()
        le = t0m
        ssid = str(
            (graph_state.get("session_meta") or {}).get("session_id")
            or graph_state.get("session_id")
            or payload.get("session_id")
            or ""
        )
        le = await emit_pipeline_step(
            session_id=ssid or None,
            stage="__start__",
            message=label_for_stage("__start__"),
            t0_monotonic=t0m,
            wall_start_ts=w0,
            last_t=le,
        )
        async for event in graph.astream(graph_state):
            for node_name, state_values in event.items():
                if isinstance(state_values, dict):
                    final_state.update(state_values)
                le = await emit_pipeline_step(
                    session_id=ssid or None,
                    stage=str(node_name),
                    message=label_for_stage(str(node_name)),
                    t0_monotonic=t0m,
                    wall_start_ts=w0,
                    last_t=le,
                )
                st = state_values.get("current_step", "") if isinstance(state_values, dict) else ""
                yield {
                    "status": "progress",
                    "node": node_name,
                    "step": st,
                }
        le = await emit_pipeline_step(
            session_id=ssid or None,
            stage="__done__",
            message=label_for_stage("__done__"),
            t0_monotonic=t0m,
            wall_start_ts=w0,
            last_t=le,
        )

        session_id = (state.get("session_meta") or {}).get("session_id") or payload.get("session_id")
        if session_id:
            review_result = final_state.get("review_result") or {}
            final_message = (
                review_result.get("final_message")
                or final_state.get("assistant_response")
                or ""
            )
            user_request = str(payload.get("message") or payload.get("user_request") or "")
            try:
                async with SessionLocal() as save_db:
                    row = await save_db.execute(
                        _sa_text("SELECT id FROM chat_sessions WHERE id = :sid"),
                        {"sid": session_id},
                    )
                    if row.scalar_one_or_none() is not None:
                        _rm = final_state.get("response_meta")
                        resp_m = _rm if isinstance(_rm, dict) else {}
                        wf = resp_m.get("invoked_workflow")
                        await append_chat_message(
                            save_db,
                            session_id,
                            "user",
                            user_request,
                            tool_calls=[],
                            active_object_ids=active_ids_at_send,
                            message_metadata={"source": "run_stream", "domain": str(domain)},
                        )
                        asst_ids = list(final_state.get("active_object_ids") or active_ids_at_send)
                        await append_chat_message(
                            save_db,
                            session_id,
                            "assistant",
                            final_message,
                            tool_calls=tool_calls_from_workflow_steps(wf),
                            active_object_ids=asst_ids,
                            agent_name=f"{str(domain)}_graph",
                            message_metadata=resp_m if resp_m else None,
                        )
            except Exception as e:
                print(f"[run_stream DB 저장 실패] {e}")

        yield {
            "status": "complete",
            "final_state": final_state
        }

    async def chat(
        self,
        domain: AgentDomain,
        session_id: str,
        user_text: str,
        *,
        active_object_ids: list[str] | None = None,
        cad_session_id: str | None = None,
    ) -> str:
        if domain not in AGENT_DOMAINS:
            return "알 수 없는 도메인입니다."

        try:
            summary_text = ""
            recent_chat  = ""
            if session_id:
                try:
                    async with SessionLocal() as db:
                        memory = await load_session_memory(db, session_id)
                        summary_text = memory.get("summary_text", "")
                        recent_chat  = memory.get("recent_chat", "")
                except Exception:
                    pass

            # Redis/S3 도면 캐시 키는 CAD 추출 시점의 session_id (cad_interop)와 동일해야 함.
            # 채팅 세션 ID(chat_sessions.id)와 다를 수 있음.
            draw_cache_id = (cad_session_id or "").strip() or session_id

            drawing_data: dict = {}
            if draw_cache_id:
                try:
                    from backend.services.cad_service import cad_service
                    drawing_data = await cad_service.get_drawing_data(draw_cache_id) or {}
                except Exception:
                    pass

            if not drawing_data and draw_cache_id:
                if (cad_session_id or "").strip():
                    logger.warning(
                        "[AgentService] drawing_path/S3에 session=%s 없음 (추출·S3 저장 실패 또는 TTL)",
                        draw_cache_id,
                    )
                else:
                    logger.warning(
                        "[AgentService] cad_session_id 비어 있음 → 채팅 세션 ID로 조회 중 (%s). "
                        "CAD에서 추출/분석 완료 후에도 동일하면 UI에 도면 캐시 ID가 안 넘어온 상태입니다.",
                        draw_cache_id,
                    )

            graph = self._get_domain_graph(domain)
            
            org_id = "unknown"
            device_id = "unknown"
            s3_path = (
                f"s3://org/{org_id}/{device_id}/cad/raw/{draw_cache_id}.json"
                if drawing_data
                else None
            )

            chat_state = {
                "session_id": session_id or "unknown",
                "org_id": org_id,
                "device_id": device_id,
                "current_phase": "INIT",
                "user_query": user_text,
                "raw_drawing_data_path": s3_path,
                "drawing_data": drawing_data,
                "active_object_ids": list(active_object_ids or []),
                "chat_history": [{"role": "user", "content": user_text}],
                "messages": [("user", user_text)],
                "retry_count": 0,
                "session_meta": {"session_id": session_id, "domain_type": domain},
                "user_request": user_text,
                "retrieved_laws": [],
                "retrieved_specs": [],
                "pending_fixes": [],
                "summary_text": summary_text,
                "recent_chat": recent_chat,
            }

            t0m = time.monotonic()
            w0 = time.time()
            le = t0m
            le = await emit_pipeline_step(
                session_id=str(session_id) if session_id else None,
                stage="__start__",
                message=label_for_stage("__start__"),
                t0_monotonic=t0m,
                wall_start_ts=w0,
                last_t=le,
            )
            final_state: dict = dict(chat_state)
            async for event in graph.astream(chat_state):
                for node_name, state_values in event.items():
                    if isinstance(state_values, dict):
                        final_state.update(state_values)
                    le = await emit_pipeline_step(
                        session_id=str(session_id) if session_id else None,
                        stage=str(node_name),
                        message=label_for_stage(str(node_name)),
                        t0_monotonic=t0m,
                        wall_start_ts=w0,
                        last_t=le,
                    )
            le = await emit_pipeline_step(
                session_id=str(session_id) if session_id else None,
                stage="__done__",
                message=label_for_stage("__done__"),
                t0_monotonic=t0m,
                wall_start_ts=w0,
                last_t=le,
            )
            result_dict: dict = final_state
            review_result = result_dict.get("review_result") or {}
            ai_response = (
                review_result.get("final_message")
                or result_dict.get("assistant_response")
                or result_dict.get("ai_response")
                or "죄송합니다. 답변을 생성하지 못했습니다."
            )

            if session_id and (summary_text or recent_chat or True):
                try:
                    async with SessionLocal() as db:
                        from sqlalchemy import text as _text
                        row = await db.execute(
                            _text("SELECT id FROM chat_sessions WHERE id = :sid"),
                            {"sid": session_id},
                        )
                        if row.scalar_one_or_none() is None:
                            raise ValueError("session not in DB")
                        
                        resp_m = result_dict.get("response_meta") or {}
                        wf2 = (resp_m.get("invoked_workflow") if isinstance(resp_m, dict) else None)
                        await append_chat_message(
                            db,
                            session_id,
                            "user",
                            user_text,
                            tool_calls=[],
                            active_object_ids=active_object_ids,
                            message_metadata={"source": "agent_chat", "domain": str(domain)},
                        )
                        await append_chat_message(
                            db,
                            session_id,
                            "assistant",
                            ai_response,
                            tool_calls=tool_calls_from_workflow_steps(wf2),
                            active_object_ids=active_object_ids,
                            agent_name=f"{str(domain)}_graph",
                            message_metadata=resp_m if resp_m else None,
                        )

                        turns = split_turns(recent_chat)
                        turns.append(format_turn_text(user_text, ai_response))

                        if len(turns) > MAX_RECENT_TURNS:
                            oldest = turns.pop(0)
                            roll_llm = ChatOpenAI(
                                model=settings.OPENAI_MODEL_NAME,
                                api_key=settings.OPENAI_API_KEY,
                                temperature=0,
                            )
                            summary_text = await compress_memory(roll_llm, summary_text, oldest)

                        recent_chat = join_turns(turns)
                        await update_recent_chat(db, session_id, recent_chat)
                        await update_summary_text(db, session_id, summary_text)

                except ValueError:
                    pass 
                except Exception as e:
                    print(f"[Chat DB 저장 실패] {e}")

            # /agent/start 와 동일하게 REVIEW_RESULT 를 cad 그룹에 전송.
            # USER_CHAT 전용 LLM 턴은 기존엔 WebSocket이 없어 C#이 pending_fix/RevCloud를 못 받음.
            final_st: dict = dict(result_dict) if isinstance(result_dict, dict) else {}
            final_st["recent_chat"] = recent_chat
            final_st["summary_text"] = summary_text
            rr0 = final_st.get("review_result") or {}
            has_pending0 = bool(final_st.get("pending_fixes"))
            has_viols0 = bool((rr0 if isinstance(rr0, dict) else {}).get("violations"))
            if session_id and (has_pending0 or has_viols0):
                if has_pending0:
                    try:
                        async with SessionLocal() as db:
                            await save_agent_state(
                                db, str(session_id), final_st
                            )  # type: ignore[arg-type]
                    except Exception as e:
                        logger.warning("[Chat] save_agent_state 실패: %s", e)
                try:
                    from backend.api.routers import agent_api as _agent_api_mod

                    await _agent_api_mod._send_review_websocket(
                        str(session_id), final_st, ai_response
                    )
                except Exception as e:
                    logger.warning("[Chat] REVIEW_RESULT 브로드캐스트 실패: %s", e)

            return ai_response

        except Exception as e:
            print(f"[LLM Error in chat] {str(e)}")
            return "AI 모델(sLLM)과 통신하는 중 문제가 발생했습니다."

    # --- 내부 헬퍼 메서드 ---
    def _build_initial_state(self, domain, state, payload) -> dict:
        """
        [핵심 수정] 어떠한 경우에도 session_id가 None이 되지 않도록 3중 방어막을 칩니다.
        또한 LangGraph 스키마 키 에러를 막기 위해 키 이름들을 강제로 맞춥니다.
        """
        # 1. session_id 추출 3중 방어
        session_id = (
            payload.get("session_id") or 
            state.get("session_id") or 
            (state.get("session_meta") or {}).get("session_id")
        )
        
        if not session_id:
            raise ValueError(f"LangGraph 실행 불가: session_id를 찾을 수 없습니다. payload_keys={list(payload.keys())}")

        org_id = payload.get("org_id") or (state.get("runtime_meta") or {}).get("org_id") or "unknown"
        device_id = payload.get("device_id") or (state.get("runtime_meta") or {}).get("device_id") or "unknown"
        user_query = str(payload.get("message") or payload.get("user_request") or state.get("user_request") or "")
        
        drawing_data = payload.get("drawing_data") or state.get("drawing_data") or {}
        s3_path = f"s3://org/{org_id}/{device_id}/cad/raw/{session_id}.json" if drawing_data else None

        # 2. 기존 상태(state)를 기반으로 새로운 딕셔너리 생성 (하위 호환성 유지)
        new_state = {**state}
        
        # 3. LangGraph 필수 스키마 키 덮어쓰기
        ih = (payload.get("intent_hint") or state.get("intent_hint") or "")
        if isinstance(ih, str):
            ih = ih.strip() or None
        else:
            ih = None
        new_state.update({
            "session_id": str(session_id),
            "org_id": org_id,
            "device_id": device_id,
            "current_phase": "INIT",
            "user_query": user_query,
            "raw_drawing_data_path": s3_path,
            
            # 기존 파이프라인 데이터 매핑
            "user_request": user_query, 
            "session_meta": {**(state.get("session_meta") or {}), "domain_type": domain},
            "drawing_data": drawing_data,
            "retrieved_laws": payload.get("retrieved_laws") or state.get("retrieved_laws") or [],
            "retrieved_specs": payload.get("retrieved_specs") or state.get("retrieved_specs") or [],
            "active_object_ids": list(payload.get("active_object_ids") or state.get("active_object_ids") or []),
            "pending_fixes": list(payload.get("pending_fixes") or state.get("pending_fixes") or []),
            "runtime_meta": {
                "org_id": org_id,
                "device_id": device_id,
                "spec_guid": payload.get("spec_guid") or (state.get("session_extra") or {}).get("spec_guid"),
            },
        })
        if ih is not None:
            new_state["intent_hint"] = ih
        else:
            new_state.pop("intent_hint", None)
        
        return new_state

    def _get_domain_graph(self, domain):
        if domain == "pipe": return pipe_review_graph
        if domain == "elec": return electric_review_graph
        if domain == "arch": return arch_review_graph
        if domain == "fire": return fire_review_graph
        return review_graph

    def _format_result(self, domain, result_state, payload):
        return {
            "domain": domain,
            "current_step": result_state.get("current_step", ""),
            "drawing_data": result_state.get("drawing_data", {}),
            "retrieved_laws": result_state.get("retrieved_laws", []),
            "retrieved_specs": result_state.get("retrieved_specs", []),
            "review_result": result_state.get("review_result", {}),
            "active_object_ids": result_state.get("active_object_ids", []),
            "pending_fixes": result_state.get("pending_fixes", []),
            "summary_text": result_state.get("summary_text", ""),
            "recent_chat": result_state.get("recent_chat", ""),
            "combined_memory": result_state.get("combined_memory", ""),
            "assistant_response": result_state.get("assistant_response", ""),
            "response_meta": result_state.get("response_meta") or {},
            "received_payload": payload,
        }