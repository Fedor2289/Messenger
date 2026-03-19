from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, EmailStr, field_validator, ConfigDict

class RegisterRequest(BaseModel):
    username: str
    email: EmailStr
    password: str
    @field_validator("username")
    @classmethod
    def val_u(cls,v):
        v=v.strip()
        if len(v)<2: raise ValueError("Минимум 2 символа")
        if len(v)>32: raise ValueError("Максимум 32 символа")
        allowed=set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
        if not all(c in allowed for c in v): raise ValueError("Только буквы, цифры, _ и -")
        return v
    @field_validator("password")
    @classmethod
    def val_p(cls,v):
        if len(v)<4: raise ValueError("Минимум 4 символа")
        return v

class LoginRequest(BaseModel):
    username: str
    password: str

class ProfileUpdate(BaseModel):
    display_name: Optional[str] = None
    bio: Optional[str] = None
    avatar_color: Optional[str] = None
    avatar_img: Optional[str] = None

class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int; username: str; display_name: Optional[str] = None
    bio: Optional[str] = None; avatar_img: Optional[str] = None
    email: str; is_online: bool; avatar_color: Optional[str] = "#5288c1"; created_at: datetime
    last_seen: Optional[datetime] = None

class UserShort(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int; username: str; display_name: Optional[str] = None
    avatar_img: Optional[str] = None; is_online: bool; avatar_color: Optional[str] = "#5288c1"
    last_seen: Optional[datetime] = None

class TokenResponse(BaseModel):
    access_token: str; token_type: str = "bearer"; user: UserOut

class ReactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    emoji: str; user_id: int

class ReplySnippet(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int; content: str; sender: Optional[UserShort] = None
    msg_type: str = "text"

class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int; msg_type: str; content: str
    media_data: Optional[str] = None; media_mime: Optional[str] = None
    media_size: Optional[int] = None; sender_id: Optional[int] = None
    room_id: int; created_at: datetime; is_read: bool
    is_deleted: bool = False; is_pinned: bool = False
    edited_at: Optional[datetime] = None
    reply_to: Optional[ReplySnippet] = None
    sender: Optional[UserShort] = None; reactions: dict = {}

class DirectRoomRequest(BaseModel):
    user_id: int

class GroupRoomRequest(BaseModel):
    name: str; member_ids: List[int]
    @field_validator("name")
    @classmethod
    def val_n(cls,v):
        v=v.strip()
        if not v: raise ValueError("Название обязательно")
        if len(v)>64: raise ValueError("Максимум 64 символа")
        return v

class ChannelRequest(BaseModel):
    name: str; description: Optional[str] = None

class RoomOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int; name: Optional[str] = None; room_type: str
    description: Optional[str] = None; created_by: Optional[int] = None
    created_at: datetime; members: List[UserShort] = []
    last_message: Optional[MessageOut] = None; unread_count: int = 0
    pinned_msg_id: Optional[int] = None

class AIMessageRequest(BaseModel):
    message: str; room_id: int
    file_data: Optional[str] = None  # base64 или текст файла
    file_name: Optional[str] = None
    file_mime: Optional[str] = None
