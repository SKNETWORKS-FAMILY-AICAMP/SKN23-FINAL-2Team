"""
File    : backend/services/agents/common/ws_message.py
Author  : 김지우
Create  : 2026-04-24
Description : 백엔드→클라이언트 WebSocket 메시지 팩토리.
              모든 도메인의 상태 메시지를 일관된 포맷으로 생성한다.
"""

from __future__ import annotations
from typing import Any


def make_ws_message(action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    WebSocket 메시지 딕셔너리를 생성한다.

    make_ws_message("ANALYSIS_STARTED")
    # {"action": "ANALYSIS_STARTED"}

    make_ws_message("CHAT_RESPONSE", {"message": "..."})
    # {"action": "CHAT_RESPONSE", "payload": {"message": "..."}}
    """
    msg: dict[str, Any] = {"action": action}
    if payload is not None:
        msg["payload"] = payload
    return msg
