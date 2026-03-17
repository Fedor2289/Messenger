"""
schemas.py — Pydantic-схемы: валидация входящих данных и сериализация ответов
"""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, EmailStr, field_validator, ConfigDict


# ── Пользователи ─────────────────────────────────────────────

class RegisterRequest(BaseModel):
    username: str
    email: EmailStr
    password: str

    @field_validator("username")
    @classmethod
    def validate_username(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 2:
            raise ValueError("Минимум 2 символа")
        if len(v) > 32:
            raise ValueError("Максимум 32 символа")
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
        if not all(c in allowed for c in v):
            raise ValueError("Только буквы, цифры, _ и -")
        return v

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        if len(v) < 4:
            raise ValueError("Минимум 4 символа")
        if len(v) > 128:
            raise ValueError("Максимум 128 символов")
        return v


class LoginRequest(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    email: str
    is_online: bool
    avatar_color: str
    created_at: datetime


class UserShort(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    is_online: bool
    avatar_color: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


# ── Сообщения ─────────────────────────────────────────────────

class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    content: str
    sender_id: Optional[int]
    room_id: int
    created_at: datetime
    is_read: bool
    sender: Optional[UserShort]


# ── Комнаты ───────────────────────────────────────────────────

class DirectRoomRequest(BaseModel):
    user_id: int


class GroupRoomRequest(BaseModel):
    name: str
    member_ids: List[int]

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Название не может быть пустым")
        if len(v) > 64:
            raise ValueError("Максимум 64 символа")
        return v

    @field_validator("member_ids")
    @classmethod
    def validate_members(cls, v: List[int]) -> List[int]:
        if len(v) > 200:
            raise ValueError("Максимум 200 участников")
        return list(set(v))  # убираем дубликаты


class RoomOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: Optional[str]
    is_group: bool
    created_at: datetime
    members: List[UserShort]
    last_message: Optional[MessageOut] = None
    unread_count: int = 0
