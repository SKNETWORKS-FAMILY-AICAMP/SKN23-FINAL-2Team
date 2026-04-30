"""
File    : backend/core/socket_manager.py
Author  : 김지우
Create  : 2026-04-11
Description : 통신 허브 구축 (FAST API)

Modification History :
    - 2026-04-11 (김지우) : C#과 리액트를 구분해서 관리하는 connectionManger 생성
    - 2026-04-15 (김지우) : 코드 보완 (연결 끊긴 소켓 처리하는 예외 로직)
"""
from fastapi import WebSocket
from typing import Dict, List

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, List[WebSocket]] = {
            "cad": [],
            "ui": []
        }

    async def connect(self, websocket: WebSocket, client_type: str):
        # CRIT-2: 허용되지 않은 클라이언트 타입은 accept 전 즉시 거절 → 소켓 누수 방지
        if client_type not in self.active_connections:
            await websocket.close(code=1008, reason="Invalid client type")
            return
        await websocket.accept()
        self.active_connections[client_type].append(websocket)

    def disconnect(self, websocket: WebSocket, client_type: str):
        if client_type in self.active_connections:
            if websocket in self.active_connections[client_type]:
                self.active_connections[client_type].remove(websocket)

    async def send_to_group(self, message: dict, client_type: str):
        # CRIT-1: 순회 중 리스트 수정 금지 → dead 목록 분리 후 일괄 제거
        dead: list = []
        for connection in list(self.active_connections.get(client_type, [])):
            try:
                await connection.send_json(message)
            except Exception:
                dead.append(connection)
        for conn in dead:
            self.disconnect(conn, client_type)

manager = ConnectionManager()