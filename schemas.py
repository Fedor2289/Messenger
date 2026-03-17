from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, EmailStr, field_validator, ConfigDict

class RegisterRequest(BaseModel):
    username: str
    email: EmailStr
    password: str

    @field_validator("username")
    @classmethod
    def val_username(cls, v):
        v = v.strip()
        if len(v) < 2: raise ValueError("Минимум 2 символа")
        if len(v) > 32: raise ValueError("Максимум 32 символа")
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
        if not all(c in allowed for c in v): raise ValueError("Только буквы, цифры, _ и -")
        return v

    @field_validator("password")
    @classmethod
    def val_password(cls, v):
        if len(v) < 4: raise ValueError("Минимум 4 символа")
        return v

class LoginRequest(BaseModel):
    username: str
    password: str

class ProfileUpdate(BaseModel):
    display_name: Optional[str] = None
    bio: Optional[str] = None
    avatar_color: Optional[str] = None

    @field_validator("display_name")
    @classmethod
    def val_dn(cls, v):
        if v is not None and len(v.strip()) > 64: raise ValueError("Максимум 64 символа")
        return v.strip() if v else v

    @field_validator("bio")
    @classmethod
    def val_bio(cls, v):
        if v is not None and len(v) > 200: raise ValueError("Максимум 200 символов")
        return v

    @field_validator("avatar_color")
    @classmethod
    def val_color(cls, v):
        if v and (len(v) != 7 or not v.startswith("#")):
            raise ValueError("Формат #RRGGBB")
        return v

class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    display_name: Optional[str]
    bio: Optional[str]
    email: str
    is_online: bool
    avatar_color: str
    created_at: datetime

class UserShort(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    display_name: Optional[str]
    is_online: bool
    avatar_color: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut

class ReactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    emoji: str
    user_id: int

class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    msg_type: str
    content: str
    media_data: Optional[str]
    media_mime: Optional[str]
    media_size: Optional[int]
    sender_id: Optional[int]
    room_id: int
    created_at: datetime
    is_read: bool
    sender: Optional[UserShort]
    reactions: List[ReactionOut] = []

class DirectRoomRequest(BaseModel):
    user_id: int

class GroupRoomRequest(BaseModel):
    name: str
    member_ids: List[int]

    @field_validator("name")
    @classmethod
    def val_name(cls, v):
        v = v.strip()
        if not v: raise ValueError("Название не может быть пустым")
        if len(v) > 64: raise ValueError("Максимум 64 символа")
        return v

class RoomOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: Optional[str]
    is_group: bool
    created_by: Optional[int]
    created_at: datetime
    members: List[UserShort]
    last_message: Optional[MessageOut] = None
    unread_count: int = 0
