"""
websocket_manager.py — Менеджер активных WebSocket соединений

Хранит словарь user_id → WebSocket.
Все методы async и thread-safe через asyncio.Lock.
"""

import asyncio
import json
import logging
from typing import Dict, List, Optional

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self):
        self._connections: Dict[int, WebSocket] = {}
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket, user_id: int):
        """Принять новое соединение. Если пользователь уже подключён — закрыть старое."""
        await websocket.accept()
        async with self._lock:
            old = self._connections.get(user_id)
            if old:
                try:
                    await old.close(code=4002)  # 4002 = вытеснено новым соединением
                except Exception:
                    pass
            self._connections[user_id] = websocket
        logger.info(f"WS connect  user={user_id}  online={len(self._connections)}")

    async def disconnect(self, user_id: int):
        async with self._lock:
            self._connections.pop(user_id, None)
        logger.info(f"WS disconnect user={user_id}  online={len(self._connections)}")

    async def send(self, user_id: int, data: dict) -> bool:
        """Отправить сообщение пользователю. False если соединение разорвано."""
        ws = self._connections.get(user_id)
        if not ws:
            return False
        try:
            await ws.send_text(json.dumps(data, ensure_ascii=False))
            return True
        except Exception:
            await self.disconnect(user_id)
            return False

    async def broadcast(self, user_ids: List[int], data: dict):
        """Параллельно отправить всем пользователям в списке."""
        if not user_ids:
            return
        await asyncio.gather(*[self.send(uid, data) for uid in user_ids], return_exceptions=True)

    def is_online(self, user_id: int) -> bool:
        return user_id in self._connections

    @property
    def online_count(self) -> int:
        return len(self._connections)


# Singleton — один экземпляр на всё приложение
manager = ConnectionManager()
