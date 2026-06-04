"""Small WebSocket fan-out for generation progress."""

from __future__ import annotations

import time
from typing import Dict, List, Optional

from fastapi import WebSocket
from starlette.websockets import WebSocketState


class ConnectionManager:
    def __init__(self):
        self.connections: Dict[int, List[WebSocket]] = {}

    async def connect(self, telegram_id: int, websocket: WebSocket):
        await websocket.accept()
        self.connections.setdefault(telegram_id, []).append(websocket)

    def disconnect(self, telegram_id: int, websocket: Optional[WebSocket] = None):
        if telegram_id not in self.connections:
            return

        if websocket is None:
            self.connections[telegram_id] = [
                item for item in self.connections[telegram_id] if item.client_state == WebSocketState.CONNECTED
            ]
        else:
            self.connections[telegram_id] = [item for item in self.connections[telegram_id] if item is not websocket]

        if not self.connections[telegram_id]:
            del self.connections[telegram_id]

    async def send_progress(
        self,
        telegram_id: int,
        generation_id: int,
        status: str,
        message: str,
        *,
        progress: int = 0,
        result_url: Optional[str] = None,
    ):
        sockets = self.connections.get(telegram_id)
        if not sockets:
            return

        payload = {
            "generation_id": generation_id,
            "status": status,
            "message": message,
            "progress": max(0, min(progress, 100)),
            "result_url": result_url,
            "timestamp": time.time(),
        }
        disconnected: List[WebSocket] = []

        for websocket in sockets:
            try:
                await websocket.send_json(payload)
            except Exception:
                disconnected.append(websocket)

        for websocket in disconnected:
            self.disconnect(telegram_id, websocket)


manager = ConnectionManager()
