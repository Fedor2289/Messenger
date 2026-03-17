import base64
import json
import logging
import time
from collections import defaultdict
from typing import List, Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session, joinedload

import models, schemas
from auth import create_token, decode_token, hash_password, verify_password
from database import Base, SessionLocal, engine, get_db
from websocket_manager import manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Messenger", version="3.0.0", docs_url="/api/docs")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False,
                   allow_methods=["*"], allow_headers=["*"])

Base.metadata.create_all(bind=engine)

# Добавляем новые колонки в существующие таблицы
import migrations
migrations.run()

app.mount("/static", StaticFiles(directory="static"), name="static")

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 МБ

class RateLimiter:
    def __init__(self, max_calls=20, window=10.0):
        self.max_calls = max_calls
        self.window = window
        self._log: dict = defaultdict(list)
    def allow(self, uid):
        now = time.monotonic()
        self._log[uid] = [t for t in self._log[uid] if now-t < self.window]
        if len(self._log[uid]) >= self.max_calls: return False
        self._log[uid].append(now); return True

_rate = RateLimiter()

# ── Хелперы ──────────────────────────────────────────────────

def require_user(token: str, db: Session) -> models.User:
    uid = decode_token(token)
    if not uid: raise HTTPException(401, "Недействительный токен")
    u = db.get(models.User, uid)
    if not u: raise HTTPException(401, "Пользователь не найден")
    return u

def reactions_grouped(reactions) -> dict:
    """Группируем реакции: {emoji: [user_id, ...]}"""
    g = {}
    for r in reactions:
        g.setdefault(r.emoji, []).append(r.user_id)
    return g

def msg_to_dict(msg: models.Message) -> dict:
    s = msg.sender
    return {
        "id":         msg.id,
        "msg_type":   msg.msg_type,
        "content":    msg.content,
        "media_data": msg.media_data,
        "media_mime": msg.media_mime,
        "media_size": msg.media_size,
        "sender_id":  msg.sender_id,
        "room_id":    msg.room_id,
        "created_at": msg.created_at.isoformat(),
        "is_read":    msg.is_read,
        "sender": {
            "id":           s.id,
            "username":     s.username,
            "display_name": s.display_name,
            "is_online":    manager.is_online(s.id),
            "avatar_color": s.avatar_color,
        } if s else {"id":0,"username":"Удалён","display_name":None,"is_online":False,"avatar_color":"#888"},
        "reactions": reactions_grouped(msg.reactions),
    }

def room_to_dict(room: models.Room, viewer_id: int, db: Session) -> dict:
    members = [{
        "id": m.user.id, "username": m.user.username,
        "display_name": m.user.display_name,
        "is_online": manager.is_online(m.user.id),
        "avatar_color": m.user.avatar_color,
    } for m in room.members]
    last = (db.query(models.Message)
            .options(joinedload(models.Message.sender), joinedload(models.Message.reactions))
            .filter(models.Message.room_id == room.id)
            .order_by(models.Message.created_at.desc()).first())
    unread = db.query(models.Message).filter(
        models.Message.room_id == room.id,
        models.Message.sender_id != viewer_id,
        models.Message.is_read == False).count()
    return {
        "id": room.id, "name": room.name, "is_group": room.is_group,
        "created_by": room.created_by,
        "created_at": room.created_at.isoformat(),
        "members": members,
        "last_message": msg_to_dict(last) if last else None,
        "unread_count": unread,
    }

def find_direct_room(db, uid1, uid2):
    q1 = db.query(models.RoomMember.room_id).filter_by(user_id=uid1).subquery()
    q2 = db.query(models.RoomMember.room_id).filter_by(user_id=uid2).subquery()
    return db.query(models.Room).filter(
        models.Room.is_group==False,
        models.Room.id.in_(q1), models.Room.id.in_(q2)).first()

# ── Системные ────────────────────────────────────────────────

@app.get("/")
async def index(): return FileResponse("static/index.html")

@app.get("/health")
async def health(): return {"status":"ok","online":manager.online_count}

# ── Авторизация ───────────────────────────────────────────────

@app.post("/api/register", response_model=schemas.TokenResponse)
async def register(body: schemas.RegisterRequest, db: Session = Depends(get_db)):
    if db.query(models.User).filter_by(username=body.username).first():
        raise HTTPException(400, "Имя пользователя уже занято")
    if db.query(models.User).filter_by(email=body.email).first():
        raise HTTPException(400, "Email уже зарегистрирован")
    user = models.User(username=body.username, email=body.email,
                       hashed_password=hash_password(body.password))
    db.add(user); db.commit(); db.refresh(user)
    return {"access_token": create_token(user.id), "token_type":"bearer","user":user}

@app.post("/api/login", response_model=schemas.TokenResponse)
async def login(body: schemas.LoginRequest, db: Session = Depends(get_db)):
    u = db.query(models.User).filter_by(username=body.username).first()
    if not u or not verify_password(body.password, u.hashed_password):
        raise HTTPException(401, "Неверное имя или пароль")
    return {"access_token": create_token(u.id), "token_type":"bearer","user":u}

# ── Профиль ───────────────────────────────────────────────────

@app.get("/api/me", response_model=schemas.UserOut)
async def get_me(token: str = Query(...), db: Session = Depends(get_db)):
    return require_user(token, db)

@app.patch("/api/me", response_model=schemas.UserOut)
async def update_me(body: schemas.ProfileUpdate, token: str = Query(...), db: Session = Depends(get_db)):
    u = require_user(token, db)
    if body.display_name is not None: u.display_name = body.display_name or None
    if body.bio          is not None: u.bio = body.bio or None
    if body.avatar_color is not None: u.avatar_color = body.avatar_color
    db.commit(); db.refresh(u)
    # Оповещаем контакты об изменении профиля через WS
    return u

# ── Пользователи ─────────────────────────────────────────────

@app.get("/api/users", response_model=List[schemas.UserShort])
async def get_users(token: str = Query(...), search: str = Query(""),
                    db: Session = Depends(get_db)):
    me = require_user(token, db)
    q = db.query(models.User).filter(models.User.id != me.id)
    if search: q = q.filter(models.User.username.ilike(f"%{search}%"))
    users = q.order_by(models.User.username).limit(50).all()
    for u in users: u.is_online = manager.is_online(u.id)
    return users

# ── Комнаты ───────────────────────────────────────────────────

@app.get("/api/rooms")
async def get_rooms(token: str = Query(...), db: Session = Depends(get_db)):
    me = require_user(token, db)
    ms = db.query(models.RoomMember).filter_by(user_id=me.id).all()
    rooms = [room_to_dict(m.room, me.id, db) for m in ms]
    rooms.sort(key=lambda r: r["last_message"]["created_at"] if r["last_message"] else r["created_at"], reverse=True)
    return rooms

@app.post("/api/rooms/direct")
async def open_direct(body: schemas.DirectRoomRequest, token: str = Query(...), db: Session = Depends(get_db)):
    me = require_user(token, db)
    if body.user_id == me.id: raise HTTPException(400, "Нельзя написать себе")
    other = db.get(models.User, body.user_id)
    if not other: raise HTTPException(404, "Пользователь не найден")
    room = find_direct_room(db, me.id, body.user_id)
    if not room:
        room = models.Room(is_group=False, created_by=me.id)
        db.add(room); db.flush()
        db.add(models.RoomMember(room_id=room.id, user_id=me.id))
        db.add(models.RoomMember(room_id=room.id, user_id=body.user_id))
        db.commit(); db.refresh(room)
    return room_to_dict(room, me.id, db)

@app.post("/api/rooms/group")
async def create_group(body: schemas.GroupRoomRequest, token: str = Query(...), db: Session = Depends(get_db)):
    me = require_user(token, db)
    room = models.Room(name=body.name, is_group=True, created_by=me.id)
    db.add(room); db.flush()
    for uid in list(set([me.id]+body.member_ids)):
        db.add(models.RoomMember(room_id=room.id, user_id=uid))
    db.commit(); db.refresh(room)
    return room_to_dict(room, me.id, db)

@app.delete("/api/rooms/{room_id}")
async def delete_room(room_id: int, token: str = Query(...), db: Session = Depends(get_db)):
    me = require_user(token, db)
    room = db.get(models.Room, room_id)
    if not room: raise HTTPException(404, "Чат не найден")
    if not room.is_group: raise HTTPException(400, "Личные чаты нельзя удалять")
    # Только создатель или участник (если создатель удалён)
    is_member = db.query(models.RoomMember).filter_by(room_id=room_id, user_id=me.id).first()
    if not is_member: raise HTTPException(403, "Нет доступа")
    if room.created_by and room.created_by != me.id:
        raise HTTPException(403, "Только создатель может удалить группу")
    # Уведомляем участников
    member_ids = [m.user_id for m in room.members]
    db.delete(room); db.commit()
    await manager.broadcast(member_ids, {"type":"room_deleted","room_id":room_id})
    return {"ok": True}

@app.get("/api/rooms/{room_id}/members")
async def get_members(room_id: int, token: str = Query(...), db: Session = Depends(get_db)):
    me = require_user(token, db)
    if not db.query(models.RoomMember).filter_by(room_id=room_id, user_id=me.id).first():
        raise HTTPException(403, "Нет доступа")
    return [{"id":m.user.id,"username":m.user.username,"display_name":m.user.display_name,
             "is_online":manager.is_online(m.user.id),"avatar_color":m.user.avatar_color}
            for m in db.query(models.RoomMember).filter_by(room_id=room_id).all()]

# ── Сообщения ─────────────────────────────────────────────────

@app.get("/api/rooms/{room_id}/messages")
async def get_messages(room_id: int, token: str = Query(...),
                       limit: int = Query(50, ge=1, le=100),
                       before_id: int = Query(0, ge=0),
                       db: Session = Depends(get_db)):
    me = require_user(token, db)
    if not db.query(models.RoomMember).filter_by(room_id=room_id, user_id=me.id).first():
        raise HTTPException(403, "Нет доступа")
    q = (db.query(models.Message)
         .options(joinedload(models.Message.sender), joinedload(models.Message.reactions))
         .filter(models.Message.room_id==room_id))
    if before_id: q = q.filter(models.Message.id < before_id)
    msgs = q.order_by(models.Message.created_at.desc()).limit(limit).all()
    msgs.reverse()
    unread = [m.id for m in msgs if m.sender_id != me.id and not m.is_read]
    if unread:
        db.query(models.Message).filter(models.Message.id.in_(unread)).update(
            {"is_read":True}, synchronize_sessi                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             