"""
models.py — Таблицы базы данных

User       → пользователи
Room       → чаты (личные и групповые)
RoomMember → кто в каком чате (связь многие-ко-многим)
Message    → сообщения
"""

import random
from datetime import datetime

from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Text, Index
from sqlalchemy.orm import relationship

from database import Base

AVATAR_COLORS = [
    "#E17055", "#00B894", "#0984E3", "#6C5CE7",
    "#FDCB6E", "#E84393", "#00CEC9", "#55EFC4",
    "#74B9FF", "#A29BFE", "#FD79A8", "#FAB1A0",
]


def _random_color() -> str:
    return random.choice(AVATAR_COLORS)


class User(Base):
    __tablename__ = "users"

    id               = Column(Integer, primary_key=True, index=True)
    username         = Column(String(32),  unique=True, index=True, nullable=False)
    email            = Column(String(100), unique=True, index=True, nullable=False)
    hashed_password  = Column(String(255), nullable=False)
    is_online        = Column(Boolean, default=False, nullable=False)
    avatar_color     = Column(String(7),   default=_random_color, nullable=False)
    created_at       = Column(DateTime,    default=datetime.utcnow, nullable=False)

    sent_messages = relationship(
        "Message", foreign_keys="Message.sender_id",
        back_populates="sender", lazy="select",
    )
    memberships = relationship("RoomMember", back_populates="user", lazy="select")


class Room(Base):
    __tablename__ = "rooms"

    id         = Column(Integer, primary_key=True, index=True)
    name       = Column(String(64), nullable=True)
    is_group   = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    messages = relationship(
        "Message", back_populates="room",
        order_by="Message.created_at", lazy="select",
    )
    # lazy="joined" — участники загружаются вместе с комнатой (нужны для отображения имени)
    members = relationship("RoomMember", back_populates="room", lazy="joined")


class RoomMember(Base):
    __tablename__ = "room_members"

    id      = Column(Integer, primary_key=True, index=True)
    room_id = Column(Integer, ForeignKey("rooms.id",  ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id",  ondelete="CASCADE"), nullable=False, index=True)

    room = relationship("Room", back_populates="members")
    # lazy="joined" — данные пользователя (имя, цвет) нужны всегда
    user = relationship("User", back_populates="memberships", lazy="joined")


class Message(Base):
    __tablename__ = "messages"

    id         = Column(Integer, primary_key=True, index=True)
    content    = Column(Text,    nullable=False)
    sender_id  = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True,  index=True)
    room_id    = Column(Integer, ForeignKey("rooms.id", ondelete="CASCADE"),  nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False,    index=True)
    is_read    = Column(Boolean,  default=False, nullable=False)

    # lazy="joined" — отправитель нужен всегда при выдаче сообщений
    sender = relationship(
        "User", foreign_keys=[sender_id],
        back_populates="sent_messages", lazy="joined",
    )
    room = relationship("Room", back_populates="messages")

    __table_args__ = (
        # Составной индекс: быстрая история чата (room_id + дата)
        Index("ix_messages_room_created", "room_id", "created_at"),
    )
