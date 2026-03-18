"""
main.py — Полноценный мессенджер
Возможности: чаты, группы, каналы, ИИ-чат, звонки (WebRTC сигналинг),
удаление/редактирование сообщений, реакции, ответы, пин, GIF-поиск
"""
import base64, httpx, json, logging, os, time
from collections import defaultdict
from datetime import datetime
from typing import List, Optional
from pywebpush import webpush, WebPushException

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import text as sql_text

import models, schemas
from auth import create_token, decode_token, hash_password, verify_password
from database import Base, SessionLocal, engine, get_db
from websocket_manager import manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# ── Env ──────────────────────────────────────────────────────
GROQ_API_KEY    = os.getenv("GROQ_API_KEY", "")
CEREBRAS_KEY    = os.getenv("CEREBRAS_API_KEY", "")
GIPHY_API_KEY   = os.getenv("GIPHY_API_KEY", "")
AI_BACKEND      = os.getenv("AI_BACKEND", "auto")
MAX_UPLOAD      = 200 * 1024 * 1024  # 200 МБ
YADISK_TOKEN    = os.getenv("YADISK_TOKEN", "")
YADISK_FOLDER   = os.getenv("YADISK_FOLDER", "messenger")

# ── VAPID Push ────────────────────────────────────────────────
VAPID_PRIVATE   = os.getenv("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC    = os.getenv("VAPID_PUBLIC_KEY", "")
VAPID_EMAIL     = os.getenv("VAPID_EMAIL", "mailto:admin@messenger.app")

def _ensure_vapid():
    global VAPID_PRIVATE, VAPID_PUBLIC
    # Если ключи уже в env — используем их
    if VAPID_PRIVATE and VAPID_PUBLIC:
        return
    # Пробуем загрузить из файла (персистентное хранение между рестартами)
    _key_file = "/tmp/vapid_keys.json"
    try:
        import json as _json
        with open(_key_file) as f:
            keys = _json.load(f)
            if keys.get("private") and keys.get("public"):
                VAPID_PRIVATE = keys["private"]
                VAPID_PUBLIC  = keys["public"]
                logger.info(f"VAPID keys loaded from cache. Public: {VAPID_PUBLIC[:20]}...")
                return
    except Exception:
        pass
    # Генерируем новые ключи
    try:
        from py_vapid import Vapid
        from cryptography.hazmat.primitives.serialization import (
            Encoding, PrivateFormat, PublicFormat, NoEncryption
        )
        import base64 as _b64, json as _json
        v = Vapid()
        v.generate_keys()
        VAPID_PRIVATE = v.private_key.private_bytes(
            encoding=Encoding.PEM,
            format=PrivateFormat.PKCS8,
            encryption_algorithm=NoEncryption()
        ).decode()
        raw_pub = v.public_key.public_bytes(
            encoding=Encoding.X962,
            format=PublicFormat.UncompressedPoint
        )
        VAPID_PUBLIC = _b64.urlsafe_b64encode(raw_pub).rstrip(b'=').decode()
        # Сохраняем в файл чтобы пережить перезапуск в рамках одного деплоя
        try:
            with open(_key_file, "w") as f:
                _json.dump({"private": VAPID_PRIVATE, "public": VAPID_PUBLIC}, f)
        except Exception:
            pass
        logger.info(
            f"VAPID keys generated. Добавь в Railway Variables:\n"
            f"  VAPID_PUBLIC_KEY={VAPID_PUBLIC}\n"
            f"  VAPID_PRIVATE_KEY={VAPID_PRIVATE.strip()}"
        )
    except Exception as e:
        logger.warning(f"VAPID setup failed: {e}. Push notifications disabled.")


app = FastAPI(title="Messenger", version="4.0.0", docs_url="/api/docs")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_credentials=False, allow_methods=["*"], allow_headers=["*"])

# НЕ вызываем create_all/migrations при импорте — делаем в startup хуке
# чтобы uvicorn успел поднять HTTP до обращения к БД

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/sw.js")
async def sw(): return FileResponse("static/sw.js", media_type="application/javascript")

@app.on_event("startup")
async def on_startup():
    import asyncio, logging
    _log = logging.getLogger(__name__)
    # Запускаем миграции в фоне — НЕ ждём, сервер стартует мгновенно
    asyncio.get_event_loop().run_in_executor(None, _do_startup)
    _log.info("Server ready, migrations running in background")

def _do_startup():
    import logging, time
    _log = logging.getLogger(__name__)
    time.sleep(1)  # даём uvicorn секунду полностью подняться
    try:
        Base.metadata.create_all(bind=engine)
        _log.info("DB tables OK")
    except Exception as e:
        _log.error(f"create_all failed: {e}")
    try:
        import migrations; migrations.run()
    except Exception as e:
        _log.error(f"Migrations failed: {e}")
    try:
        _ensure_vapid()
    except Exception as e:
        _log.error(f"VAPID init failed: {e}")

# ── Yandex Disk ───────────────────────────────────────────
import uuid as _uuid

async def yadisk_upload(raw: bytes, filename: str, mime: str) -> str:
    """
    Загружает файл на Яндекс Диск и возвращает публичный URL.
    Если токена нет — возвращает пустую строку (fallback на base64).
    """
    if not YADISK_TOKEN:
        return ""
    
    # Генерируем уникальное имя чтобы избежать коллизий
    ext = filename.rsplit(".", 1)[-1] if "." in filename else "bin"
    unique_name = f"{_uuid.uuid4().hex}.{ext}"
    disk_path = f"/{YADISK_FOLDER}/{unique_name}"
    
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            headers = {"Authorization": f"OAuth {YADISK_TOKEN}"}
            
            # Создаём папку если нет
            await c.put(
                "https://cloud-api.yandex.net/v1/disk/resources",
                headers=headers,
                params={"path": f"/{YADISK_FOLDER}"}
            )  # игнорируем ошибку если папка уже есть
            
            # 1. Получаем URL для загрузки
            r = await c.get(
                "https://cloud-api.yandex.net/v1/disk/resources/upload",
                headers=headers,
                params={"path": disk_path, "overwrite": "true"}
            )
            if r.status_code != 200:
                logger.error(f"YaDisk upload link error: {r.status_code} {r.text}")
                return ""
            upload_url = r.json()["href"]
            
            # 2. Загружаем файл
            up = await c.put(upload_url, content=raw, headers={"Content-Type": mime})
            if up.status_code not in (200, 201):
                logger.error(f"YaDisk upload error: {up.status_code}")
                return ""
            
            # 3. Публикуем файл
            pub = await c.put(
                "https://cloud-api.yandex.net/v1/disk/resources/publish",
                headers=headers,
                params={"path": disk_path}
            )
            
            # 4. Получаем public_key для скачивания (retry — иногда не сразу появляется)
            public_key = ""
            for attempt in range(4):
                info = await c.get(
                    "https://cloud-api.yandex.net/v1/disk/resources",
                    headers=headers,
                    params={"path": disk_path}
                )
                if info.status_code != 200:
                    break
                data = info.json()
                public_key = data.get("public_key", "")
                if public_key:
                    break
                import asyncio
                await asyncio.sleep(0.8)  # ждём пока Яндекс обработает публикацию
            
            if not public_key:
                logger.error(f"YaDisk: public_key not available for {disk_path}")
                return ""
            
            return f"yadisk:{public_key}"
    except Exception as e:
        logger.error(f"YaDisk upload exception: {e}")
        return ""

async def yadisk_get_download_url(public_key: str) -> str:
    """Получает прямую ссылку для скачивания с Яндекс Диска"""
    if not YADISK_TOKEN:
        return ""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                "https://cloud-api.yandex.net/v1/disk/public/resources/download",
                headers={"Authorization": f"OAuth {YADISK_TOKEN}"},
                params={"public_key": public_key}
            )
            if r.status_code == 200:
                return r.json().get("href", "")
    except Exception as e:
        logger.error(f"YaDisk get download URL: {e}")
    return ""


# ── Rate limiter ─────────────────────────────────────────────
class RL:
    def __init__(self, n=20, w=10.0):
        self.n=n; self.w=w; self._l=defaultdict(list)
    def ok(self, uid):
        now=time.monotonic()
        self._l[uid]=[t for t in self._l[uid] if now-t<self.w]
        if len(self._l[uid])>=self.n: return False
        self._l[uid].append(now); return True
rl=RL()


ADMIN_USERNAMES = set(x.strip() for x in os.getenv("ADMIN_USERNAMES","admin").split(",") if x.strip())

def is_admin_user(user: models.User) -> bool:
    return user.username in ADMIN_USERNAMES

# ── Helpers ───────────────────────────────────────────────────
def auth(token, db):
    uid=decode_token(token)
    if not uid: raise HTTPException(401,"Недействительный токен")
    u=db.get(models.User, uid)
    if not u: raise HTTPException(401,"Пользователь не найден")
    return u

def rxn_grouped(rxns):
    g={}
    for r in rxns: g.setdefault(r.emoji,[]).append(r.user_id)
    return g

def msg_dict(msg):
    if not msg: return None
    s=msg.sender
    rt=None
    if msg.reply_to and not msg.reply_to.is_deleted:
        rs=msg.reply_to.sender
        rt={"id":msg.reply_to.id,"content":msg.reply_to.content[:80],
            "msg_type":msg.reply_to.msg_type,
            "sender":{"id":rs.id,"username":rs.username,
                      "display_name":getattr(rs,"display_name",None),
                      "avatar_img":getattr(rs,"avatar_img",None),
                      "avatar_color":rs.avatar_color or "#5288c1","is_online":manager.is_online(rs.id)} if rs else None}
    return {
        "id":msg.id,"msg_type":msg.msg_type,
        "content":"[Сообщение удалено]" if msg.is_deleted else msg.content,
        "media_data":None if msg.is_deleted else msg.media_data,
        "media_mime":msg.media_mime,"media_size":msg.media_size,
        "sender_id":msg.sender_id,"room_id":msg.room_id,
        "created_at":msg.created_at.isoformat(),
        "is_read":msg.is_read,"is_deleted":msg.is_deleted,
        "is_pinned":msg.is_pinned,
        "edited_at":msg.edited_at.isoformat() if msg.edited_at else None,
        "reply_to":rt,
        "reactions":rxn_grouped(msg.reactions),
        "sender":{
            "id":s.id,"username":s.username,
            "display_name":getattr(s,"display_name",None),
            "avatar_img":getattr(s,"avatar_img",None),
            "avatar_color":s.avatar_color or "#5288c1","is_online":manager.is_online(s.id),
            "last_seen":s.last_seen.isoformat() if getattr(s,"last_seen",None) else None,
        } if s else {"id":0,"username":"Удалён","display_name":None,"avatar_img":None,
                     "avatar_color":"#888","is_online":False,"last_seen":None},
    }

def room_dict(room, viewer_id, db):
    members=[{
        "id":m.user.id,"username":m.user.username,
        "display_name":getattr(m.user,"display_name",None),
        "avatar_img":getattr(m.user,"avatar_img",None),
        "avatar_color":m.user.avatar_color or "#5288c1",
        "is_online":manager.is_online(m.user.id),
        "is_admin":m.is_admin,
        "last_seen":m.user.last_seen.isoformat() if getattr(m.user,"last_seen",None) else None,
    } for m in room.members]
    last=(db.query(models.Message)
          .options(joinedload(models.Message.sender),
                   joinedload(models.Message.reactions),
                   joinedload(models.Message.reply_to))
          .filter_by(room_id=room.id)
          .order_by(models.Message.created_at.desc()).first())
    unread=db.query(models.Message).filter(
        models.Message.room_id==room.id,
        models.Message.sender_id!=viewer_id,
        models.Message.is_read==False,
        models.Message.is_deleted==False).count()
    return {
        "id":room.id,"name":room.name,
        "room_type":getattr(room,"room_type","chat"),
        "description":getattr(room,"description",None),
        "created_by":getattr(room,"created_by",None),
        "created_at":room.created_at.isoformat(),
        "members":members,"last_message":msg_dict(last),
        "unread_count":unread,
        "pinned_msg_id":getattr(room,"pinned_msg_id",None),
    }

def find_direct(db,u1,u2):
    q1=db.query(models.RoomMember.room_id).filter_by(user_id=u1).subquery()
    q2=db.query(models.RoomMember.room_id).filter_by(user_id=u2).subquery()
    return db.query(models.Room).filter(
        models.Room.room_type=='chat',
        models.Room.id.in_(q1),models.Room.id.in_(q2)).first()

def load_msg(db, msg_id):
    return (db.query(models.Message)
            .options(joinedload(models.Message.sender),
                     joinedload(models.Message.reactions),
                     joinedload(models.Message.reply_to))
            .get(msg_id))

# ── Routes ───────────────────────────────────────────────────
@app.get("/")
async def index(): return FileResponse("static/index.html")

@app.get("/health")
async def health(): return {"status":"ok","online":manager.online_count}

# ── Push Notifications ────────────────────────────────────────
@app.get("/api/push/vapid-key")
async def vapid_key():
    return {"publicKey": VAPID_PUBLIC or ""}

@app.post("/api/push/subscribe")
async def push_subscribe(body:dict, token:str=Query(...), db:Session=Depends(get_db)):
    me=auth(token,db)
    sub_json=json.dumps(body)
    endpoint=body.get("endpoint","")
    if not endpoint:
        raise HTTPException(400,"Нет endpoint в подписке")
    db.execute(
        sql_text(
            "INSERT INTO push_subscriptions(user_id,endpoint,subscription) VALUES(:uid,:ep,:sub) "
            "ON CONFLICT(user_id,endpoint) DO UPDATE SET subscription=:sub"
        ),
        {"uid":me.id,"ep":endpoint,"sub":sub_json}
    )
    db.commit()
    return {"ok":True}

@app.delete("/api/push/unsubscribe")
async def push_unsubscribe(token:str=Query(...), endpoint:str=Query(...), db:Session=Depends(get_db)):
    me=auth(token,db)
    db.execute(
        sql_text("DELETE FROM push_subscriptions WHERE user_id=:uid AND endpoint=:ep"),
        {"uid":me.id,"ep":endpoint}
    )
    db.commit()
    return {"ok":True}

async def send_push_to_user(db, user_id:int, title:str, body:str, room_id:int=None, tag:str="msg"):
    if not VAPID_PRIVATE or not VAPID_PUBLIC:
        logger.warning("Push: VAPID keys not set, skipping")
        return
    try:
        rows=db.execute(
            sql_text("SELECT subscription FROM push_subscriptions WHERE user_id=:uid"),
            {"uid":user_id}
        ).fetchall()
    except Exception as e:
        logger.error(f"Push: DB error reading subscriptions: {e}")
        return
    if not rows:
        return
    payload=json.dumps({"title":title,"body":body,"room_id":room_id,"tag":tag})
    # Нормализуем приватный ключ — vapidkeys.com даёт raw base64url, pywebpush хочет его же
    priv_key = VAPID_PRIVATE.strip()
    # Если это PEM — оставляем как есть, если raw base64url — тоже оставляем
    for row in rows:
        try:
            sub=json.loads(row[0])
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=priv_key,
                vapid_claims={"sub": VAPID_EMAIL}
            )
            logger.info(f"Push sent to user {user_id}: {title}")
        except WebPushException as e:
            logger.error(f"Push WebPushException user={user_id}: {e}, response={e.response.text if e.response else 'no response'}")
            if e.response and e.response.status_code in (404, 410):
                try:
                    db.execute(
                        sql_text("DELETE FROM push_subscriptions WHERE user_id=:uid AND subscription=:sub"),
                        {"uid":user_id,"sub":row[0]}
                    )
                    db.commit()
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Push error user={user_id}: {type(e).__name__}: {e}")

# ── Ringtone Upload ───────────────────────────────────────────
@app.post("/api/upload/ringtone")
async def upload_ringtone(token:str=Query(...), file:UploadFile=File(...), db:Session=Depends(get_db)):
    me=auth(token,db)
    raw=await file.read()
    if len(raw)>20*1024*1024: raise HTTPException(400,"Файл слишком большой (макс. 20 МБ)")
    mime=file.content_type or "audio/mpeg"
    if not mime.startswith("audio/"):
        raise HTTPException(400,"Только аудиофайлы")
    fname=file.filename or "ringtone.mp3"
    yadisk_uri=await yadisk_upload(raw, fname, mime)
    if not yadisk_uri: raise HTTPException(503,"Не удалось загрузить на Яндекс.Диск")
    # Сохраняем как сообщение в AI-комнате пользователя (или создаём её)
    ai_room=db.query(models.RoomMember).join(models.Room).filter(
        models.RoomMember.user_id==me.id, models.Room.room_type=="ai").first()
    if not ai_room:
        room=models.Room(name="ИИ Ассистент",room_type="ai",created_by=me.id)
        db.add(room); db.flush()
        db.add(models.RoomMember(room_id=room.id,user_id=me.id,is_admin=True))
        db.commit(); db.refresh(room)
        target_room_id=room.id
    else:
        target_room_id=ai_room.room_id
    msg=models.Message(msg_type="ringtone",content=fname,media_data=yadisk_uri,
                       media_mime=mime,media_size=len(raw),sender_id=me.id,
                       room_id=target_room_id,reply_to_id=None)
    db.add(msg); db.commit(); db.refresh(msg)
    return {"media_id": msg.id, "url": f"/api/media/{msg.id}"}

# Auth
@app.post("/api/register", response_model=schemas.TokenResponse)
async def register(body:schemas.RegisterRequest, db:Session=Depends(get_db)):
    if db.query(models.User).filter_by(username=body.username).first():
        raise HTTPException(400,"Имя пользователя уже занято")
    if db.query(models.User).filter_by(email=body.email).first():
        raise HTTPException(400,"Email уже зарегистрирован")
    from models import _random_color
    u=models.User(username=body.username,email=body.email,
                  hashed_password=hash_password(body.password),
                  avatar_color=_random_color())
    db.add(u); db.commit(); db.refresh(u)
    if not u.avatar_color:
        u.avatar_color = _random_color(); db.commit(); db.refresh(u)
    return {"access_token":create_token(u.id),"token_type":"bearer","user":u}

@app.post("/api/login", response_model=schemas.TokenResponse)
async def login(body:schemas.LoginRequest, db:Session=Depends(get_db)):
    u=db.query(models.User).filter_by(username=body.username).first()
    if not u or not verify_password(body.password,u.hashed_password):
        raise HTTPException(401,"Неверное имя или пароль")
    if not u.avatar_color:
        from models import _random_color
        u.avatar_color = _random_color(); db.commit(); db.refresh(u)
    return {"access_token":create_token(u.id),"token_type":"bearer","user":u}

# Profile
@app.get("/api/me", response_model=schemas.UserOut)
async def me(token:str=Query(...), db:Session=Depends(get_db)):
    return auth(token,db)

@app.patch("/api/me", response_model=schemas.UserOut)
async def update_me(body:schemas.ProfileUpdate, token:str=Query(...), db:Session=Depends(get_db)):
    u=auth(token,db)
    if body.display_name is not None: u.display_name=body.display_name or None
    if body.bio is not None: u.bio=body.bio or None
    if body.avatar_color is not None: u.avatar_color=body.avatar_color
    if body.avatar_img is not None:
        if body.avatar_img and len(body.avatar_img)>500_000:
            raise HTTPException(400,"Фото слишком большое")
        u.avatar_img=body.avatar_img or None
    db.commit(); db.refresh(u); return u

# Users
@app.get("/api/users", response_model=List[schemas.UserShort])
async def users(token:str=Query(...), search:str=Query(""), db:Session=Depends(get_db)):
    me=auth(token,db)
    q=db.query(models.User).filter(models.User.id!=me.id)
    if search: q=q.filter(models.User.username.ilike(f"%{search}%"))
    us=q.order_by(models.User.username).limit(50).all()
    for u in us: u.is_online=manager.is_online(u.id)
    return us

# Rooms
@app.get("/api/rooms")
async def rooms(token:str=Query(...), db:Session=Depends(get_db)):
    me=auth(token,db)
    ms=db.query(models.RoomMember).filter_by(user_id=me.id).all()
    rs=[room_dict(m.room,me.id,db) for m in ms]
    rs.sort(key=lambda r: r["last_message"]["created_at"] if r["last_message"] else r["created_at"],reverse=True)
    return rs

@app.post("/api/rooms/direct")
async def direct(body:schemas.DirectRoomRequest, token:str=Query(...), db:Session=Depends(get_db)):
    me=auth(token,db)
    if body.user_id==me.id: raise HTTPException(400,"Нельзя написать себе")
    other=db.get(models.User,body.user_id)
    if not other: raise HTTPException(404,"Пользователь не найден")
    room=find_direct(db,me.id,body.user_id)
    if not room:
        room=models.Room(room_type='chat',created_by=me.id)
        db.add(room); db.flush()
        db.add(models.RoomMember(room_id=room.id,user_id=me.id,is_admin=True))
        db.add(models.RoomMember(room_id=room.id,user_id=body.user_id))
        db.commit(); db.refresh(room)
    return room_dict(room,me.id,db)

@app.post("/api/rooms/group")
async def group(body:schemas.GroupRoomRequest, token:str=Query(...), db:Session=Depends(get_db)):
    me=auth(token,db)
    room=models.Room(name=body.name,room_type='group',created_by=me.id)
    db.add(room); db.flush()
    for uid in list(set([me.id]+body.member_ids)):
        db.add(models.RoomMember(room_id=room.id,user_id=uid,is_admin=(uid==me.id)))
    db.commit(); db.refresh(room)
    return room_dict(room,me.id,db)

@app.post("/api/rooms/channel")
async def channel(body:schemas.ChannelRequest, token:str=Query(...), db:Session=Depends(get_db)):
    me=auth(token,db)
    room=models.Room(name=body.name,room_type='channel',
                     description=body.description,created_by=me.id)
    db.add(room); db.flush()
    db.add(models.RoomMember(room_id=room.id,user_id=me.id,is_admin=True))
    db.commit(); db.refresh(room)
    return room_dict(room,me.id,db)

@app.post("/api/rooms/{rid}/join")
async def join_channel(rid:int, token:str=Query(...), db:Session=Depends(get_db)):
    me=auth(token,db)
    room=db.get(models.Room,rid)
    if not room: raise HTTPException(404,"Не найден")
    if getattr(room,"room_type","chat")!="channel": raise HTTPException(400,"Не канал")
    if db.query(models.RoomMember).filter_by(room_id=rid,user_id=me.id).first():
        return room_dict(room,me.id,db)
    db.add(models.RoomMember(room_id=rid,user_id=me.id,is_admin=False))
    db.commit(); db.refresh(room)
    return room_dict(room,me.id,db)

@app.delete("/api/rooms/{rid}")
async def del_room(rid:int, token:str=Query(...), db:Session=Depends(get_db)):
    me=auth(token,db)
    room=db.get(models.Room,rid)
    if not room: raise HTTPException(404,"Не найден")
    rt=getattr(room,"room_type","chat")
    if rt=="chat": raise HTTPException(400,"Личные чаты нельзя удалять")
    if room.created_by and room.created_by!=me.id: raise HTTPException(403,"Только создатель")
    ids=[m.user_id for m in room.members]
    # Удаляем участников вручную сначала — CASCADE в Python ORM
    db.query(models.RoomMember).filter_by(room_id=rid).delete(synchronize_session=False)
    # Удаляем сообщения (и реакции через CASCADE в БД)
    db.query(models.Message).filter_by(room_id=rid).delete(synchronize_session=False)
    db.expire_all()  # сбрасываем кэш сессии чтобы избежать StaleDataError
    db.delete(room); db.commit()
    await manager.broadcast(ids,{"type":"room_deleted","room_id":rid})
    return {"ok":True}

@app.get("/api/rooms/{rid}/members")
async def mbrs(rid:int, token:str=Query(...), db:Session=Depends(get_db)):
    me=auth(token,db)
    if not db.query(models.RoomMember).filter_by(room_id=rid,user_id=me.id).first():
        raise HTTPException(403,"Нет доступа")
    ms=db.query(models.RoomMember).filter_by(room_id=rid).all()
    return [{"id":m.user.id,"username":m.user.username,
             "display_name":getattr(m.user,"display_name",None),
             "avatar_img":getattr(m.user,"avatar_img",None),
             "avatar_color":m.user.avatar_color or "#5288c1",
             "is_online":manager.is_online(m.user.id),
             "is_admin":m.is_admin,
             "last_seen":m.user.last_seen.isoformat() if getattr(m.user,"last_seen",None) else None}
            for m in ms]

# Messages
@app.get("/api/rooms/{rid}/messages")
async def msgs(rid:int, token:str=Query(...),
               limit:int=Query(50,ge=1,le=100), before_id:int=Query(0,ge=0),
               db:Session=Depends(get_db)):
    me=auth(token,db)
    if not db.query(models.RoomMember).filter_by(room_id=rid,user_id=me.id).first():
        raise HTTPException(403,"Нет доступа")
    q=(db.query(models.Message)
       .options(joinedload(models.Message.sender),
                joinedload(models.Message.reactions),
                joinedload(models.Message.reply_to))
       .filter_by(room_id=rid))
    if before_id: q=q.filter(models.Message.id<before_id)
    ms=q.order_by(models.Message.created_at.desc()).limit(limit).all()
    ms.reverse()
    unread=[m.id for m in ms if m.sender_id!=me.id and not m.is_read and not m.is_deleted]
    if unread:
        db.query(models.Message).filter(models.Message.id.in_(unread)).update(
            {"is_read":True},synchronize_session=False); db.commit()
    return {"messages":[msg_dict(m) for m in ms],"has_more":len(ms)==limit}

@app.delete("/api/messages/{mid}")
async def del_msg(mid:int, token:str=Query(...), db:Session=Depends(get_db)):
    me=auth(token,db)
    msg=db.get(models.Message,mid)
    if not msg: raise HTTPException(404,"Не найдено")
    room_id=msg.room_id
    if msg.sender_id!=me.id:
        mb=db.query(models.RoomMember).filter_by(room_id=room_id,user_id=me.id).first()
        if not mb or not mb.is_admin: raise HTTPException(403,"Нельзя удалить чужое сообщение")
    ids=[m.user_id for m in db.query(models.RoomMember).filter_by(room_id=room_id).all()]
    db.delete(msg); db.commit()  # Хард-удаление — полностью из БД
    await manager.broadcast(ids,{"type":"msg_deleted","message_id":mid,"room_id":room_id})
    return {"ok":True}

@app.patch("/api/messages/{mid}")
async def edit_msg(mid:int, token:str=Query(...), content:str=Query(...), db:Session=Depends(get_db)):
    me=auth(token,db)
    msg=db.get(models.Message,mid)
    if not msg or msg.is_deleted: raise HTTPException(404,"Не найдено")
    if msg.sender_id!=me.id: raise HTTPException(403,"Нельзя редактировать чужое")
    if msg.msg_type!="text": raise HTTPException(400,"Редактировать можно только текст")
    content=content.strip()[:4096]
    if not content: raise HTTPException(400,"Пустое сообщение")
    msg.content=content; msg.edited_at=datetime.utcnow(); db.commit()
    m=load_msg(db,mid)
    ids=[m2.user_id for m2 in db.query(models.RoomMember).filter_by(room_id=msg.room_id).all()]
    await manager.broadcast(ids,{"type":"msg_edited","message":msg_dict(m)})
    return msg_dict(m)

@app.post("/api/messages/{mid}/pin")
async def pin_msg(mid:int, token:str=Query(...), db:Session=Depends(get_db)):
    me=auth(token,db)
    msg=db.get(models.Message,mid)
    if not msg: raise HTTPException(404,"Не найдено")
    mb=db.query(models.RoomMember).filter_by(room_id=msg.room_id,user_id=me.id).first()
    if not mb: raise HTTPException(403,"Нет доступа")
    msg.is_pinned=not msg.is_pinned
    room=db.get(models.Room,msg.room_id)
    if msg.is_pinned: room.pinned_msg_id=mid
    elif getattr(room,"pinned_msg_id",None)==mid: room.pinned_msg_id=None
    db.commit()
    ids=[m.user_id for m in db.query(models.RoomMember).filter_by(room_id=msg.room_id).all()]
    await manager.broadcast(ids,{"type":"msg_pinned","message_id":mid,
                                  "room_id":msg.room_id,"is_pinned":msg.is_pinned})
    return {"ok":True,"is_pinned":msg.is_pinned}

# Upload
@app.post("/api/rooms/{rid}/upload")
async def upload(rid:int, token:str=Query(...), file:UploadFile=File(...),
                 reply_to_id:int=Form(0), db:Session=Depends(get_db)):
    me=auth(token,db)
    if not db.query(models.RoomMember).filter_by(room_id=rid,user_id=me.id).first():
        raise HTTPException(403,"Нет доступа")
    raw=await file.read()
    if len(raw)>MAX_UPLOAD: raise HTTPException(400,f"Файл слишком большой (макс. {MAX_UPLOAD//1024//1024} МБ)")
    mime=file.content_type or "application/octet-stream"
    fname=file.filename or "file"
    if mime.startswith("image/"): mt="gif" if mime=="image/gif" else "image"
    elif mime.startswith("audio/"): mt="voice"
    elif mime.startswith("video/"): mt="video"
    else: mt="file"
    
    # Пробуем загрузить на Яндекс Диск
    yadisk_uri = await yadisk_upload(raw, fname, mime)
    
    if yadisk_uri:
        # Файл на Яндекс Диске — храним только URI, не base64
        msg=models.Message(msg_type=mt, content=fname,
                           media_data=yadisk_uri,   # "yadisk:public_key"
                           media_mime=mime, media_size=len(raw),
                           sender_id=me.id, room_id=rid,
                           reply_to_id=reply_to_id or None)
        logger.info(f"Uploaded to YaDisk: {fname} ({len(raw)//1024}KB)")
    else:
        # Fallback: храним в БД как base64 (если нет токена Диска)
        b64=base64.b64encode(raw).decode()
        msg=models.Message(msg_type=mt, content=fname,
                           media_data=b64, media_mime=mime, media_size=len(raw),
                           sender_id=me.id, room_id=rid,
                           reply_to_id=reply_to_id or None)
        logger.info(f"Stored in DB (no YaDisk): {fname} ({len(raw)//1024}KB)")
    
    db.add(msg); db.commit()
    m=load_msg(db,msg.id)
    ids=[mb.user_id for mb in db.query(models.RoomMember).filter_by(room_id=rid).all()]
    await manager.broadcast(ids,{"type":"new_message","message":msg_dict(m)})
    # Push всем кроме отправителя (сервис-воркер сам решит показывать или нет)
    _sndr = m.get("sender") or {}; sender_name = _sndr.get("display_name") or _sndr.get("username") or "Кто-то"
    pbody = m["content"][:80] if m.get("msg_type")=="text" else "📎 Медиафайл"
    for uid in ids:
        if uid != me.id:
            await send_push_to_user(db, uid, sender_name, pbody, room_id=rid)
    return msg_dict(m)

@app.get("/api/media/{msg_id}/url")
async def get_media_url(msg_id:int, token:str=Query(...), db:Session=Depends(get_db)):
    """Возвращает прямую ссылку на файл (для аудио/видео/изображений)."""
    auth(token,db)
    msg=db.get(models.Message,msg_id)
    if not msg or not msg.media_data: raise HTTPException(404,"Медиа не найдено")
    if msg.media_data.startswith("yadisk:"):
        public_key=msg.media_data[7:]
        download_url=await yadisk_get_download_url(public_key)
        if not download_url: raise HTTPException(503,"Не удалось получить ссылку с Яндекс Диска")
        return {"url": download_url, "mime": msg.media_mime}
    else:
        # base64 — возвращаем data URL
        return {"url": f"data:{msg.media_mime};base64,{msg.media_data}", "mime": msg.media_mime}

@app.get("/api/media/{msg_id}")
async def get_media(msg_id:int, token:str=Query(...), dl:int=Query(0), db:Session=Depends(get_db)):
    """Отдаёт медиафайл. Аудио/видео — стрим через сервер (нужен для браузера). Изображения — редирект."""
    from fastapi.responses import RedirectResponse, Response, StreamingResponse
    from starlette.requests import Request
    auth(token,db)
    msg=db.get(models.Message,msg_id)
    if not msg or not msg.media_data: raise HTTPException(404,"Медиа не найдено")

    mime = msg.media_mime or "application/octet-stream"

    if msg.media_data.startswith("yadisk:"):
        public_key=msg.media_data[7:]
        download_url=await yadisk_get_download_url(public_key)
        if not download_url: raise HTTPException(503,"Не удалось получить файл с Яндекс Диска")

        # Аудио, видео и изображения — стримим через сервер (нужен для CORS и браузерной совместимости)
        if mime.startswith("audio/") or mime.startswith("video/") or mime.startswith("image/"):
            try:
                async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
                    r = await c.get(download_url)
                    if r.status_code != 200:
                        raise HTTPException(502,"Ошибка получения файла")
                    fname = msg.content or "file"
                    return Response(
                        content=r.content,
                        media_type=mime,
                        headers={
                            "Content-Disposition": f'inline; filename="{fname}"',
                            "Accept-Ranges": "bytes",
                            "Cache-Control": "public, max-age=3600",
                            "Access-Control-Allow-Origin": "*",
                        }
                    )
            except HTTPException: raise
            except Exception as e:
                logger.error(f"Media stream error: {e}")
                raise HTTPException(502,"Ошибка получения файла")

        # Прочие файлы — стримим с disposition=attachment
        try:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
                r = await c.get(download_url)
                if r.status_code != 200:
                    raise HTTPException(502,"Ошибка получения файла")
                fname = msg.content or "file"
                return Response(
                    content=r.content,
                    media_type=mime,
                    headers={
                        "Content-Disposition": f'attachment; filename="{fname}"',
                        "Cache-Control": "public, max-age=3600",
                    }
                )
        except HTTPException: raise
        except Exception as e:
            logger.error(f"File download error: {e}")
            raise HTTPException(502,"Ошибка получения файла")
    else:
        raw=base64.b64decode(msg.media_data)
        fname = msg.content or "file"
        disposition = "attachment" if dl else "inline"
        return Response(content=raw, media_type=mime,
                        headers={"Content-Disposition": f'{disposition}; filename="{fname}"'})

# Reactions
@app.post("/api/messages/{mid}/react")
async def react(mid:int, emoji:str=Query(...), token:str=Query(...), db:Session=Depends(get_db)):
    me=auth(token,db)
    msg=db.get(models.Message,mid)
    if not msg: raise HTTPException(404,"Не найдено")
    ex=db.query(models.MessageReaction).filter_by(message_id=mid,user_id=me.id,emoji=emoji).first()
    if ex: db.delete(ex)
    else: db.add(models.MessageReaction(message_id=mid,user_id=me.id,emoji=emoji))
    db.commit(); db.refresh(msg)
    g=rxn_grouped(msg.reactions)
    ids=[m.user_id for m in db.query(models.RoomMember).filter_by(room_id=msg.room_id).all()]
    await manager.broadcast(ids,{"type":"reaction_update","message_id":mid,"room_id":msg.room_id,"reactions":g})
    return g

# GIF search (Giphy — бесплатный ключ на developers.giphy.com)
@app.get("/api/gif/search")
async def gif_search(q:str=Query(...), token:str=Query(...), limit:int=Query(20), db:Session=Depends(get_db)):
    auth(token,db)
    if not GIPHY_API_KEY:
        return {"results":[]}
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r=await c.get("https://api.giphy.com/v1/gifs/search",
                          params={"q":q,"api_key":GIPHY_API_KEY,"limit":limit,"rating":"g","lang":"ru"})
            data=r.json()
        results=[{
            "id":item["id"],
            "url":item["images"]["original"]["url"],
            "preview":item["images"]["fixed_width_small"]["url"],
            "title":item.get("title",""),
        } for item in data.get("data",[])]
        return {"results":results}
    except Exception as e:
        logger.error(f"GIF search error: {e}"); return {"results":[]}

# AI Chat
@app.post("/api/ai/message")
async def ai_message(body:schemas.AIMessageRequest, token:str=Query(...), db:Session=Depends(get_db)):
    me=auth(token,db)
    if not db.query(models.RoomMember).filter_by(room_id=body.room_id,user_id=me.id).first():
        raise HTTPException(403,"Нет доступа")
    room=db.get(models.Room,body.room_id)
    if not room or getattr(room,"room_type","chat")!="ai":
        raise HTTPException(400,"Не AI-чат")

    # Сохраняем сообщение пользователя
    umsg=models.Message(msg_type="text",content=body.message,sender_id=me.id,room_id=body.room_id)
    db.add(umsg); db.commit()
    um=load_msg(db,umsg.id)
    ids=[m.user_id for m in db.query(models.RoomMember).filter_by(room_id=body.room_id).all()]
    await manager.broadcast(ids,{"type":"new_message","message":msg_dict(um)})

    # Загружаем контекст диалога (последние 20 сообщений)
    history=db.query(models.Message).filter(
        models.Message.room_id==body.room_id,
        models.Message.is_deleted==False
    ).order_by(models.Message.created_at.desc()).limit(20).all()
    history.reverse()

    messages=[{"role":"user" if m.sender_id==me.id else "assistant",
               "content":m.content} for m in history]

    ai_reply=await _call_ai(messages)

    # Сохраняем ответ ИИ (sender_id=None = AI)
    amsg=models.Message(msg_type="text",content=ai_reply,sender_id=None,room_id=body.room_id)
    db.add(amsg); db.commit()
    am=load_msg(db,amsg.id)
    await manager.broadcast(ids,{"type":"new_message","message":msg_dict(am)})
    return {"ok":True}

async def _call_ai(messages:list) -> str:
    system={"role":"system","content":"Ты полезный ИИ-ассистент в мессенджере. Отвечай на русском языке, кратко и по делу."}
    payload_msgs=[system]+messages[-18:]

    # Пробуем Groq
    if GROQ_API_KEY and AI_BACKEND in ("groq","auto"):
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r=await c.post("https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization":f"Bearer {GROQ_API_KEY}","Content-Type":"application/json"},
                    json={"model":"llama-3.1-8b-instant","messages":payload_msgs,"max_tokens":1024})
                if r.status_code==200:
                    return r.json()["choices"][0]["message"]["content"]
        except Exception as e: logger.error(f"Groq error: {e}")

    # Пробуем Cerebras
    if CEREBRAS_KEY and AI_BACKEND in ("cerebras","auto"):
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r=await c.post("https://api.cerebras.ai/v1/chat/completions",
                    headers={"Authorization":f"Bearer {CEREBRAS_KEY}","Content-Type":"application/json"},
                    json={"model":"llama-3.3-70b","messages":payload_msgs,"max_tokens":1024})
                if r.status_code==200:
                    return r.json()["choices"][0]["message"]["content"]
        except Exception as e: logger.error(f"Cerebras error: {e}")

    return "⚠️ ИИ временно недоступен. Проверьте API ключ в переменных окружения Railway (GROQ_API_KEY или CEREBRAS_API_KEY)."


@app.patch("/api/rooms/{rid}")
async def update_room(rid:int, token:str=Query(...), db:Session=Depends(get_db),
                      name:str=Query(None), description:str=Query(None),
                      avatar_img:str=Query(None), avatar_color:str=Query(None)):
    me=auth(token,db)
    room=db.get(models.Room,rid)
    if not room: raise HTTPException(404,"Не найдено")
    mb=db.query(models.RoomMember).filter_by(room_id=rid,user_id=me.id).first()
    if not mb or not mb.is_admin: raise HTTPException(403,"Только администратор")
    if name is not None: room.name=name
    if description is not None: room.description=description
    if avatar_img is not None:
        if avatar_img and len(avatar_img)>3_000_000: raise HTTPException(400,"Фото слишком большое")
        room.avatar_img=avatar_img or None
    if avatar_color is not None: room.avatar_color=avatar_color
    db.commit()
    r=room_dict(room,me.id,db)
    ids=[m.user_id for m in db.query(models.RoomMember).filter_by(room_id=rid).all()]
    await manager.broadcast(ids,{"type":"room_updated","room":r})
    return r


@app.post("/api/rooms/{rid}/members/add")
async def add_member(rid:int, user_id:int=Query(...), token:str=Query(...), db:Session=Depends(get_db)):
    me=auth(token,db)
    room=db.get(models.Room,rid)
    if not room or room.room_type not in ("group","channel"): raise HTTPException(400,"Не группа")
    mb=db.query(models.RoomMember).filter_by(room_id=rid,user_id=me.id).first()
    if not mb or not mb.is_admin: raise HTTPException(403,"Только админ")
    if db.query(models.RoomMember).filter_by(room_id=rid,user_id=user_id).first():
        raise HTTPException(400,"Уже в группе")
    u=db.get(models.User,user_id)
    if not u: raise HTTPException(404,"Пользователь не найден")
    db.add(models.RoomMember(room_id=rid,user_id=user_id,is_admin=False))
    db.commit()
    await manager.send(user_id,{"type":"added_to_room","room":room_dict(room,user_id,db)})
    return {"ok":True}

@app.delete("/api/rooms/{rid}/members/{uid}")
async def remove_member(rid:int, uid:int, token:str=Query(...), db:Session=Depends(get_db)):
    me=auth(token,db)
    room=db.get(models.Room,rid)
    if not room or room.room_type not in ("group","channel"): raise HTTPException(400,"Не группа")
    # Можно уйти самому или убрать другого (если ты админ)
    if uid!=me.id:
        mb=db.query(models.RoomMember).filter_by(room_id=rid,user_id=me.id).first()
        if not mb or not mb.is_admin: raise HTTPException(403,"Только админ")
    target=db.query(models.RoomMember).filter_by(room_id=rid,user_id=uid).first()
    if not target: raise HTTPException(404,"Не участник")
    db.delete(target); db.commit()
    await manager.send(uid,{"type":"removed_from_room","room_id":rid})
    ids=[m.user_id for m in db.query(models.RoomMember).filter_by(room_id=rid).all()]
    await manager.broadcast(ids,{"type":"member_removed","room_id":rid,"user_id":uid})
    return {"ok":True}

@app.patch("/api/rooms/{rid}/members/{uid}/admin")
async def toggle_admin(rid:int, uid:int, token:str=Query(...), db:Session=Depends(get_db)):
    me=auth(token,db)
    room=db.get(models.Room,rid)
    if not room: raise HTTPException(404,"Не найдено")
    if room.created_by!=me.id: raise HTTPException(403,"Только создатель")
    mb=db.query(models.RoomMember).filter_by(room_id=rid,user_id=uid).first()
    if not mb: raise HTTPException(404,"Не участник")
    mb.is_admin=not mb.is_admin; db.commit()
    return {"ok":True,"is_admin":mb.is_admin}

# Create AI room for user
@app.post("/api/rooms/ai")
async def create_ai_room(token:str=Query(...), db:Session=Depends(get_db)):
    me=auth(token,db)
    # Проверяем, нет ли уже AI-комнаты
    existing=db.query(models.RoomMember).join(models.Room).filter(
        models.RoomMember.user_id==me.id,
        models.Room.room_type=="ai").first()
    if existing: return room_dict(existing.room,me.id,db)
    room=models.Room(name="ИИ Ассистент",room_type="ai",created_by=me.id)
    db.add(room); db.flush()
    db.add(models.RoomMember(room_id=room.id,user_id=me.id,is_admin=True))
    db.commit(); db.refresh(room)
    return room_dict(room,me.id,db)

# Public channels list
@app.get("/api/channels")
async def pub_channels(token:str=Query(...), search:str=Query(""), db:Session=Depends(get_db)):
    auth(token,db)
    q=db.query(models.Room).filter(models.Room.room_type=="channel")
    if search: q=q.filter(models.Room.name.ilike(f"%{search}%"))
    rooms=q.order_by(models.Room.created_at.desc()).limit(30).all()
    return [{"id":r.id,"name":r.name,"description":getattr(r,"description",""),
             "members_count":len(r.members)} for r in rooms]

# ── Admin endpoints ───────────────────────────────────────────

@app.get("/api/admin/users")
async def admin_users(token:str=Query(...), db:Session=Depends(get_db)):
    me=auth(token,db)
    if not is_admin_user(me): raise HTTPException(403,"Нет прав")
    users=db.query(models.User).order_by(models.User.created_at.desc()).all()
    return [{"id":u.id,"username":u.username,"display_name":u.display_name,
             "email":u.email,"is_online":manager.is_online(u.id),
             "avatar_color":u.avatar_color,"created_at":u.created_at.isoformat()}
            for u in users]

@app.get("/api/admin/rooms")
async def admin_rooms(token:str=Query(...), db:Session=Depends(get_db)):
    me=auth(token,db)
    if not is_admin_user(me): raise HTTPException(403,"Нет прав")
    rooms=db.query(models.Room).order_by(models.Room.created_at.desc()).limit(100).all()
    return [{"id":r.id,"name":r.name,"room_type":getattr(r,"room_type","chat"),
             "members_count":len(r.members),"created_at":r.created_at.isoformat()} for r in rooms]

@app.post("/api/admin/message/{user_id}")
async def admin_message(user_id:int, content:str=Query(...), token:str=Query(...), db:Session=Depends(get_db)):
    """Админ пишет пользователю от лица системы/поддержки"""
    me=auth(token,db)
    if not is_admin_user(me): raise HTTPException(403,"Нет прав")
    target=db.get(models.User,user_id)
    if not target: raise HTTPException(404,"Пользователь не найден")
    # Находим или создаём чат поддержки
    support_room = _get_or_create_support_room(db, me.id, user_id)
    msg=models.Message(msg_type="text",content=content,sender_id=me.id,room_id=support_room.id)
    db.add(msg); db.commit()
    m=load_msg(db,msg.id)
    await manager.broadcast([me.id,user_id],{"type":"new_message","message":msg_dict(m)})
    return {"ok":True}

@app.delete("/api/admin/users/{uid}")
async def admin_del_user(uid:int, token:str=Query(...), db:Session=Depends(get_db)):
    me=auth(token,db)
    if not is_admin_user(me): raise HTTPException(403,"Нет прав")
    if uid==me.id: raise HTTPException(400,"Нельзя удалить себя")
    u=db.get(models.User,uid)
    if not u: raise HTTPException(404,"Не найден")
    db.delete(u); db.commit()
    return {"ok":True}

@app.post("/api/support")
async def open_support(token:str=Query(...), db:Session=Depends(get_db)):
    """Открыть чат с техподдержкой"""
    me=auth(token,db)
    # Находим первого пользователя-админа
    admin=db.query(models.User).filter(
        models.User.username.in_(list(ADMIN_USERNAMES))
    ).first()
    if not admin:
        # Если нет никакого аккаунта "admin", возвращаем AI-чат
        existing=db.query(models.RoomMember).join(models.Room).filter(
            models.RoomMember.user_id==me.id, models.Room.room_type=="ai").first()
        if existing: return room_dict(existing.room,me.id,db)
        room=models.Room(name="Техподдержка",room_type="ai",created_by=me.id)
        db.add(room); db.flush()
        db.add(models.RoomMember(room_id=room.id,user_id=me.id,is_admin=True))
        db.commit(); db.refresh(room)
        return room_dict(room,me.id,db)
    room=_get_or_create_support_room(db,admin.id,me.id)
    return room_dict(room,me.id,db)

def _get_or_create_support_room(db, admin_id, user_id):
    """Находит или создаёт чат поддержки между admin и user"""
    if admin_id==user_id:
        # Если admin открывает сам себе — ищем AI
        existing=db.query(models.RoomMember).join(models.Room).filter(
            models.RoomMember.user_id==user_id,models.Room.room_type=="ai").first()
        if existing: return existing.room
    # Ищем существующий чат
    q1=db.query(models.RoomMember.room_id).filter_by(user_id=admin_id).subquery()
    q2=db.query(models.RoomMember.room_id).filter_by(user_id=user_id).subquery()
    room=db.query(models.Room).filter(
        models.Room.room_type=="chat",
        models.Room.id.in_(q1),models.Room.id.in_(q2)).first()
    if room: return room
    room=models.Room(name="Поддержка",room_type="chat",created_by=admin_id)
    db.add(room); db.flush()
    db.add(models.RoomMember(room_id=room.id,user_id=admin_id,is_admin=True))
    if admin_id!=user_id:
        db.add(models.RoomMember(room_id=room.id,user_id=user_id))
    db.commit(); db.refresh(room); return room

# ── WebSocket ─────────────────────────────────────────────────
@app.websocket("/ws/{token}")
async def ws(websocket:WebSocket, token:str):
    uid:Optional[int]=None; db=SessionLocal()
    try:
        uid=decode_token(token)
        if not uid: await websocket.close(code=4001); return
        u=db.get(models.User,uid)
        if not u: await websocket.close(code=4001); return

        await manager.connect(websocket,uid)
        u.is_online=True; db.commit()
        await _status(db,uid,True)

        while True:
            try: raw=await websocket.receive_text()
            except WebSocketDisconnect: break
            try: d=json.loads(raw)
            except: continue
            if not isinstance(d,dict): continue
            t=d.get("type","")

            if t in("message","sticker"):
                if not rl.ok(uid):
                    await manager.send(uid,{"type":"error","message":"Слишком быстро"}); continue
                await _on_msg(db,uid,d)
            elif t=="typing": await _on_typing(db,uid,d)
            elif t=="read": await _on_read(db,uid,d)
            elif t=="ping": await manager.send(uid,{"type":"pong"})
            # WebRTC сигналинг
            elif t in("call_offer","call_answer","call_ice","call_end","call_busy"):
                await _on_call(db,uid,d)

    except WebSocketDisconnect: pass
    except Exception as e: logger.error(f"WS uid={uid}: {e}",exc_info=True)
    finally:
        if uid is not None:
            await manager.disconnect(uid)
            try:
                u2=db.get(models.User,uid)
                if u2:
                    u2.is_online=False
                    u2.last_seen=datetime.utcnow()
                    db.commit()
                await _status(db,uid,False)
            except Exception as e: logger.error(f"WS cleanup: {e}")
        db.close()

async def _on_msg(db,sender_id,d):
    content=(d.get("content") or "").strip()[:4096]
    room_id=d.get("room_id")
    reply_to_id=d.get("reply_to_id")
    hint=d.get("msg_type_hint","")
    # Определяем тип: sticker, gif_url, или text
    if d.get("type")=="sticker":
        mt="sticker"
    elif hint=="gif_url" or (content.startswith("https://") and "tenor" in content):
        mt="gif_url"
    else:
        mt="text"
    if not content or not room_id: return
    mb=db.query(models.RoomMember).filter_by(room_id=room_id,user_id=sender_id).first()
    if not mb: return
    room=db.get(models.Room,room_id)
    # В канале могут писать только админы
    if getattr(room,"room_type","chat")=="channel" and not mb.is_admin: return
    msg=models.Message(msg_type=mt,content=content,sender_id=sender_id,
                       room_id=room_id,reply_to_id=reply_to_id or None)
    db.add(msg); db.commit()
    m=load_msg(db,msg.id)
    ids=[m2.user_id for m2 in db.query(models.RoomMember).filter_by(room_id=room_id).all()]
    await manager.broadcast(ids,{"type":"new_message","message":msg_dict(m)})
    # Push тем кто офлайн
    md=msg_dict(m)
    _sndr=md.get("sender") or {}
    sname=_sndr.get("display_name") or _sndr.get("username") or "Кто-то"
    pbody=content[:80] if mt=="text" else ("🎨 Стикер" if mt=="sticker" else "📎 Медиа")
    for uid in ids:
        if uid!=sender_id:
            await send_push_to_user(db,uid,sname,pbody,room_id=room_id)

async def _on_typing(db,uid,d):
    rid=d.get("room_id"); u=db.get(models.User,uid)
    if not rid or not u: return
    others=[m.user_id for m in db.query(models.RoomMember).filter(
        models.RoomMember.room_id==rid,models.RoomMember.user_id!=uid).all()]
    await manager.broadcast(others,{"type":"typing","room_id":rid,"user_id":uid,
                                     "username":u.username,"is_typing":bool(d.get("is_typing"))})

async def _on_read(db,uid,d):
    rid=d.get("room_id")
    if not rid or not db.query(models.RoomMember).filter_by(room_id=rid,user_id=uid).first(): return
    unread=db.query(models.Message).filter(
        models.Message.room_id==rid,models.Message.sender_id!=uid,
        models.Message.is_read==False).all()
    if not unread: return
    senders=set(m.sender_id for m in unread if m.sender_id)
    for m in unread: m.is_read=True
    db.commit()
    await manager.broadcast(list(senders),{"type":"messages_read","room_id":rid,"reader_id":uid})

async def _on_call(db,caller_id,d):
    """WebRTC сигналинг — пересылаем offer/answer/ICE целевому пользователю"""
    t=d.get("type"); target_id=d.get("target_id"); room_id=d.get("room_id")
    if not target_id: return
    # Добавляем caller_id в данные и пересылаем
    payload={**d,"caller_id":caller_id}
    await manager.send(target_id,payload)
    # Push для входящего звонка если адресат офлайн
    if t=="call_offer":
        caller=db.get(models.User,caller_id)
        cname=(caller.display_name or caller.username) if caller else "Кто-то"
        ctype="видео" if d.get("call_type")=="video" else "голосовой"
        await send_push_to_user(db,target_id,f"📞 Входящий {ctype} звонок",cname,tag="call")
    # Записываем в историю звонков
    if t=="call_offer" and room_id:
        ct=d.get("call_type","voice")
        call=models.Call(room_id=room_id,caller_id=caller_id,call_type=ct)
        db.add(call); db.commit()
        db.refresh(call)
        d["_call_id"]=call.id
    elif t=="call_end" and d.get("_call_id"):
        call=db.get(models.Call,d["_call_id"])
        if call:
            call.ended_at=datetime.utcnow()
            call.status="answered" if d.get("answered") else "missed"
            if call.ended_at and call.started_at:
                call.duration_sec=int((call.ended_at-call.started_at).total_seconds())
            db.commit()

async def _status(db,uid,online):
    ms=db.query(models.RoomMember).filter_by(user_id=uid).all()
    contacts=set()
    for m in ms:
        for mm in m.room.members:
            if mm.user_id!=uid: contacts.add(mm.user_id)
    u=db.get(models.User,uid)
    ls=u.last_seen.isoformat() if u and getattr(u,"last_seen",None) else None
    await manager.broadcast(list(contacts),{"type":"user_status","user_id":uid,
                                             "is_online":online,"last_seen":ls})
