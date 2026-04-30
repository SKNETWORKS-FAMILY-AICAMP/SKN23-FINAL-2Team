"""
File    : backend/services/session_service.py
Author  : 양창일
Create  : 2026-04-13
Description : chat_sessions 생성 및 조회를 처리하는 서비스

Modification History :
    - 2026-04-13 (양창일) : 세션 생성 및 조회 함수 추가
    - 2026-04-14 (양창일) : devices 존재 여부를 선검증하도록 수정
    - 2026-04-15 (양창일) : summary_text recent_chat 컬럼 기반 응답으로 변경
    - 2026-04-19 (김지우) : AsyncSession 대응을 위한 함수 비동기화 적용
    - 2026-04-22 : get_session_messages — tool_calls, active_object_ids JSON 파싱
"""

from __future__ import annotations

import json
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text


def _parse_json_list_column(val) -> list:
    if val is None or val == "":
        return []
    if isinstance(val, list):
        return val
    try:
        p = json.loads(val)
        return p if isinstance(p, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _parse_json_object_column(val) -> dict | None:
    if val is None or val == "":
        return None
    if isinstance(val, dict):
        return val
    try:
        p = json.loads(val)
        return p if isinstance(p, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


async def create_chat_session(
    db: AsyncSession,
    device_id: str,
    domain_type: str,
    session_title: str,
) -> dict:
    device_query = text(
        """
        SELECT id
        FROM devices
        WHERE id = :device_id
        """
    )
    result = await db.execute(device_query, {"device_id": device_id})
    device_row = result.first()
    if device_row is None:
        raise ValueError("Device not found. Create or use an existing devices.id first.")

    query = text(
        """
        INSERT INTO chat_sessions (device_id, domain_type, session_title, summary_text, recent_chat)
        VALUES (
            :device_id,
            :domain_type,
            :session_title,
            :summary_text,
            :recent_chat
        )
        RETURNING id, device_id, domain_type, session_title, summary_text, recent_chat
        """
    )
    result = await db.execute(
        query,
        {
            "device_id": device_id,
            "domain_type": domain_type,
            "session_title": session_title,
            "summary_text": "",
            "recent_chat": "",
        },
    )
    await db.commit()
    row = result.mappings().one()
    return {
        "session_id": str(row["id"]),
        "device_id": str(row["device_id"]),
        "domain_type": str(row["domain_type"]),
        "session_title": str(row["session_title"]),
        "summary_text": str(row["summary_text"] or ""),
        "recent_chat": str(row["recent_chat"] or ""),
    }


AGENT_TYPE_TO_DOMAIN = {
    "전기": "elec", "배관": "pipe", "건축": "arch", "소방": "fire",
    "elec": "elec", "pipe": "pipe", "arch": "arch", "fire": "fire",
}


import uuid

async def create_session_for_frontend(
    db: AsyncSession,
    agent_type: str,
    dwg_filename: str = "",
    device_id: str = "",
) -> dict:
    """프론트엔드 채팅 UI에서 호출하는 세션 생성. device_id가 있으면 기기별 격리."""
    domain_type = AGENT_TYPE_TO_DOMAIN.get(agent_type, agent_type)
    title = "새 대화"
    
    safe_device_id = device_id.strip() if device_id else None
    if safe_device_id:
        try:
            uuid.UUID(safe_device_id)
        except ValueError:
            safe_device_id = None

    result = await db.execute(
        text(
            """
            INSERT INTO chat_sessions (device_id, domain_type, session_title, summary_text, recent_chat)
            VALUES (:device_id, :domain_type, :session_title, '', '')
            RETURNING id, device_id, domain_type, session_title, created_at
            """
        ),
        {"device_id": safe_device_id, "domain_type": domain_type, "session_title": title},
    )
    await db.commit()
    row = result.mappings().one()
    created_at = row["created_at"]
    return {
        "id": str(row["id"]),
        "agent_type": str(row["domain_type"]),
        "title": str(row["session_title"]),
        "dwg_filename": dwg_filename,
        "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at or ""),
        "message_count": 0,
    }


async def list_sessions_by_domain(
    db: AsyncSession,
    domain_type: str,
    device_id: str = "",
) -> list[dict]:
    """도메인별 세션 목록 조회 (메시지 수 포함). device_id가 있으면 해당 기기 세션만 반환."""
    # 'CAD-Agent가 준비되었습니다...' 안내 메시지만 있는 세션은 목록에서 제외하는 조건
    # 1) 메시지 개수가 0개보다 많아야 함
    # 2) 만약 메시지가 1개라면, 그 내용이 안내 문구와 정확히 일치하거나 포함하면 안 됨
    filter_condition = """
        HAVING COUNT(cm.id) > 0
           AND NOT (COUNT(cm.id) = 1 AND (MAX(cm.content) LIKE 'CAD-Agent가 준비되었습니다%' OR MAX(cm.content) = ''))
    """

    if device_id:
        result = await db.execute(
            text(
                f"""
                SELECT cs.id, cs.domain_type, cs.session_title, cs.created_at,
                       COUNT(cm.id) AS message_count
                FROM chat_sessions cs
                LEFT JOIN chat_messages cm ON cm.session_id = cs.id
                WHERE cs.domain_type = :domain_type
                  AND cs.device_id = :device_id
                GROUP BY cs.id, cs.domain_type, cs.session_title, cs.created_at
                {filter_condition}
                ORDER BY cs.created_at DESC
                """
            ),
            {"domain_type": domain_type, "device_id": device_id},
        )
    else:
        result = await db.execute(
            text(
                f"""
                SELECT cs.id, cs.domain_type, cs.session_title, cs.created_at,
                       COUNT(cm.id) AS message_count
                FROM chat_sessions cs
                LEFT JOIN chat_messages cm ON cm.session_id = cs.id
                WHERE cs.domain_type = :domain_type
                GROUP BY cs.id, cs.domain_type, cs.session_title, cs.created_at
                {filter_condition}
                ORDER BY cs.created_at DESC
                """
            ),
            {"domain_type": domain_type},
        )
    rows = result.mappings().all()
    out = []
    for row in rows:
        created_at = row["created_at"]
        out.append({
            "id": str(row["id"]),
            "agent_type": str(row["domain_type"]),
            "title": str(row["session_title"] or "새 대화"),
            "dwg_filename": "",
            "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at or ""),
            "message_count": int(row["message_count"]),
        })
    return out


async def get_session_messages(db: AsyncSession, session_id: str) -> list[dict]:
    """세션의 채팅 메시지 목록 조회."""
    result = await db.execute(
        text(
            """
            SELECT id, session_id, role, content, tool_calls, active_object_ids,
                   token_count, agent_name, tool_call_id, approval_status, metadata, created_at
            FROM chat_messages
            WHERE session_id = :session_id
            ORDER BY created_at ASC
            """
        ),
        {"session_id": session_id},
    )
    rows = result.mappings().all()
    out = []
    for row in rows:
        created_at = row["created_at"]
        # DB의 "assistant" → 프론트의 "agent" 로 변환
        role = "agent" if row["role"] == "assistant" else str(row["role"])

        out.append({
            "id": str(row["id"]),
            "session_id": str(row["session_id"]),
            "role": role,
            "content": str(row["content"] or ""),
            "active_object_ids": _parse_json_list_column(row.get("active_object_ids")),
            "tool_calls": _parse_json_list_column(row.get("tool_calls")),
            "token_count": int(row["token_count"] or 0) if row.get("token_count") is not None else 0,
            "agent_name": row.get("agent_name"),
            "tool_call_id": row.get("tool_call_id"),
            "approval_status": row.get("approval_status") or "completed",
            "metadata": _parse_json_object_column(row.get("metadata")),
            "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at or ""),
        })
    return out


async def get_chat_session(db: AsyncSession, session_id: str) -> dict:
    query = text(
        """
        SELECT id, device_id, domain_type, session_title, summary_text, recent_chat
        FROM chat_sessions
        WHERE id = :session_id
        """
    )
    result = await db.execute(query, {"session_id": session_id})
    row = result.mappings().first()
    if row is None:
        raise ValueError("Session not found")

    return {
        "session_id": str(row["id"]),
        "device_id": str(row["device_id"]),
        "domain_type": str(row["domain_type"]),
        "session_title": str(row["session_title"]),
        "summary_text": str(row["summary_text"] or ""),
        "recent_chat": str(row["recent_chat"] or ""),
    }
