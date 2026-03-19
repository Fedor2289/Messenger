"""
websocket_manager.py — Менеджер WebSocket соединений с поддержкой нескольких табов
"""

import asyncio
import json
import logging
from typing import Dict, List, Set

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self):
        # user_id → set of WebSocket connections (несколько табов)
        self._connections: Dict[int, Set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket, user_id: int):
        """Принять новое соединение. Поддерживаем несколько табов одновременно."""
        await websocket.accept()
        async with self._lock:
            if user_id not in self._connections:
                self._connections[user_id] = set()
            self._connections[user_id].add(websocket)
        logger.info(f"WS connect  user={user_id}  online={len(self._connections)}")

    async def disconnect(self, user_id: int, websocket: WebSocket = None):
        async with self._lock:
            if user_id in self._connections:
                if websocket:
                    self._connections[user_id].discard(websocket)
                    if not self._connections[user_id]:
                        del self._connections[user_id]
                else:
                    del self._connections[user_id]
        logger.info(f"WS disconnect user={user_id}  online={len(self._connections)}")

    async def send(self, user_id: int, data: dict) -> bool:
        """Отправить сообщение всем вкладкам пользователя."""
        sockets = self._connections.get(user_id, set()).copy()
        if not sockets:
            return False
        text = json.dumps(data, ensure_ascii=False)
        dead = []
        for ws in sockets:
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        # Чистим мёртвые соединения
        if dead:
            async with self._lock:
                for ws in dead:
                    self._connections.get(user_id, set()).discard(ws)
                if user_id in self._connections and not self._connections[user_id]:
                    del self._connections[user_id]
        return len(sockets) > len(dead)

    async def broadcast(self, user_ids: List[int], data: dict):
        """Параллельно отправить всем пользователям."""
        if not user_ids:
            return
        await asyncio.gather(*[self.send(uid, data) for uid in user_ids], return_exceptions=True)

    def is_online(self, user_id: int) -> bool:
        return bool(self._connections.get(user_id))

    @property
    def online_count(self) -> int:
        return len(self._connections)


manager = ConnectionManager()
