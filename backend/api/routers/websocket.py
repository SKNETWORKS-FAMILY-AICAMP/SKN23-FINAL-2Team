"""
File    : backend/api/routers/websocket.py
Author  : 김지우
Description : 4대 도메인(전기, 배관, 건축, 소방) 통합 Dispatcher 및 실시간 연동

- EXTRACT_DATA: "cad" 그룹(AutoCAD 플러그인)으로 중계 — SocketMessageHandler.EXTRACT_DATA 와 쌍.
- USER_CHAT: cad_service / agent_service 와 연동(세션·도면 캐시).
- C# /cad Interop(REST) + 본 WebSocket이 CAD 데이터·승인 흐름의 두 축.

Modification History :
    - 2026-04-17 (김지우) : 4개 도메인 Translator(DrawingAgent/ActionAgent) 통합 매핑 및 연동
"""

import asyncio
import contextlib
import json
import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from backend.core.socket_manager import manager
from backend.core.database import SessionLocal
from backend.services.agent_service import AgentService
from backend.services.cad_service import cad_service
from backend.services.agents.common.ws_message import make_ws_message
from backend.services.agents.elec.sub.action import DrawCommandParser
from backend.services.review_cancel import mark_review_cancelled
from backend.services.state_service import (
    confirm_pending_fixes,
    mark_fix_result,
    reject_single_fix,
)

_draw_command_parser = DrawCommandParser()

# --- [1] 도메인별 명령어 변환기 임포트 ---
from backend.services.agents.elec.sub.action import ActionAgent as ElecActionAgent
from backend.services.agents.pipe.sub.action import ActionAgent as PipeActionAgent
from backend.services.agents.arch.sub.action import ActionAgent as ArchActionAgent
from backend.services.agents.fire.sub.action import ActionAgent as FireActionAgent

router = APIRouter()
agent_service = AgentService()

# --- [2] 도메인별 Translator 매핑 딕셔너리 ---
TRANSLATORS = {
    "elec": ElecActionAgent,
    "pipe": PipeActionAgent,
    "arch": ArchActionAgent,
    "fire": FireActionAgent
}


def _message_payload(data: dict) -> dict:
    payload = data.get("payload")
    return payload if isinstance(payload, dict) else {}


def _message_session_id(data: dict) -> str:
    payload = _message_payload(data)
    return str(payload.get("session_id") or data.get("session_id") or "").strip()


def _message_violation_id(data: dict) -> str:
    payload = _message_payload(data)
    return str(payload.get("violation_id") or data.get("violation_id") or "").strip()


async def _persist_reject_fix_result(data: dict) -> None:
    session_id = _message_session_id(data)
    violation_id = _message_violation_id(data)
    if not session_id or not violation_id:
        return

    try:
        async with SessionLocal() as db:
            if violation_id.upper() == "ALL":
                await confirm_pending_fixes(db, session_id, [])
            else:
                await reject_single_fix(db, session_id, violation_id)
    except ValueError:
        logging.debug(
            "[WebSocket] REJECT_FIX DB update skipped: non-db violation_id=%r session=%s",
            violation_id,
            session_id,
        )
    except Exception:
        logging.exception(
            "[WebSocket] REJECT_FIX DB update failed: violation_id=%r session=%s",
            violation_id,
            session_id,
        )


async def _persist_cad_fix_result(data: dict) -> None:
    session_id = _message_session_id(data)
    violation_id = _message_violation_id(data)
    payload = _message_payload(data)
    if not session_id or not violation_id:
        return

    try:
        async with SessionLocal() as db:
            updated = await mark_fix_result(
                db,
                session_id,
                violation_id,
                success=bool(payload.get("success")),
            )
        logging.info(
            "[WebSocket] FIX_RESULT DB update session=%s violation_id=%s success=%s rows=%s",
            session_id,
            violation_id,
            bool(payload.get("success")),
            updated,
        )
    except ValueError:
        logging.debug(
            "[WebSocket] FIX_RESULT DB update skipped: non-db violation_id=%r session=%s",
            violation_id,
            session_id,
        )
    except Exception:
        logging.exception(
            "[WebSocket] FIX_RESULT DB update failed: violation_id=%r session=%s",
            violation_id,
            session_id,
        )


@router.websocket("/ws/{client_type}")
async def websocket_endpoint(websocket: WebSocket, client_type: str):
    await manager.connect(websocket, client_type)
    chat_task: asyncio.Task | None = None

    async def stop_chat_task(send_notice: bool = True):
        nonlocal chat_task
        if chat_task and not chat_task.done():
            chat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await chat_task
            if send_notice:
                await websocket.send_json(
                    make_ws_message("CHAT_CANCELLED", {"message": "에이전트 응답 생성이 중단되었습니다."})
                    | {"message": "에이전트 응답 생성이 중단되었습니다."}
                )
        chat_task = None

    async def process_user_chat(data: dict, session_id: str | None):
        payload = data.get("payload", {})
        user_text = payload.get("text", "")
        domain = payload.get("domain") or data.get("domain") or "pipe"
        chat_session_id = payload.get("session_id") or session_id
        active_object_ids = payload.get("active_object_ids") or []
        cad_session_id = (payload.get("cad_session_id") or "").strip() or None

        print(
            f"💬 [Chat] {user_text} (선택 객체: {len(active_object_ids)}개, "
            f"cad_session={cad_session_id or '-'})"
        )

        await websocket.send_json(make_ws_message("ANALYSIS_STARTED"))

        try:
            ai_response = await agent_service.chat(
                domain, chat_session_id, user_text,
                active_object_ids=active_object_ids,
                cad_session_id=cad_session_id,
            )
        except asyncio.CancelledError:
            logging.info("[WebSocket] USER_CHAT task cancelled session=%s", chat_session_id)
            raise

        if not isinstance(ai_response, str):
            ai_response = str(ai_response) if ai_response else "응답을 생성하지 못했습니다."

        await websocket.send_json(
            make_ws_message("CHAT_RESPONSE", {"message": ai_response})
            | {"message": ai_response}
        )

        try:
            drawing_data = {}
            if cad_session_id:
                drawing_data = await cad_service.get_drawing_data(cad_session_id) or {}
            raw_entities = (
                drawing_data.get("entities")
                or drawing_data.get("elements")
                or []
            )
            entity_by_handle: dict = {
                str(e.get("handle")): e
                for e in raw_entities
                if isinstance(e, dict) and e.get("handle")
            }
            cad_cmd = await _draw_command_parser.parse(
                user_text, active_object_ids, entity_by_handle
            )

            if cad_cmd is None:
                pass

            elif isinstance(cad_cmd, dict) and cad_cmd.get("no_selection"):
                warn_msg = cad_cmd.get(
                    "message",
                    "수정할 객체를 AutoCAD에서 먼저 선택한 후 다시 요청하세요.",
                )
                await websocket.send_json(
                    make_ws_message("CHAT_RESPONSE", {"message": warn_msg})
                    | {"message": warn_msg}
                )
                logging.info("[WebSocket] DrawCommandParser: 선택 객체 없음 — 안내 메시지 전송")

            elif isinstance(cad_cmd, list):
                sent = 0
                for fix_item in cad_cmd:
                    handle = fix_item.pop("_handle", None)
                    payload: dict = {"auto_fix": fix_item}
                    if handle:
                        payload["handle"] = handle
                    await manager.send_to_group(
                        {"action": "CAD_ACTION", "payload": payload}, "cad"
                    )
                    sent += 1
                logging.info(
                    "[WebSocket] CAD_ACTION(수정) 전송: %d건 type=%s",
                    sent,
                    (cad_cmd[0] if cad_cmd else {}).get("type"),
                )

            elif isinstance(cad_cmd, dict):
                await manager.send_to_group(
                    {"action": "CAD_ACTION", "payload": {"auto_fix": cad_cmd}},
                    "cad",
                )
                logging.info(
                    "[WebSocket] CAD_ACTION(생성) 전송: type=%s layer=%s",
                    cad_cmd.get("type"), cad_cmd.get("new_layer"),
                )

        except asyncio.CancelledError:
            raise
        except Exception as _draw_err:
            logging.debug("[WebSocket] DrawCommandParser 오류(무시): %s", _draw_err)
    
    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action", "")
            domain = data.get("domain", "pipe")
            session_id = data.get("session_id")

            # 1. React → C# 도면 재추출 요청 릴레이
            if action == "EXTRACT_DATA":
                await manager.send_to_group(data, "cad")

            # 2. 일반 채팅 메시지 처리
            elif action == "CANCEL_REVIEW":
                payload = data.get("payload", {}) or {}
                cancel_session_id = (
                    payload.get("session_id")
                    or payload.get("cad_session_id")
                    or session_id
                    or ""
                )
                if cancel_session_id:
                    mark_review_cancelled(str(cancel_session_id))
                    await manager.send_to_group(
                        {
                            "action": "REVIEW_CANCELLED",
                            "session_id": str(cancel_session_id),
                            "message": "사용자에 의해 분석이 중단되었습니다.",
                        },
                        "ui",
                    )
                await manager.send_to_group(data, "cad")

            elif action == "CANCEL_CHAT":
                await stop_chat_task(send_notice=True)

            elif action == "USER_CHAT":
                await stop_chat_task(send_notice=False)
                chat_task = asyncio.create_task(process_user_chat(data, session_id))

            # 3. Ping/Pong keepalive
            elif action == "PING":
                await websocket.send_json(make_ws_message("PONG"))

            # 4. UI ↔ CAD 릴레이 (기존 로직)
            elif client_type == "ui" and action in (
                "APPROVE_FIX", "REJECT_FIX", "ZOOM_TO_ENTITY", "CAD_ACTION"
            ):
                if action == "REJECT_FIX":
                    await _persist_reject_fix_result(data)
                await manager.send_to_group(data, "cad")

            elif client_type == "cad" and action in (
                "CAD_DATA_EXTRACTED", "HEARTBEAT", "FIX_RESULT", "CREATE_RESULT"
            ):
                if action == "FIX_RESULT":
                    await _persist_cad_fix_result(data)
                await manager.send_to_group(data, "ui")

    except WebSocketDisconnect:
        await stop_chat_task(send_notice=False)
        manager.disconnect(websocket, client_type)
    except Exception as e:
        await stop_chat_task(send_notice=False)
        print(f"WebSocket Error: {e}")
        manager.disconnect(websocket, client_type)
        try:
            await websocket.close(code=1001)
        except Exception:
            pass
