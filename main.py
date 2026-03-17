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
            "avatar_img":   getattr(s, "avatar_img", None),
            "is_online":    manager.is_online(s.id),
            "avatar_color": s.avatar_color,
        } if s else {"id":0,"username":"Удалён","display_name":None,"avatar_img":None,"is_online":False,"avatar_color":"#888"},
        "reactions": reactions_grouped(msg.reactions),
    }

def room_to_dict(room: models.Room, viewer_id: int, db: Session) -> dict:
    members = [{
        "id": m.user.id, "username": m.user.username,
        "display_name": m.user.display_name,
        "avatar_img": getattr(m.user, "avatar_img", None),
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
    if body.avatar_img   is not None:
        # Проверяем размер base64 (примерно 2 МБ = ~2.7 МБ base64)
        if len(body.avatar_img) > 3_000_000:
            raise HTTPException(400, "Фото слишком большое (макс. 2 МБ)")
        u.avatar_img = body.avatar_img
    elif body.avatar_img == "":
        u.avatar_img = None  # пустая строка = удалить фото
    db.commit(); db.refresh(u)
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
             "avatar_img":getattr(m.user, "avatar_img", None),
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
            {"is_read":True}, synchronize_session=False)
        db.commit()
    return {"messages":[msg_to_dict(m) for m in msgs], "has_more": len(msgs)==limit}

# ── Загрузка медиа ────────────────────────────────────────────

@app.post("/api/rooms/{room_id}/upload")
async def upload_media(room_id: int, token: str = Query(...),
                       file: UploadFile = File(...),
                       db: Session = Depends(get_db)):
    """Загрузка файла/изображения/голосового сообщения"""
    me = require_user(token, db)
    if not db.query(models.RoomMember).filter_by(room_id=room_id, user_id=me.id).first():
        raise HTTPException(403, "Нет доступа")

    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, f"Файл слишком большой (макс. 10 МБ)")

    mime = file.content_type or "application/octet-stream"
    b64  = base64.b64encode(raw).decode()

    # Определяем тип сообщения по MIME
    if mime.startswith("image/"):
        msg_type = "gif" if mime == "image/gif" else "image"
    elif mime.startswith("audio/"):
        msg_type = "voice"
    else:
        msg_type = "file"

    content = file.filename or "file"
    msg = models.Message(msg_type=msg_type, content=content,
                         media_data=b64, media_mime=mime, media_size=len(raw),
                         sender_id=me.id, room_id=room_id)
    db.add(msg); db.commit()
    msg = (db.query(models.Message)
           .options(joinedload(models.Message.sender), joinedload(models.Message.reactions))
           .get(msg.id))

    data = msg_to_dict(msg)
    member_ids = [m.user_id for m in db.query(models.RoomMember).filter_by(room_id=room_id).all()]
    await manager.broadcast(member_ids, {"type":"new_message","message":data})
    return data

# ── Реакции ───────────────────────────────────────────────────

@app.post("/api/messages/{msg_id}/react")
async def react(msg_id: int, token: str = Query(...), emoji: str = Query(...),
                db: Session = Depends(get_db)):
    me = require_user(token, db)
    msg = db.get(models.Message, msg_id)
    if not msg: raise HTTPException(404, "Сообщение не найдено")
    if not db.query(models.RoomMember).filter_by(room_id=msg.room_id, user_id=me.id).first():
        raise HTTPException(403, "Нет доступа")

    existing = db.query(models.MessageReaction).filter_by(
        message_id=msg_id, user_id=me.id, emoji=emoji).first()
    if existing:
        db.delete(existing)  # toggle — убираем если уже есть
    else:
        db.add(models.MessageReaction(message_id=msg_id, user_id=me.id, emoji=emoji))
    db.commit()

    # Перечитываем реакции
    db.refresh(msg)
    grouped = reactions_grouped(msg.reactions)
    member_ids = [m.user_id for m in db.query(models.RoomMember).filter_by(room_id=msg.room_id).all()]
    await manager.broadcast(member_ids, {
        "type": "reaction_update",
        "message_id": msg_id,
        "room_id": msg.room_id,
        "reactions": grouped,
    })
    return grouped

# ── WebSocket ─────────────────────────────────────────────────

@app.websocket("/ws/{token}")
async def ws_endpoint(websocket: WebSocket, token: str):
    user_id: Optional[int] = None
    db = SessionLocal()
    try:
        user_id = decode_token(token)
        if not user_id: await websocket.close(code=4001); return
        user = db.get(models.User, user_id)
        if not user: await websocket.close(code=4001); return

        await manager.connect(websocket, user_id)
        user.is_online = True; db.commit()
        await _broadcast_status(db, user_id, True)

        while True:
            try: raw = await websocket.receive_text()
            except WebSocketDisconnect: break
            try: data = json.loads(raw)
            except: continue
            if not isinstance(data, dict): continue
            t = data.get("type","")
            if t == "message":
                if not _rate.allow(user_id):
                    await manager.send(user_id,{"type":"error","message":"Слишком быстро"}); continue
                await _on_message(db, user_id, data)
            elif t == "typing": await _on_typing(db, user_id, data)
            elif t == "read":   await _on_read(db, user_id, data)
            elif t == "ping":   await manager.send(user_id,{"type":"pong"})
    except WebSocketDisconnect: pass
    except Exception as e: logger.error(f"WS error uid={user_id}: {e}", exc_info=True)
    finally:
        if user_id is not None:
            await manager.disconnect(user_id)
            try:
                u = db.get(models.User, user_id)
                if u: u.is_online = False; db.commit()
                await _broadcast_status(db, user_id, False)
            except Exception as e: logger.error(f"WS cleanup: {e}")
        db.close()

async def _on_message(db, sender_id, data):
    content = (data.get("content") or "").strip()[:4096]
    room_id = data.get("room_id")
    msg_type = data.get("type", "message")
    # Стикеры — особый тип
    if msg_type == "sticker":
        actual_type = "sticker"
    else:
        actual_type = "text"
    if not content or not room_id: return
    if not db.query(models.RoomMember).filter_by(room_id=room_id, user_id=sender_id).first(): return
    msg = models.Message(msg_type=actual_type, content=content, sender_id=sender_id, room_id=room_id)
    db.add(msg); db.commit()
    msg = (db.query(models.Message)
           .options(joinedload(models.Message.sender), joinedload(models.Message.reactions))
           .get(msg.id))
    ids = [m.user_id for m in db.query(models.RoomMember).filter_by(room_id=room_id).all()]
    await manager.broadcast(ids, {"type":"new_message","message":msg_to_dict(msg)})

async def _on_typing(db, user_id, data):
    room_id = data.get("room_id")
    if not room_id: return
    u = db.get(models.User, user_id)
    if not u: return
    others = [m.user_id for m in db.query(models.RoomMember).filter(
        models.RoomMember.room_id==room_id, models.RoomMember.user_id!=user_id).all()]
    await manager.broadcast(others,{"type":"typing","room_id":room_id,
        "user_id":user_id,"username":u.username,"is_typing":bool(data.get("is_typing"))})

async def _on_read(db, user_id, data):
    room_id = data.get("room_id")
    if not room_id: return
    if not db.query(models.RoomMember).filter_by(room_id=room_id, user_id=user_id).first(): return
    unread = db.query(models.Message).filter(
        models.Message.room_id==room_id, models.Message.sender_id!=user_id,
        models.Message.is_read==False).all()
    if not unread: return
    senders = set(m.sender_id for m in unread if m.sender_id)
    for m in unread: m.is_read = True
    db.commit()
    await manager.broadcast(list(senders),{"type":"messages_read","room_id":room_id,"reader_id":user_id})

async def _broadcast_status(db, user_id, is_online):
    ms = db.query(models.RoomMember).filter_by(user_id=user_id).all()
    contacts = set()
    for m in ms:
        for mm in m.room.members:
            if mm.user_id != user_id: contacts.add(mm.user_id)
    await manager.broadcast(list(contacts),{"type":"user_status","user_id":user_id,"is_online":is_online})
