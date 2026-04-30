"""
File    : backend/services/memory_service.py
Author  : 양창일
Create  : 2026-04-15
Description : chat_sessions 의 summary_text 와 recent_chat 을 독립적으로 관리하는 서비스

Modification History :
    - 2026-04-15 (양창일) : TEXT 기반 메모리 저장 함수 추가
    - 2026-04-19 (김지우) : AsyncSession 대응을 위한 함수 비동기화 적용
"""

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text


async def load_session_memory(db: AsyncSession, session_id: str) -> dict[str, str]:
    query = text(
        """
        SELECT summary_text, recent_chat
        FROM chat_sessions
        WHERE id = :session_id
        """
    )
    result = await db.execute(query, {"session_id": session_id})
    row = result.mappings().first()
    if row is None:
        raise ValueError("Session not found")

    return {
        "summary_text": str(row["summary_text"] or ""),
        "recent_chat": str(row["recent_chat"] or ""),
    }


async def update_summary_text(db: AsyncSession, session_id: str, summary_text: str) -> None:
    query = text(
        """
        UPDATE chat_sessions
        SET summary_text = :summary_text,
            updated_at = NOW()
        WHERE id = :session_id
        """
    )
    await db.execute(query, {"session_id": session_id, "summary_text": summary_text})
    await db.commit()


async def update_recent_chat(db: AsyncSession, session_id: str, recent_chat: str) -> None:
    query = text(
        """
        UPDATE chat_sessions
        SET recent_chat = :recent_chat,
            updated_at = NOW()
        WHERE id = :session_id
        """
    )
    await db.execute(query, {"session_id": session_id, "recent_chat": recent_chat})
    await db.commit()
