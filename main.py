"""
main.py — FastAPI приложение: все роуты и WebSocket

Структура:
  /           → отдаёт static/index.html
  /health     → healthcheck для Railway
  /api/*      → REST API
  /ws/{token} → WebSocket для реального времени
"""

import json
import logging
import time
from collections import defaultdict
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session, joinedload

import models
import schemas
from auth import create_token, decode_token, hash_password, verify_password
from database import Base, SessionLocal, engine, get_db
from websocket_manager import manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# ── Инициализация ─────────────────────────────────────────────

app = FastAPI(title="Messenger", version="2.0.0", docs_url="/api/docs")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,  # False обязательно при allow_origins=["*"]
    allow_methods=["*"],
    allow_headers=["*"],
)

# Создаём таблицы (при первом запуске)
Base.metadata.create_all(bind=engine)

app.mount("/static", StaticFiles(directory="static"), name="static")


# ── Rate limiter (защита от спама) ───────────────────────────

class RateLimiter:
    """Не более max_calls за window_sec секунд на пользователя"""
    def __init__(self, max_calls: int = 20, window_sec: float = 10.0):
        self.max_calls = max_calls
        self.window = window_sec
        self._log: dict = defaultdict(list)

    def allow(self, user_id: int) -> bool:
        now = time.monotonic()
        history = [t for t in self._log[user_id] if now - t < self.window]
        self._log[user_id] = history
        if len(history) >= self.max_calls:
            return False
        self._log[user_id].append(now)
        return True


_rate = RateLimiter()


# ── Вспомогательные функции ───────────────────────────────────

def require_user(token: str, db: Session) -> models.User:
    """Проверяет JWT и возвращает пользователя. 401 если невалидный."""
    user_id = decode_token(token)
    if not user_id:
        raise HTTPException(401, "Недействительный токен")
    user = db.get(models.User, user_id)
    if not user:
        raise HTTPException(401, "Пользователь не найден")
    return user


def msg_to_dict(msg: models.Message) -> dict:
    """Сериализует сообщение в словарь для JSON."""
    sender = msg.sender
    return {
        "id":         msg.id,
        "content":    msg.content,
        "sender_id":  msg.sender_id,
        "room_id":    msg.room_id,
        "created_at": msg.created_at.isoformat(),
        "is_read":    msg.is_read,
        "sender": {
            "id":           sender.id,
            "username":     sender.username,
            "is_online":    manager.is_online(sender.id),
            "avatar_color": sender.avatar_color,
        } if sender else {"id": 0, "username": "Удалён", "is_online": False, "avatar_color": "#888"},
    }


def room_to_dict(room: models.Room, viewer_id: int, db: Session) -> dict:
    """Сериализует комнату в словарь для сайдбара."""
    members = [
        {
            "id":           m.user.id,
            "username":     m.user.username,
            "is_online":    manager.is_online(m.user.id),
            "avatar_color": m.user.avatar_color,
        }
        for m in room.members
    ]

    # Последнее сообщение — один запрос с joinedload
    last = (
        db.query(models.Message)
        .options(joinedload(models.Message.sender))
        .filter(models.Message.room_id == room.id)
        .order_by(models.Message.created_at.desc())
        .first()
    )

    # Счётчик непрочитанных
    unread = db.query(models.Message).filter(
        models.Message.room_id == room.id,
        models.Message.sender_id != viewer_id,
        models.Message.is_read == False,
    ).count()

    return {
        "id":           room.id,
        "name":         room.name,
        "is_group":     room.is_group,
        "created_at":   room.created_at.isoformat(),
        "members":      members,
        "last_message": msg_to_dict(last) if last else None,
        "unread_count": unread,
    }


def find_direct_room(db: Session, uid1: int, uid2: int) -> Optional[models.Room]:
    """Ищет существующий личный чат между двумя пользователями."""
    q1 = db.query(models.RoomMember.room_id).filter(models.RoomMember.user_id == uid1).subquery()
    q2 = db.query(models.RoomMember.room_id).filter(models.RoomMember.user_id == uid2).subquery()
    return db.query(models.Room).filter(
        models.Room.is_group == False,
        models.Room.id.in_(q1),
        models.Room.id.in_(q2),
    ).first()


# ── Системные роуты ───────────────────────────────────────────

@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.get("/health")
async def health():
    """Railway использует этот endpoint для проверки что сервис живой"""
    return {"status": "ok", "online": manager.online_count}


# ── Авторизация ───────────────────────────────────────────────

@app.post("/api/register", response_model=schemas.TokenResponse)
async def register(body: schemas.RegisterRequest, db: Session = Depends(get_db)):
    if db.query(models.User).filter(models.User.username == body.username).first():
        raise HTTPException(400, "Имя пользователя уже занято")
    if db.query(models.User).filter(models.User.email == body.email).first():
        raise HTTPException(400, "Email уже зарегистрирован")

    user = models.User(
        username=body.username,
        email=body.email,
        hashed_password=hash_password(body.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info(f"New user: {user.username}")
    return {"access_token": create_token(user.id), "token_type": "bearer", "user": user}


@app.post("/api/login", response_model=schemas.TokenResponse)
async def login(body: schemas.LoginRequest, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.username == body.username).first()
    if not user or not verify_password(body.password, user.hashed_password):
        raise HTTPException(401, "Неверное имя или пароль")
    return {"access_token": create_token(user.id), "token_type": "bearer", "user": user}


# ── Пользователи ─────────────────────────────────────────────

@app.get("/api/me", response_model=schemas.UserOut)
async def get_me(token: str = Query(...), db: Session = Depends(get_db)):
    return require_user(token, db)


@app.get("/api/users", response_model=List[schemas.UserShort])
async def get_users(
    token: str = Query(...),
    search: str = Query(""),
    db: Session = Depends(get_db),
):
    me = require_user(token, db)
    q = db.query(models.User).filter(models.User.id != me.id)
    if search:
        q = q.filter(models.User.username.ilike(f"%{search}%"))
    users = q.order_by(models.User.username).limit(50).all()
    for u in users:
        u.is_online = manager.is_online(u.id)
    return users


# ── Чаты (комнаты) ────────────────────────────────────────────

@app.get("/api/rooms")
async def get_rooms(token: str = Query(...), db: Session = Depends(get_db)):
    me = require_user(token, db)
    memberships = db.query(models.RoomMember).filter(
        models.RoomMember.user_id == me.id
    ).all()
    rooms = [room_to_dict(m.room, me.id, db) for m in memberships]
    rooms.sort(
        key=lambda r: r["last_message"]["created_at"] if r["last_message"] else r["created_at"],
        reverse=True,
    )
    return rooms


@app.post("/api/rooms/direct")
async def open_direct(
    body: schemas.DirectRoomRequest,
    token: str = Query(...),
    db: Session = Depends(get_db),
):
    me = require_user(token, db)
    if body.user_id == me.id:
        raise HTTPException(400, "Нельзя написать самому себе")
    other = db.get(models.User, body.user_id)
    if not other:
        raise HTTPException(404, "Пользователь не найден")

    room = find_direct_room(db, me.id, body.user_id)
    if not room:
        room = models.Room(is_group=False)
        db.add(room)
        db.flush()
        db.add(models.RoomMember(room_id=room.id, user_id=me.id))
        db.add(models.RoomMember(room_id=room.id, user_id=body.user_id))
        db.commit()
        db.refresh(room)

    return room_to_dict(room, me.id, db)


@app.post("/api/rooms/group")
async def create_group(
    body: schemas.GroupRoomRequest,
    token: str = Query(...),
    db: Session = Depends(get_db),
):
    me = require_user(token, db)
    room = models.Room(name=body.name, is_group=True)
    db.add(room)
    db.flush()
    member_ids = list(set([me.id] + body.member_ids))
    for uid in member_ids:
        db.add(models.RoomMember(room_id=room.id, user_id=uid))
    db.commit()
    db.refresh(room)
    return room_to_dict(room, me.id, db)


@app.get("/api/rooms/{room_id}/members")
async def get_room_members(
    room_id: int,
    token: str = Query(...),
    db: Session = Depends(get_db),
):
    me = require_user(token, db)
    if not db.query(models.RoomMember).filter_by(room_id=room_id, user_id=me.id).first():
        raise HTTPException(403, "Нет доступа")
    members = db.query(models.RoomMember).filter_by(room_id=room_id).all()
    return [
        {
            "id": m.user.id,
            "username": m.user.username,
            "is_online": manager.is_online(m.user.id),
            "avatar_color": m.user.avatar_color,
        }
        for m in members
    ]


# ── Сообщения ─────────────────────────────────────────────────

@app.get("/api/rooms/{room_id}/messages")
async def get_messages(
    room_id: int,
    token: str = Query(...),
    limit: int = Query(50, ge=1, le=100),
    before_id: int = Query(0, ge=0),   # для пагинации: загрузить сообщения ДО этого ID
    db: Session = Depends(get_db),
):
    me = require_user(token, db)
    if not db.query(models.RoomMember).filter_by(room_id=room_id, user_id=me.id).first():
        raise HTTPException(403, "Нет доступа")

    q = (
        db.query(models.Message)
        .options(joinedload(models.Message.sender))
        .filter(models.Message.room_id == room_id)
    )
    if before_id:
        q = q.filter(models.Message.id < before_id)

    messages = q.order_by(models.Message.created_at.desc()).limit(limit).all()
    messages.reverse()  # показываем в хронологическом порядке

    # Пометить входящие как прочитанные
    unread_ids = [m.id for m in messages if m.sender_id != me.id and not m.is_read]
    if unread_ids:
        db.query(models.Message).filter(models.Message.id.in_(unread_ids)).update(
            {"is_read": True}, synchronize_session=False
        )
        db.commit()

    return {
        "messages": [msg_to_dict(m) for m in messages],
        "has_more": len(messages) == limit,
    }


# ── WebSocket ─────────────────────────────────────────────────

@app.websocket("/ws/{token}")
async def ws_endpoint(websocket: WebSocket, token: str):
    """
    Клиент подключается: wss://host/ws/TOKEN

    Входящие события (client → server):
      {"type": "message",  "room_id": 1,  "content": "Привет!"}
      {"type": "typing",   "room_id": 1,  "is_typing": true}
      {"type": "read",     "room_id": 1}
      {"type": "ping"}

    Исходящие события (server → client):
      {"type": "new_message",  "message": {...}}
      {"type": "user_status",  "user_id": 2, "is_online": true}
      {"type": "typing",       "room_id": 1, "user_id": 2, "username": "X", "is_typing": true}
      {"type": "messages_read","room_id": 1, "reader_id": 2}
      {"type": "error",        "message": "текст ошибки"}
      {"type": "pong"}
    """
    user_id: Optional[int] = None
    db = SessionLocal()

    try:
        user_id = decode_token(token)
        if not user_id:
            await websocket.close(code=4001)
            return

        user = db.get(models.User, user_id)
        if not user:
            await websocket.close(code=4001)
            return

        await manager.connect(websocket, user_id)
        user.is_online = True
        db.commit()
        await _broadcast_status(db, user_id, True)

        # ── Основной цикл ──────────────────────────────────────
        while True:
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                break

            try:
                data = json.loads(raw)
            except (ValueError, json.JSONDecodeError):
                continue

            if not isinstance(data, dict):
                continue

            t = data.get("type", "")

            if t == "message":
                if not _rate.allow(user_id):
                    await manager.send(user_id, {"type": "error", "message": "Слишком быстро, подождите секунду"})
                    continue
                await _on_message(db, user_id, data)

            elif t == "typing":
                await _on_typing(db, user_id, data)

            elif t == "read":
                await _on_read(db, user_id, data)

            elif t == "ping":
                await manager.send(user_id, {"type": "pong"})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"WS error user={user_id}: {e}", exc_info=True)
    finally:
        if user_id is not None:
            await manager.disconnect(user_id)
            try:
                u = db.get(models.User, user_id)
                if u:
                    u.is_online = False
                    db.commit()
                await _broadcast_status(db, user_id, False)
            except Exception as e:
                logger.error(f"WS cleanup error: {e}")
        db.close()


async def _on_message(db: Session, sender_id: int, data: dict):
    content = (data.get("content") or "").strip()[:4096]
    room_id = data.get("room_id")
    if not content or not room_id:
        return

    # Проверяем доступ
    if not db.query(models.RoomMember).filter_by(room_id=room_id, user_id=sender_id).first():
        return

    # Сохраняем сообщение
    msg = models.Message(content=content, sender_id=sender_id, room_id=room_id)
    db.add(msg)
    db.commit()

    # Перечитываем с joinedload
    msg = db.query(models.Message).options(joinedload(models.Message.sender)).get(msg.id)

    # Рассылаем всем участникам комнаты
    member_ids = [m.user_id for m in db.query(models.RoomMember).filter_by(room_id=room_id).all()]
    await manager.broadcast(member_ids, {"type": "new_message", "message": msg_to_dict(msg)})


async def _on_typing(db: Session, user_id: int, data: dict):
    room_id = data.get("room_id")
    if not room_id:
        return
    user = db.get(models.User, user_id)
    if not user:
        return
    others = [
        m.user_id for m in db.query(models.RoomMember).filter(
            models.RoomMember.room_id == room_id,
            models.RoomMember.user_id != user_id,
        ).all()
    ]
    await manager.broadcast(others, {
        "type": "typing",
        "room_id": room_id,
        "user_id": user_id,
        "username": user.username,
        "is_typing": bool(data.get("is_typing", False)),
    })


async def _on_read(db: Session, user_id: int, data: dict):
    """Пометить сообщения в комнате прочитанными и уведомить отправителей."""
    room_id = data.get("room_id")
    if not room_id:
        return
    if not db.query(models.RoomMember).filter_by(room_id=room_id, user_id=user_id).first():
        return

    # Помечаем как прочитанные
    unread = db.query(models.Message).filter(
        models.Message.room_id == room_id,
        models.Message.sender_id != user_id,
        models.Message.is_read == False,
    ).all()

    if not unread:
        return

    sender_ids = set(m.sender_id for m in unread if m.sender_id)
    for m in unread:
        m.is_read = True
    db.commit()

    # Уведомляем отправителей что прочитали
    await manager.broadcast(list(sender_ids), {
        "type": "messages_read",
        "room_id": room_id,
        "reader_id": user_id,
    })


async def _broadcast_status(db: Session, user_id: int, is_online: bool):
    """Уведомить контакты пользователя об изменении статуса."""
    memberships = db.query(models.RoomMember).filter_by(user_id=user_id).all()
    contacts: set = set()
    for m in memberships:
        for member in m.room.members:
            if member.user_id != user_id:
                contacts.add(member.user_id)
    await manager.broadcast(list(contacts), {
        "type": "user_status",
        "user_id": user_id,
        "is_online": is_online,
    })
