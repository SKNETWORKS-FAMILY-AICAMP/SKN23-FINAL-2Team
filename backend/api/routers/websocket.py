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
import uuid as _uuid
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from backend.core.socket_manager import manager
from backend.core.database import SessionLocal
from backend.services.agent_service import AgentService
from backend.services.cad_service import cad_service
from backend.services.agents.common.ws_message import make_ws_message
from backend.services.agents.common.draw_command import DrawCommandParser
from backend.services.agents.pipe.sub.action import PipeDrawCommandParser
from backend.services.agents.elec.sub.action import ElecDrawCommandParser
from backend.services.review_cancel import mark_review_cancelled
from backend.services.state_service import (
    confirm_pending_fixes,
    mark_fix_result,
    reject_single_fix,
)

_draw_command_parser = DrawCommandParser()
_pipe_draw_command_parser = PipeDrawCommandParser()
_elec_draw_command_parser = ElecDrawCommandParser()


def _draw_parser_for_domain(domain: str):
    domain_key = str(domain or "").strip().lower()
    if domain_key in {"pipe", "piping", "plumbing", "배관"}:
        return _pipe_draw_command_parser
    if domain_key in {"elec", "electric", "electrical", "전기"}:
        return _elec_draw_command_parser
    return _draw_command_parser

_ACI_COLOR_WORDS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (1, ("빨간", "빨강", "적색", "red")),
    (2, ("노란", "노랑", "황색", "yellow")),
    (3, ("초록", "녹색", "green")),
    (4, ("청록", "cyan")),
    (5, ("파란", "파랑", "청색", "blue")),
    (6, ("보라", "자주", "magenta", "purple")),
    (7, ("흰색", "하양", "백색", "white", "검정", "검은", "black")),
)
_COLOR_ACTION_WORDS = (
    "바꿔", "바꾸", "변경", "수정해", "수정하", "칠해", "칠하", "적용",
    "해줘", "해라", "set", "change", "apply", "paint",
)


_DELETE_WORDS = (
    "삭제", "지워", "지워줘", "제거", "없애", "없애줘", "delete", "remove", "erase",
)


def _direct_delete(user_text: str, active_handles: list[str]) -> dict | None:
    """선택 객체 직접 삭제 shortcut. 삭제 키워드 + 선택 객체 있을 때만 반환."""
    text = (user_text or "").lower()
    if not any(w in text for w in _DELETE_WORDS):
        return None
    if not active_handles:
        return {
            "no_selection": True,
            "message": "삭제할 객체를 AutoCAD에서 먼저 선택한 뒤 다시 요청해 주세요.",
        }
    return {
        "type": "DELETE",
        "target_handles": active_handles,
    }


def _estimate_create_bbox(cad_cmd: dict) -> dict | None:
    """CREATE_ENTITY 명령에서 생성 위치 bbox를 추정한다 (ZOOM_TO_ENTITY용)."""
    try:
        pad = 500.0  # 여백(mm)
        if cad_cmd.get("new_center"):
            cx = float(cad_cmd["new_center"].get("x", 0))
            cy = float(cad_cmd["new_center"].get("y", 0))
            r  = float(cad_cmd.get("new_radius") or 200) + pad
            return {"x1": cx - r, "y1": cy - r, "x2": cx + r, "y2": cy + r}
        if cad_cmd.get("new_start") and cad_cmd.get("new_end"):
            sx, sy = float(cad_cmd["new_start"]["x"]), float(cad_cmd["new_start"]["y"])
            ex, ey = float(cad_cmd["new_end"]["x"]),   float(cad_cmd["new_end"]["y"])
            return {"x1": min(sx, ex) - pad, "y1": min(sy, ey) - pad,
                    "x2": max(sx, ex) + pad, "y2": max(sy, ey) + pad}
        if cad_cmd.get("new_vertices"):
            xs = [float(v["x"]) for v in cad_cmd["new_vertices"]]
            ys = [float(v["y"]) for v in cad_cmd["new_vertices"]]
            return {"x1": min(xs) - pad, "y1": min(ys) - pad,
                    "x2": max(xs) + pad, "y2": max(ys) + pad}
        bx = float(cad_cmd.get("base_x") or 0)
        by = float(cad_cmd.get("base_y") or 0)
        if bx or by:
            return {"x1": bx - pad, "y1": by - pad, "x2": bx + pad, "y2": by + pad}
    except Exception:
        pass
    return None


def _direct_color_modify(user_text: str, active_handles: list[str]) -> dict | list[dict] | None:
    text = (user_text or "").lower()
    if not text:
        return None
    if not any(word in text for word in _COLOR_ACTION_WORDS):
        return None

    new_color = next(
        (
            aci
            for aci, words in _ACI_COLOR_WORDS
            if any(word in text for word in words)
        ),
        None,
    )
    if new_color is None:
        return None

    if not active_handles:
        return {
            "no_selection": True,
            "message": "수정할 객체를 AutoCAD에서 먼저 선택한 뒤 다시 요청해 주세요.",
        }

    return {
        "type": "COLOR",
        "new_color": new_color,
        "target_handles": active_handles,
    }


def _collapse_bulk_modify(cad_cmd: list[dict]) -> dict | None:
    if len(cad_cmd) <= 1:
        return None

    target_handles: list[str] = []
    base_fix: dict | None = None
    for item in cad_cmd:
        if not isinstance(item, dict):
            return None
        fix_item = dict(item)
        handle = fix_item.pop("_handle", None)
        if not handle:
            return None
        if base_fix is None:
            base_fix = fix_item
        elif fix_item != base_fix:
            return None
        target_handles.append(str(handle))

    if not base_fix:
        return None
    fix_type = str(base_fix.get("type") or "").upper()
    if fix_type not in {"LAYER", "COLOR", "LINETYPE", "LINEWEIGHT", "MOVE", "ROTATE", "SCALE", "DELETE"}:
        return None

    base_fix["target_handles"] = target_handles
    return base_fix

# --- [1] 도메인별 명령어 변환기 임포트 ---
from backend.services.agents.elec.sub.action import ActionAgent as ElecActionAgent
from backend.services.agents.pipe.sub.action import ActionAgent as PipeActionAgent
from backend.services.agents.arch.sub.action import ActionAgent as ArchActionAgent
from backend.services.agents.fire.sub.action import ActionAgent as FireActionAgent

router = APIRouter()
agent_service = AgentService()

# 배치 생성 제안 추적
# proposal_type: "create" | "replace" | "modify"
# delete_handles: REPLACE 시 승인 후 삭제할 기존 핸들 목록
# pending_cmds: MODIFY 시 승인 후 전송할 수정 명령 목록
_batch_proposals: dict[str, dict] = {}


async def _send_list_as_proposal(
    cad_cmd: list,
    manager: "ConnectionManager",
    websocket: "WebSocket",
    action_session_id: "str | None" = None,
    *,
    wrap_cad_action: bool = True,
) -> bool:
    """
    list 형식의 CAD 명령을 REPLACE / MODIFY / CREATE 제안으로 변환하여
    구름마크를 표시하고 검토 패널(BATCH_PROPOSAL)로 전송한다.

    wrap_cad_action=True  → cad_action_message() 래퍼 사용 (handle_user_draw 경로)
    wrap_cad_action=False → 직접 dict 구성 (process_user_chat 경로)

    반환: True(처리 완료), False(처리 불가 — 호출측이 fallback)
    """
    def _msg(payload_dict: dict) -> dict:
        payload = dict(payload_dict)
        if action_session_id and not payload.get("session_id"):
            payload["session_id"] = action_session_id
        m = {"action": "CAD_ACTION", "payload": payload}
        if action_session_id:
            m["session_id"] = action_session_id
        return m

    has_create      = any(isinstance(i, dict) and str(i.get("type") or "").upper() == "CREATE_ENTITY" for i in cad_cmd)
    has_delete_cmd  = any(isinstance(i, dict) and str(i.get("type") or "").upper() == "DELETE"        for i in cad_cmd)
    has_modify_hdl  = any(isinstance(i, dict) and "_handle" in i                                       for i in cad_cmd)

    delete_handles_pending = [
        h for i in cad_cmd
        if isinstance(i, dict) and str(i.get("type") or "").upper() == "DELETE"
        for h in (i.get("target_handles") or [])
    ]
    n_creates = sum(1 for i in cad_cmd if isinstance(i, dict) and str(i.get("type") or "").upper() == "CREATE_ENTITY")

    # ── DIRECT APPLY: 제안/구름마크 없이 실제 CAD 수정 명령을 순차 전송 ─────
    # 예: "접지봉을 2개로 수정" → 아래쪽 접지봉 삭제 + E3/x3EA 텍스트를 E2/x2EA로 즉시 수정.
    if any(isinstance(i, dict) and i.get("direct_apply") for i in cad_cmd) and not has_create:
        sent = 0
        user_messages: list[str] = []
        for item in cad_cmd:
            if not isinstance(item, dict):
                continue
            fix = dict(item)
            msg = str(fix.pop("message", "") or "").strip()
            fix.pop("direct_apply", None)
            if msg:
                user_messages.append(msg)

            payload: dict = {"auto_fix": fix}
            if action_session_id:
                payload["session_id"] = action_session_id
            direct_msg: dict = {"action": "CAD_ACTION", "payload": payload}
            if action_session_id:
                direct_msg["session_id"] = action_session_id
            sent += await manager.send_to_group(direct_msg, "cad")

        if sent == 0:
            await websocket.send_json(make_ws_message("CAD_DISCONNECTED", {
                "message": "AutoCAD 플러그인이 서버에 연결되어 있지 않아 CAD 수정 요청을 전달하지 못했습니다."
            }))
            return True

        done_msg = user_messages[-1] if user_messages else "요청하신 CAD 수정을 도면에 반영했습니다."
        await websocket.send_json(
            make_ws_message("CHAT_RESPONSE", {"message": done_msg}) | {"message": done_msg}
        )
        logging.info("[WebSocket] CAD_ACTION(direct list) sent: %d commands=%d", sent, len(cad_cmd))
        return True

    # ── REPLACE 제안: DELETE 보류 + CREATE 후 구름마크 ─────────
    if has_delete_cmd and has_create:
        pid = str(_uuid.uuid4())
        ev  = asyncio.Event()
        _batch_proposals[pid] = {
            "handles": [], "expected": n_creates, "event": ev,
            "websocket": websocket,
            "delete_handles": delete_handles_pending,
            "pending_cmds": [], "proposal_type": "replace",
        }
        sent = 0
        create_items: list[dict] = []
        for item in cad_cmd:
            if not isinstance(item, dict): continue
            if str(item.get("type") or "").upper() == "DELETE": continue  # 보류
            fp: dict = {"auto_fix": item}
            if str(item.get("type") or "").upper() == "CREATE_ENTITY":
                fp["batch_proposal_id"] = pid
                create_items.append(item)
            if action_session_id:
                fp["session_id"] = action_session_id
            sent += await manager.send_to_group(_msg(fp), "cad")

        if sent == 0:
            _batch_proposals.pop(pid, None)
            await websocket.send_json(make_ws_message("CAD_DISCONNECTED", {
                "message": "AutoCAD 플러그인이 서버에 연결되어 있지 않아 CAD 요청을 전달하지 못했습니다."
            }))
            return True

        try:
            await asyncio.wait_for(ev.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass

        collected = _batch_proposals.get(pid, {}).get("handles", [])

        if delete_handles_pending:
            await manager.send_to_group(
                {"action": "MARK_ENTITIES", "payload": {"handles": delete_handles_pending}}, "cad"
            )

        layers = list({c.get("new_layer") or "AI_PROPOSAL" for c in create_items})
        desc   = (
            f"기존 객체 {len(delete_handles_pending)}개를 새 객체 {len(create_items)}개로 "
            "교체하는 제안을 준비했습니다."
        )
        bboxes = [_estimate_create_bbox(c) for c in create_items]
        valid  = [b for b in bboxes if b]
        if valid:
            merged = {
                "x1": min(b["x1"] for b in valid), "y1": min(b["y1"] for b in valid),
                "x2": max(b["x2"] for b in valid), "y2": max(b["y2"] for b in valid),
            }
            await manager.send_to_group({"action": "ZOOM_TO_ENTITY", "payload": {"bbox": merged}}, "cad")
        await websocket.send_json(make_ws_message("BATCH_PROPOSAL", {
            "proposal_id": pid, "count": len(create_items), "handles": collected,
            "layers": layers, "description": desc,
            "delete_count": len(delete_handles_pending), "proposal_type": "replace",
        }))
        logging.info("[Proposal] REPLACE 제안: delete=%d create=%d", len(delete_handles_pending), len(create_items))
        return True

    # ── MODIFY 제안: 속성 변경 ─────────────────────────────────
    if has_modify_hdl:
        bulk_cmd = _collapse_bulk_modify(cad_cmd)
        if bulk_cmd is not None:
            affected = list(bulk_cmd.get("target_handles") or [])
            pending_cmds_list: list[dict] = [bulk_cmd]
        else:
            affected          = [str(i["_handle"]) for i in cad_cmd if isinstance(i, dict) and "_handle" in i]
            pending_cmds_list = [dict(i) for i in cad_cmd if isinstance(i, dict)]

        _type_label = {
            "COLOR": "색상", "LAYER": "레이어", "LINEWEIGHT": "선두께",
            "TEXT_CONTENT": "텍스트", "TEXT_HEIGHT": "글자크기",
        }
        first_type  = str((next((i for i in cad_cmd if isinstance(i, dict)), {})).get("type") or "").upper()
        type_label  = _type_label.get(first_type, "속성")
        desc        = f"선택된 {len(affected)}개 객체의 {type_label}을 수정하는 제안을 준비했습니다."

        pid = str(_uuid.uuid4())
        ev  = asyncio.Event(); ev.set()
        _batch_proposals[pid] = {
            "handles": [], "expected": 0, "event": ev, "websocket": websocket,
            "delete_handles": [], "pending_cmds": pending_cmds_list, "proposal_type": "modify",
        }
        if affected:
            await manager.send_to_group(
                {"action": "MARK_ENTITIES", "payload": {"handles": affected}}, "cad"
            )
        await websocket.send_json(make_ws_message("BATCH_PROPOSAL", {
            "proposal_id": pid, "count": 0, "handles": [], "layers": [],
            "description": desc, "delete_count": 0, "proposal_type": "modify",
        }))
        logging.info("[Proposal] MODIFY 제안: %d개 핸들 desc=%s", len(affected), desc)
        return True

    # ── CREATE 제안 (기존 로직, proposal_type 추가) ─────────────
    if has_create:
        pid = str(_uuid.uuid4())
        ev  = asyncio.Event()
        _batch_proposals[pid] = {
            "handles": [], "expected": n_creates, "event": ev,
            "websocket": websocket,
            "delete_handles": [], "pending_cmds": [], "proposal_type": "create",
        }
        sent = 0
        create_items = []
        for item in cad_cmd:
            if not isinstance(item, dict): continue
            fp = dict(item)
            fp.pop("_handle", None)
            fix_payload: dict = {"auto_fix": fp}
            if str(fp.get("type") or "").upper() == "CREATE_ENTITY":
                fix_payload["batch_proposal_id"] = pid
                create_items.append(fp)
            if action_session_id:
                fix_payload["session_id"] = action_session_id
            sent += await manager.send_to_group(_msg(fix_payload), "cad")

        if sent == 0:
            _batch_proposals.pop(pid, None)
            await websocket.send_json(make_ws_message("CAD_DISCONNECTED", {
                "message": "AutoCAD 플러그인이 서버에 연결되어 있지 않아 CAD 생성 요청을 전달하지 못했습니다."
            }))
            return True

        try:
            await asyncio.wait_for(ev.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass

        collected = _batch_proposals.get(pid, {}).get("handles", [])
        _batch_proposals.pop(pid, {})
        layers = list({c.get("new_layer") or "AI_PROPOSAL" for c in create_items})
        desc   = f"{len(create_items)}개 객체를 새로 생성하는 제안을 준비했습니다. 대상 레이어는 {', '.join(sorted(layers))}입니다."
        bboxes = [_estimate_create_bbox(c) for c in create_items]
        valid  = [b for b in bboxes if b]
        if valid:
            merged = {
                "x1": min(b["x1"] for b in valid), "y1": min(b["y1"] for b in valid),
                "x2": max(b["x2"] for b in valid), "y2": max(b["y2"] for b in valid),
            }
            await manager.send_to_group({"action": "ZOOM_TO_ENTITY", "payload": {"bbox": merged}}, "cad")
        await websocket.send_json(make_ws_message("BATCH_PROPOSAL", {
            "proposal_id": pid, "count": len(create_items), "handles": collected,
            "layers": layers, "description": desc, "delete_count": 0, "proposal_type": "create",
        }))
        logging.info("[Proposal] CREATE 제안: %d개 객체", len(create_items))
        return True

    return False  # 처리 불가

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

    async def try_send_direct_cad_action(
        user_text: str,
        active_object_ids: list,
        chat_session_id: str | None,
        cad_session_id: str | None,
        domain: str,
        view_center: tuple[float, float] | None = None,
    ) -> bool:
        """Handle direct CAD edit/draw chat commands before the full agent path."""
        try:
            action_session_id = str(chat_session_id or cad_session_id or "").strip()

            def cad_action_message(fix_payload: dict) -> dict:
                payload = dict(fix_payload)
                auto_fix = payload.get("auto_fix")
                if isinstance(auto_fix, dict) and str(auto_fix.get("type") or "").upper() == "DELETE":
                    auto_fix = dict(auto_fix)
                    auto_fix.setdefault("modification_tier", 4)
                    payload["auto_fix"] = auto_fix
                if action_session_id and not payload.get("session_id"):
                    payload["session_id"] = action_session_id
                message = {"action": "CAD_ACTION", "payload": payload}
                if action_session_id:
                    message["session_id"] = action_session_id
                return message

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
            active_handles = [str(handle) for handle in (active_object_ids or []) if handle]

            cad_cmd = _direct_delete(user_text, active_handles)
            if cad_cmd is None:
                cad_cmd = _direct_color_modify(user_text, active_handles)
            if cad_cmd is None:
                cad_cmd = await _draw_parser_for_domain(domain).parse(
                    user_text, active_handles, entity_by_handle,
                    view_center=view_center,
                )
            if cad_cmd is None:
                return False

            if isinstance(cad_cmd, dict) and cad_cmd.get("no_selection"):
                warn_msg = cad_cmd.get(
                    "message",
                    "수정할 객체를 AutoCAD에서 먼저 선택한 뒤 다시 요청해 주세요.",
                )
                await websocket.send_json(
                    make_ws_message("CHAT_RESPONSE", {"message": warn_msg})
                    | {"message": warn_msg}
                )
                logging.info("[WebSocket] DrawCommandParser: no selected objects")
                return True

            if isinstance(cad_cmd, list):
                handled = await _send_list_as_proposal(
                    cad_cmd, manager, websocket, action_session_id,
                    wrap_cad_action=True,
                )
                if handled:
                    return True

            if isinstance(cad_cmd, dict):
                ftype = str(cad_cmd.get("type") or "").upper()

                # 직접 삭제: tier 없이 즉시 실행
                if ftype == "DELETE" and cad_cmd.get("target_handles"):
                    clean_cmd = dict(cad_cmd)
                    custom_message = str(clean_cmd.pop("message", "") or "").strip()
                    clean_cmd.pop("direct_apply", None)
                    direct_msg: dict = {"action": "CAD_ACTION", "payload": {"auto_fix": clean_cmd}}
                    if action_session_id:
                        direct_msg["session_id"] = action_session_id
                        direct_msg["payload"]["session_id"] = action_session_id
                    sent = await manager.send_to_group(direct_msg, "cad")
                    n = len(clean_cmd["target_handles"])
                    if custom_message:
                        del_msg = custom_message
                    elif n:
                        del_msg = f"선택하신 {n}개 객체를 CAD에서 삭제했습니다."
                    else:
                        del_msg = "선택하신 객체를 CAD에서 삭제했습니다."
                    await websocket.send_json(
                        make_ws_message("CHAT_RESPONSE", {"message": del_msg})
                        | {"message": del_msg}
                    )
                    await websocket.send_json(make_ws_message("SELECTION_CLEARED", {}))
                    logging.info("[WebSocket] CAD_ACTION(direct delete) sent: %d handles=%d", sent, n)
                    return True

                # 신규 생성 → proposal 플로우로 라우팅 (구름마크 + 승인/거절 패널)
                if ftype.startswith("CREATE"):
                    handled = await _send_list_as_proposal(
                        [cad_cmd], manager, websocket, action_session_id,
                        wrap_cad_action=True,
                    )
                    if handled:
                        return True

                # 그 외 수정 명령 (bulk modify 등) 직접 전송
                sent = await manager.send_to_group(
                    cad_action_message({"auto_fix": cad_cmd}),
                    "cad",
                )
                if sent == 0:
                    await websocket.send_json(
                        make_ws_message(
                            "CAD_DISCONNECTED",
                            {"message": "AutoCAD 플러그인이 서버에 연결되어 있지 않아 CAD 수정 요청을 전달하지 못했습니다."},
                        )
                    )
                logging.info(
                    "[WebSocket] CAD_ACTION(modify) sent: %d type=%s handles=%d",
                    sent,
                    cad_cmd.get("type"),
                    len(cad_cmd.get("target_handles") or []),
                )
                return True

        except asyncio.CancelledError:
            raise
        except Exception as draw_err:
            logging.debug("[WebSocket] DrawCommandParser error ignored: %s", draw_err)

        return False

    async def process_user_chat(data: dict, session_id: str | None):
        payload = data.get("payload", {})
        user_text = payload.get("text", "")
        domain = payload.get("domain") or data.get("domain") or "pipe"
        chat_session_id = payload.get("session_id") or session_id
        active_object_ids = payload.get("active_object_ids") or []
        cad_session_id = (payload.get("cad_session_id") or "").strip() or None

        # 프론트엔드에서 전달한 뷰포트 중심 좌표
        _vcx = payload.get("view_center_x")
        _vcy = payload.get("view_center_y")
        view_center: tuple[float, float] | None = (
            (float(_vcx), float(_vcy)) if _vcx is not None and _vcy is not None else None
        )

        print(
            f"💬 [Chat] {user_text} (선택 객체: {len(active_object_ids)}개, "
            f"cad_session={cad_session_id or '-'}, view_center={view_center})"
        )

        await websocket.send_json(make_ws_message("ANALYSIS_STARTED"))

        if await try_send_direct_cad_action(
            user_text,
            active_object_ids,
            chat_session_id,
            cad_session_id,
            domain,
            view_center=view_center,
        ):
            return

        try:
            chat_result = await agent_service.chat(
                domain, chat_session_id, user_text,
                active_object_ids=active_object_ids,
                cad_session_id=cad_session_id,
            )
        except asyncio.CancelledError:
            logging.info("[WebSocket] USER_CHAT task cancelled session=%s", chat_session_id)
            raise
        except Exception as exc:
            logging.exception("[WebSocket] USER_CHAT failed session=%s", chat_session_id)
            msg = f"요청 처리 중 오류가 발생했습니다: {exc}"
            await websocket.send_json(
                make_ws_message("CHAT_RESPONSE", {"message": msg})
                | {"message": msg}
            )
            return

        # chat()은 {"message": str, "response_meta": dict, "domain": str} dict를 반환
        if isinstance(chat_result, dict):
            ai_response    = chat_result.get("message") or "응답을 생성하지 못했습니다."
            response_meta  = chat_result.get("response_meta") or {}
            msg_domain     = chat_result.get("domain") or domain
        else:
            # 하위 호환 — 문자열 그대로인 경우
            ai_response   = str(chat_result) if chat_result else "응답을 생성하지 못했습니다."
            response_meta = {}
            msg_domain    = domain

        await websocket.send_json(
            make_ws_message("CHAT_RESPONSE", {
                "message":       ai_response,
                "response_meta": response_meta,
                "domain":        msg_domain,
            }) | {
                "message":       ai_response,
                "response_meta": response_meta,
                "domain":        msg_domain,
            }
        )

        try:
            action_session_id = str(chat_session_id or cad_session_id or "").strip()
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
            cad_cmd = await _draw_parser_for_domain(domain).parse(
                user_text, active_object_ids, entity_by_handle,
                view_center=view_center,
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
                await _send_list_as_proposal(
                    cad_cmd, manager, websocket, action_session_id,
                    wrap_cad_action=False,
                )

            elif isinstance(cad_cmd, dict):
                cad_cmd = dict(cad_cmd)
                ftype2 = str(cad_cmd.get("type") or "").upper()
                if ftype2.startswith("CREATE"):
                    # 신규 생성 → proposal 플로우 (구름마크 + 승인/거절)
                    await _send_list_as_proposal(
                        [cad_cmd], manager, websocket, action_session_id,
                        wrap_cad_action=False,
                    )
                else:
                    if ftype2 == "DELETE":
                        cad_cmd.setdefault("modification_tier", 4)
                    fix_payload = {"auto_fix": cad_cmd}
                    if action_session_id:
                        fix_payload["session_id"] = action_session_id
                    msg = {"action": "CAD_ACTION", "payload": fix_payload}
                    if action_session_id:
                        msg["session_id"] = action_session_id
                    await manager.send_to_group(msg, "cad")
                    logging.info(
                        "[WebSocket] CAD_ACTION(수정) 전송: type=%s layer=%s",
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
                sent = await manager.send_to_group(data, "cad")
                if sent == 0:
                    await websocket.send_json(
                        make_ws_message(
                            "CAD_DISCONNECTED",
                            {
                                "message": (
                                    "AutoCAD 플러그인이 서버에 연결되어 있지 않아 "
                                    "도면 추출 요청을 전달하지 못했습니다. "
                                    "CAD Agent를 연결한 뒤 다시 검토해 주세요."
                                )
                            },
                        )
                    )

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

            # 3-b. 생성/수정/교체 제안 승인/거절
            elif action == "APPROVE_ENTITY":
                payload = data.get("payload") or {}
                proposal_id = str(payload.get("proposal_id") or "")
                proposal = _batch_proposals.pop(proposal_id, {})
                ptype = proposal.get("proposal_type", "create")
                delete_handles = proposal.get("delete_handles", [])
                pending_cmds   = proposal.get("pending_cmds",   [])

                # REPLACE: 기존 객체 삭제 (새 객체는 이미 도면에 있음)
                if delete_handles:
                    await manager.send_to_group(
                        {"action": "CAD_ACTION", "payload": {
                            "auto_fix": {"type": "DELETE", "target_handles": delete_handles}
                        }},
                        "cad",
                    )

                # MODIFY: 대기 중이던 수정 명령 CAD에 전송
                for cmd_item in pending_cmds:
                    fix_item = dict(cmd_item)
                    handle   = fix_item.pop("_handle", None)
                    fp: dict = {"auto_fix": fix_item}
                    if handle:
                        fp["handle"] = handle
                    await manager.send_to_group(
                        {"action": "CAD_ACTION", "payload": fp}, "cad"
                    )

                await manager.send_to_group({"action": "CLEAR_ZOOM_HIGHLIGHT"}, "cad")
                msg_map = {
                    "replace": f"교체 제안을 승인했습니다. 기존 객체 {len(delete_handles)}개는 삭제하고 새 객체를 도면에 유지할게요.",
                    "modify":  "수정 제안을 승인했습니다. 이제 변경 내용을 CAD에 반영하겠습니다.",
                    "create":  "생성 제안을 승인했습니다. 새로 만든 객체를 도면에 유지할게요.",
                }
                await websocket.send_json(make_ws_message("CHAT_RESPONSE", {
                    "message": msg_map.get(ptype, "승인 요청을 처리했습니다.")
                }))
                logging.info("[WebSocket] APPROVE_ENTITY type=%s delete=%d modify=%d",
                             ptype, len(delete_handles), len(pending_cmds))

            elif action == "REJECT_ENTITY":
                payload = data.get("payload") or {}
                proposal_id = str(payload.get("proposal_id") or "")
                # 프론트엔드가 보낸 handles (생성된 새 객체) + proposal의 타입 확인
                new_handles = [str(h) for h in (payload.get("handles") or []) if h]
                proposal = _batch_proposals.pop(proposal_id, {})
                ptype = proposal.get("proposal_type", "create")

                # CREATE/REPLACE: 새로 만든 객체 삭제
                if new_handles:
                    await manager.send_to_group(
                        {"action": "CAD_ACTION", "payload": {
                            "auto_fix": {"type": "DELETE", "target_handles": new_handles}
                        }},
                        "cad",
                    )
                # MODIFY: 아무것도 안 함 (기존 객체 그대로 유지)

                await manager.send_to_group({"action": "CLEAR_ZOOM_HIGHLIGHT"}, "cad")
                msg_map = {
                    "replace": "교체 제안을 취소했습니다. 기존 객체는 그대로 유지할게요.",
                    "modify":  "수정 제안을 취소했습니다. 기존 객체는 그대로 유지할게요.",
                    "create":  (
                        f"생성 제안을 취소했습니다. 임시로 만든 {len(new_handles)}개 객체는 도면에서 정리했습니다."
                        if new_handles else "생성 제안을 취소했습니다. 도면에는 새 객체를 남기지 않았어요."
                    ),
                }
                await websocket.send_json(make_ws_message("CHAT_RESPONSE", {
                    "message": msg_map.get(ptype, "취소 요청을 처리했습니다.")
                }))
                logging.info("[WebSocket] REJECT_ENTITY type=%s deleted=%d", ptype, len(new_handles))

            # 4. UI ↔ CAD 릴레이 (기존 로직)
            elif client_type == "ui" and action in (
                "APPROVE_FIX", "REJECT_FIX", "ZOOM_TO_ENTITY", "CAD_ACTION"
            ):
                if action == "REJECT_FIX":
                    await _persist_reject_fix_result(data)
                await manager.send_to_group(data, "cad")

            elif client_type == "cad" and action in (
                "CAD_DATA_EXTRACTED", "HEARTBEAT", "FIX_RESULT", "CREATE_RESULT",
                "DRAWING_LOCAL_DIRTY",
            ):
                if action == "FIX_RESULT":
                    await _persist_cad_fix_result(data)
                elif action == "CREATE_RESULT":
                    # 배치 생성 핸들 수집
                    p = _message_payload(data)
                    bid = str(p.get("batch_proposal_id") or "")
                    h = str(p.get("handle") or p.get("created_handle") or "")
                    if bid and h and bid in _batch_proposals:
                        proposal = _batch_proposals[bid]
                        proposal["handles"].append(h)
                        if len(proposal["handles"]) >= proposal["expected"]:
                            proposal["event"].set()
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
