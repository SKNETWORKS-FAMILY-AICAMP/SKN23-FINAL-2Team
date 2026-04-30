"""
File    : backend/services/state_service.py
Author  : 양창일
Create  : 2026-04-13
Description : chat_sessions.summary_text recent_chat 및 chat_messages 를 관리하는 공통 상태 서비스

Modification History :
    - 2026-04-13 (양창일) : AgentState 로드 저장 및 최근 문맥 관리 함수 추가
    - 2026-04-13 (양창일) : 턴 요약 및 assistant 응답 반영 함수 추가
    - 2026-04-13 (양창일) : agent 실행 결과 반영 함수 추가
    - 2026-04-13 (양창일) : CAD payload 반영 함수 추가
    - 2026-04-13 (양창일) : 세션 state 기반 payload 병합 함수 추가
    - 2026-04-13 (양창일) : retrieved_laws 반영 함수 추가
    - 2026-04-15 (양창일) : JSONB context_state 제거 후 TEXT 메모리 저장 방식으로 리팩토링
    - 2026-04-15 (송주엽) : chat_messages JSONB → TEXT, pending_fixes DB 저장/로드 추가
    - 2026-04-19 (김지우) : AsyncSession 대응을 위한 load_agent_state 등 DB I/O 함수 비동기화 적용
    - 2026-04-23 : review_results INSERT — violation_level 기본 'WARNING' (스키마 NOT NULL·문서 3.16 정합)
    - 2026-04-23 : review_results id 는 DB IDENTITY — INSERT 생략, 저장 후 load_pending_fixes로 fix_id 갱신
    - 2026-04-23 : confirm_pending_fixes — ANY(:ids) 는 asyncpg 에서 int[] 필요(문자열 배열이면 DataError)
"""

import json
import uuid
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from backend.services.graph.nodes.memory_summary_node import build_combined_memory
from backend.services.graph.state import AgentState, ChatMessageRef, TurnSummary
from backend.services.memory_service import update_recent_chat, update_summary_text


def _coerce_review_result_ids_for_any(raw: list[str]) -> list[int]:
    """API는 fix_id를 문자열로 보낼 수 있음. review_results.id 는 INTEGER·ANY(:ids) 는 int[] 바인딩이 필요."""
    out: list[int] = []
    for x in raw:
        try:
            out.append(int(str(x).strip()))
        except (TypeError, ValueError) as e:
            raise ValueError(f"invalid review_results id: {x!r}") from e
    return out


def build_initial_state(
    session_id: str,
    domain_type: str,
    session_title: str,
    user_request: str,
    summary_text: str = "",
    recent_chat: str = "",
) -> AgentState:
    return {
        "session_meta": {
            "session_id": session_id,
            "domain_type": cast(Any, domain_type),
            "session_title": session_title,
        },
        "user_request": user_request,
        "drawing_data": {},
        "retrieved_laws": [],
        "review_result": {
            "is_violation": False,
            "violations": [],
            "suggestions": [],
            "referenced_laws": [],
            "final_message": "",
        },
        "current_step": "request_received",
        "summary_text": summary_text or "",
        "recent_chat": recent_chat or "",
        "combined_memory": build_combined_memory(summary_text or "", recent_chat or ""),
        "assistant_response": "",
        "recent_chat_history": [],
        "turn_summaries": [],
        "active_object_ids": [],
        "recent_message_ids": [],
        "pending_fixes": [],
    }


async def create_chat_session(
    db: AsyncSession,
    session_id: str,
    domain_type: str,
    session_title: str = "",
    org_id: str = "",
) -> None:
    """
    chat_sessions 테이블에 새 세션을 생성합니다.
    device_id는 nullable이므로 C# GUID 세션도 등록 가능합니다.
    """
    await db.execute(
        text(
            """
            INSERT INTO chat_sessions (id, org_id, domain_type, session_title, summary_text, recent_chat)
            VALUES (:id, :org_id, :domain_type, :session_title, '', '')
            ON CONFLICT (id) DO NOTHING
            """
        ),
        {
            "id":            session_id,
            "org_id":        org_id or None,
            "domain_type":   domain_type,
            "session_title": session_title or f"{domain_type} 검토",
        },
    )
    await db.commit()


async def load_agent_state(db: AsyncSession, session_id: str) -> AgentState:
    session_query = text(
        """
        SELECT id, domain_type, session_title, summary_text, recent_chat
        FROM chat_sessions
        WHERE id = :session_id
        """
    )
    result = await db.execute(session_query, {"session_id": session_id})
    row = result.mappings().first()
    if row is None:
        raise ValueError("Session not found")

    state = build_initial_state(
        session_id=str(row["id"]),
        domain_type=str(row["domain_type"]),
        session_title=str(row["session_title"] or ""),
        user_request="",
        summary_text=str(row["summary_text"] or ""),
        recent_chat=str(row["recent_chat"] or ""),
    )

    # PENDING 상태 pending_fixes 복원
    state["pending_fixes"] = await load_pending_fixes(db, session_id)
    return state


async def save_agent_state(db: AsyncSession, session_id: str, state: AgentState) -> None:
    await update_summary_text(db, session_id, state.get("summary_text") or "")
    await update_recent_chat(db, session_id, state.get("recent_chat") or "")
    # pending_fixes 동기화 (기존 PENDING 삭제 후 재삽입)
    pending = state.get("pending_fixes") or []
    if pending is not None:
        await sync_pending_fixes(db, session_id, pending)
        # id 는 DB IDENTITY — INSERT 후 fix_id(노출값) 를 DB id 와 맞춤
        state["pending_fixes"] = await load_pending_fixes(db, session_id)


# ── pending_fixes DB 저장/로드 ────────────────────────────────────────────────

async def load_pending_fixes(db: AsyncSession, session_id: str) -> list:
    """
    review_results 테이블에서 PENDING 또는 CONFIRMED 상태 수정 항목을 불러옵니다.
    - PENDING   : 검토 후 사용자 확인 대기 중
    - CONFIRMED : 사용자가 선택 완료, ActionAgent 실행 대기 중
    두 상태 모두 로드해야 confirm 후 재실행 시 ActionAgent가 수정 목록을 인식할 수 있습니다.
    """
    query = text(
        """
        SELECT id                 AS fix_id,
               target_handle      AS equipment_id,
               violation_type,
               violation_level,
               action,
               description,
               proposed_fix,
               status,
               reference_chunk_id
        FROM review_results
        WHERE session_id = :session_id
          AND status IN ('PENDING', 'CONFIRMED')
        ORDER BY created_at ASC
        """
    )
    result_db = await db.execute(query, {"session_id": session_id})
    rows = result_db.mappings().all()
    result = []
    for r in rows:
        proposed = {}
        try:
            raw = r["proposed_fix"]
            if raw:
                proposed = json.loads(raw)
        except Exception:
            pass
        row = {
            "fix_id":         str(r["fix_id"]),
            "equipment_id":   str(r["equipment_id"] or ""),
            "violation_type": str(r["violation_type"] or ""),
            "action":         str(r["action"] or ""),
            "description":    str(r["description"] or ""),
            "proposed_fix":   proposed,
            "status":         str(r.get("status") or "PENDING"),
        }
        rci = r.get("reference_chunk_id")
        if rci is not None:
            try:
                row["reference_chunk_id"] = int(rci)
            except (TypeError, ValueError):
                pass
        result.append(row)
    return result


async def sync_pending_fixes(db: AsyncSession, session_id: str, pending_fixes: list) -> None:
    """
    pending_fixes 목록을 review_results 테이블과 동기화합니다.
    - 기존 PENDING 행 삭제 후 현재 목록으로 재삽입 (간단한 replace 전략)
    """
    delete_query = text(
        "DELETE FROM review_results WHERE session_id = :session_id AND status = 'PENDING'"
    )
    await db.execute(delete_query, {"session_id": session_id})

    if not pending_fixes:
        await db.commit()
        return

    # id: PostgreSQL GENERATED ALWAYS AS IDENTITY — 수동 UUID 삽입 불가(앱 fix_id 는 INSERT 생략 후 SELECT id)
    insert_query = text(
        """
        INSERT INTO review_results
            (session_id, target_handle, violation_type, violation_level, action,
             description, proposed_fix, status, reference_chunk_id)
        VALUES
            (:session_id, :equipment_id, :violation_type, :violation_level, :action,
             :description, :proposed_fix, 'PENDING', :reference_chunk_id)
        """
    )
    for fix in pending_fixes:
        ref_c = fix.get("reference_chunk_id")
        await db.execute(insert_query, {
            "session_id":     session_id,
            "equipment_id":   str(fix.get("equipment_id", "")),
            "violation_type": str(fix.get("violation_type", "")),
            "violation_level": str(
                (fix.get("violation_level") or "WARNING") or "WARNING"
            ),
            "action":         str(fix.get("action", "")),
            "description":    str(fix.get("description", "")),
            "proposed_fix":   json.dumps(fix.get("proposed_fix") or {}, ensure_ascii=False),
            "reference_chunk_id": int(ref_c) if ref_c is not None else None,
        })
    await db.commit()


async def confirm_pending_fixes(
    db: AsyncSession,
    session_id: str,
    selected_fix_ids: list[str],
) -> list:
    """
    선택된 fix_id만 CONFIRMED 상태로 변경하고, 나머지 PENDING은 REJECTED 처리합니다.
    반환: CONFIRMED 상태 fix 목록
    """
    if selected_fix_ids:
        ids_int = _coerce_review_result_ids_for_any(selected_fix_ids)
        confirm_query = text(
            """
            UPDATE review_results
            SET status = 'CONFIRMED', updated_at = NOW()
            WHERE session_id = :session_id
              AND status = 'PENDING'
              AND id = ANY(:ids)
            """
        )
        await db.execute(confirm_query, {
            "session_id": session_id,
            "ids": ids_int,
        })

    reject_query = text(
        """
        UPDATE review_results
        SET status = 'REJECTED', updated_at = NOW()
        WHERE session_id = :session_id
          AND status = 'PENDING'
        """
    )
    await db.execute(reject_query, {"session_id": session_id})
    await db.commit()

    return await load_confirmed_fixes(db, session_id)


async def apply_single_fix(db: AsyncSession, session_id: str, fix_id: str) -> None:
    """단일 fix를 APPLIED 상태로 변경합니다."""
    try:
        fid = int(str(fix_id).strip())
    except (TypeError, ValueError) as e:
        raise ValueError(f"invalid review_results id: {fix_id!r}") from e
    await db.execute(
        text("""
            UPDATE review_results
            SET status = 'APPLIED', updated_at = NOW()
            WHERE session_id = :session_id AND id = :fix_id
        """),
        {"session_id": session_id, "fix_id": fid},
    )
    await db.commit()


async def mark_fix_result(
    db: AsyncSession,
    session_id: str,
    fix_id: str,
    *,
    success: bool,
) -> int:
    """CAD FIX_RESULT를 DB 상태(APPLIED/FAILED)에 반영합니다."""
    status = "APPLIED" if success else "FAILED"
    if str(fix_id).upper() == "ALL":
        result = await db.execute(
            text("""
                UPDATE review_results
                SET status = :status, updated_at = NOW()
                WHERE session_id = :session_id
                  AND status IN ('PENDING', 'CONFIRMED')
            """),
            {"session_id": session_id, "status": status},
        )
        await db.commit()
        return result.rowcount or 0

    try:
        fid = int(str(fix_id).strip())
    except (TypeError, ValueError) as e:
        raise ValueError(f"invalid review_results id: {fix_id!r}") from e
    result = await db.execute(
        text("""
            UPDATE review_results
            SET status = :status, updated_at = NOW()
            WHERE session_id = :session_id
              AND id = :fix_id
              AND status IN ('PENDING', 'CONFIRMED')
        """),
        {"session_id": session_id, "fix_id": fid, "status": status},
    )
    await db.commit()
    return result.rowcount or 0


async def clear_open_review_results(db: AsyncSession, session_id: str) -> int:
    """도면 revision이 바뀐 경우 아직 열린 검토 결과를 제거합니다."""
    result = await db.execute(
        text("""
            DELETE FROM review_results
            WHERE session_id = :session_id
              AND status IN ('PENDING', 'CONFIRMED', 'FAILED')
        """),
        {"session_id": session_id},
    )
    await db.commit()
    return result.rowcount or 0


async def apply_all_pending(db: AsyncSession, session_id: str) -> int:
    """세션의 모든 PENDING fix를 APPLIED로 변경합니다. 변경된 행 수 반환."""
    result = await db.execute(
        text("""
            UPDATE review_results
            SET status = 'APPLIED', updated_at = NOW()
            WHERE session_id = :session_id AND status = 'PENDING'
        """),
        {"session_id": session_id},
    )
    await db.commit()
    return result.rowcount


async def reject_single_fix(db: AsyncSession, session_id: str, fix_id: str) -> None:
    """단일 fix를 REJECTED 상태로 변경합니다."""
    try:
        fid = int(str(fix_id).strip())
    except (TypeError, ValueError) as e:
        raise ValueError(f"invalid review_results id: {fix_id!r}") from e
    await db.execute(
        text("""
            UPDATE review_results
            SET status = 'REJECTED', updated_at = NOW()
            WHERE session_id = :session_id AND id = :fix_id
        """),
        {"session_id": session_id, "fix_id": fid},
    )
    await db.commit()


async def load_confirmed_fixes(db: AsyncSession, session_id: str) -> list:
    """CONFIRMED 상태 수정 항목을 불러옵니다 (ActionAgent 실행용)."""
    query = text(
        """
        SELECT id AS fix_id, target_handle AS equipment_id,
               violation_type, action, description, proposed_fix
        FROM review_results
        WHERE session_id = :session_id
          AND status = 'CONFIRMED'
        ORDER BY created_at ASC
        """
    )
    result_db = await db.execute(query, {"session_id": session_id})
    rows = result_db.mappings().all()
    result = []
    for r in rows:
        proposed = {}
        try:
            raw = r["proposed_fix"]
            if raw:
                proposed = json.loads(raw)
        except Exception:
            pass
        result.append({
            "fix_id":         str(r["fix_id"]),
            "equipment_id":   str(r["equipment_id"] or ""),
            "violation_type": str(r["violation_type"] or ""),
            "action":         str(r["action"] or ""),
            "description":    str(r["description"] or ""),
            "proposed_fix":   proposed,
        })
    return result


def _json_or_null(obj: list | dict | None) -> str | None:
    if obj is None:
        return None
    if isinstance(obj, (list, dict)) and not obj and isinstance(obj, list):
        return "[]"
    return json.dumps(obj, ensure_ascii=False)


def tool_calls_from_workflow_steps(invoked: list | None) -> list[dict[str, Any]]:
    """
    response_meta.invoked_workflow → DB tool_calls JSON.
    단계마다 `id`를 붙여 추적(및 스키마 tool_call_id와 1차 연동)에 사용.
    """
    if not invoked:
        return []
    return [
        {
            "id": f"wf_{uuid.uuid4().hex[:12]}",
            "type": "workflow_step",
            "agent": str(a),
        }
        for a in invoked if a
    ]


def _tool_calls_for_db(invoked: list | None) -> str:
    return json.dumps(tool_calls_from_workflow_steps(invoked), ensure_ascii=False)


async def append_chat_message(
    db: AsyncSession,
    session_id: str,
    role: str,
    content: str,
    *,
    tool_calls: list | None = None,
    active_object_ids: list[str] | None = None,
    token_count: int | None = None,
    agent_name: str | None = None,
    tool_call_id: str | None = None,
    approval_status: str | None = "completed",
    message_metadata: dict | None = None,
) -> str:
    """
    chat_messages 전 컬럼을 채웁니다.
    - tool_calls: OpenAI tool_calls 형태 list[dict] 또는 workflow 단계(내부에서 JSON 문자열)
    - tool_call_id: OpenAI 'tool' 역할 응답이 특정 assistant tool_calls.id를 가리킬 때 사용.
      미지정이면 assistant 메시지이고 tool_calls[0]에 `id`가 있으면 그 값을 사용(상관 id).
    - active_object_ids: 이 메시지를 남길 당시 도면에 선택(포커스)된 엔티티 ID (JSON list 문자열).
    - message_metadata: DB 컬럼 metadata (응답 response_meta, 도메인 등)
    """
    if tool_calls is None:
        tc_s = "[]"
    elif isinstance(tool_calls, list) and (not tool_calls or isinstance(tool_calls[0], dict)):
        tc_s = json.dumps(tool_calls, ensure_ascii=False)
    elif isinstance(tool_calls, list) and isinstance(tool_calls[0], str):
        tc_s = json.dumps(
            [{"type": "legacy_string", "name": n} for n in tool_calls],
            ensure_ascii=False,
        )
    else:
        tc_s = json.dumps(list(tool_calls) if tool_calls else [], ensure_ascii=False)
    if tool_call_id is None and role == "assistant" and tc_s not in ("", "[]", "null"):
        try:
            arr = json.loads(tc_s)
            if (
                isinstance(arr, list)
                and arr
                and isinstance(arr[0], dict)
                and (tid := (arr[0].get("id") or arr[0].get("tool_call_id")))
            ):
                tool_call_id = str(tid)[:100]
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass
    aoi_s = _json_or_null(active_object_ids or [])
    meta_s = _json_or_null(message_metadata) if message_metadata is not None else None
    tc_val = int(token_count) if token_count is not None else 0
    if tc_val <= 0 and content:
        tc_val = max(1, len((content or "").encode("utf-8")) // 4)

    query = text(
        """
        INSERT INTO chat_messages (
            session_id, role, content,
            tool_calls, active_object_ids,
            token_count, agent_name, tool_call_id, approval_status, metadata
        )
        VALUES (
            :session_id, :role, :content,
            :tool_calls, :active_object_ids,
            :token_count, :agent_name, :tool_call_id, :approval_status, :metadata
        )
        RETURNING id
        """
    )
    result = await db.execute(
        query,
        {
            "session_id":        session_id,
            "role":              role,
            "content":           content,
            "tool_calls":        tc_s,
            "active_object_ids": aoi_s or "[]",
            "token_count":       tc_val,
            "agent_name":        agent_name,
            "tool_call_id":      tool_call_id,
            "approval_status":   approval_status or "completed",
            "metadata":          meta_s,
        },
    )
    msg_id = str(result.scalar_one())
    # 목록에 표시할 제목: 첫 사용자 말(아직 "새 대화"인 세션만)
    if role == "user" and (content or "").strip():
        raw = (content or "").strip()
        short = (raw[:57] + "…") if len(raw) > 60 else raw
        short = short[:200]
        await db.execute(
            text(
                """
                UPDATE chat_sessions
                SET session_title = :t
                WHERE id = :sid
                  AND (session_title IS NULL OR TRIM(session_title) = '' OR session_title = '새 대화')
                """
            ),
            {"t": short, "sid": session_id},
        )
    await db.commit()
    return msg_id


def push_recent_chat_history(
    state: AgentState,
    message_id: str,
    role: str,
    content: str,
    active_object_ids: list[str] | None = None,
    tool_calls: list[str] | None = None,
    max_items: int = 20,
) -> AgentState:
    message_ref: ChatMessageRef = {
        "message_id": message_id,
        "role": role,
        "content": content,
        "active_object_ids": active_object_ids or [],
        "tool_calls": tool_calls or [],
    }
    state["recent_chat_history"] = [*state["recent_chat_history"], message_ref][-max_items:]
    state["recent_message_ids"] = [*state["recent_message_ids"], message_id][-max_items:]
    return state


def apply_user_request(
    state: AgentState,
    user_request: str,
    active_object_ids: list[str] | None = None,
) -> AgentState:
    state["user_request"] = user_request
    state["current_step"] = "request_received"
    state["active_object_ids"] = active_object_ids or []
    return state


def append_turn_summary(
    state: AgentState,
    user_intent: str,
    reviewed_object_ids: list[str] | None = None,
    retrieved_law_refs: list[str] | None = None,
    violations_found: list[str] | None = None,
    suggested_actions: list[str] | None = None,
) -> AgentState:
    turn_summary: TurnSummary = {
        "turn_index": len(state["turn_summaries"]) + 1,
        "user_intent": user_intent,
        "reviewed_object_ids": reviewed_object_ids or [],
        "retrieved_law_refs": retrieved_law_refs or [],
        "violations_found": violations_found or [],
        "suggested_actions": suggested_actions or [],
        "step_after_turn": state["current_step"],
    }
    state["turn_summaries"] = [*state["turn_summaries"], turn_summary]
    return state


def apply_agent_execution_result(state: AgentState, result: dict[str, Any]) -> AgentState:
    state["drawing_data"] = result.get("drawing_data") or {}
    state["retrieved_laws"] = result.get("retrieved_laws") or []
    if result.get("retrieved_specs") is not None:
        state["retrieved_specs"] = result.get("retrieved_specs") or []
    state["review_result"] = result.get("review_result") or state["review_result"]
    state["current_step"] = result.get("current_step") or state["current_step"]
    state["active_object_ids"] = result.get("active_object_ids") or state["active_object_ids"]
    state["summary_text"] = result.get("summary_text") or state["summary_text"]
    state["recent_chat"] = result.get("recent_chat") or state["recent_chat"]
    state["combined_memory"] = result.get("combined_memory") or build_combined_memory(
        state["summary_text"],
        state["recent_chat"],
    )
    state["assistant_response"] = (
        result.get("review_result", {}).get("final_message")
        or result.get("assistant_response")
        or state.get("assistant_response", "")
    )
    if "response_meta" in result:
        state["response_meta"] = result.get("response_meta") or {}
    if result.get("pending_fixes") is not None:
        state["pending_fixes"] = result.get("pending_fixes", [])
    return state


def apply_cad_drawing_data(state: AgentState, drawing_data: dict[str, Any]) -> AgentState:
    state["drawing_data"] = drawing_data
    state["current_step"] = "drawing_parsed"
    return state


def apply_retrieved_laws(state: AgentState, retrieved_laws: list[dict[str, Any]]) -> AgentState:
    state["retrieved_laws"] = retrieved_laws
    if retrieved_laws:
        state["current_step"] = "laws_retrieved"
    return state


def merge_agent_payload_with_state(state: AgentState, payload: dict[str, Any]) -> dict[str, Any]:
    merged_payload = dict(payload)
    if not merged_payload.get("drawing_data"):
        merged_payload["drawing_data"] = state.get("drawing_data", {})
    if not merged_payload.get("retrieved_laws"):
        merged_payload["retrieved_laws"] = state.get("retrieved_laws", [])
    if not merged_payload.get("active_object_ids"):
        merged_payload["active_object_ids"] = state.get("active_object_ids", [])
    if not merged_payload.get("pending_fixes"):
        merged_payload["pending_fixes"] = state.get("pending_fixes", [])
    if not merged_payload.get("summary_text"):
        merged_payload["summary_text"] = state.get("summary_text", "")
    if not merged_payload.get("recent_chat"):
        merged_payload["recent_chat"] = state.get("recent_chat", "")
    return merged_payload
