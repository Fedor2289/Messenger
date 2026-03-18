import random
from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Text, Index, UniqueConstraint, BigInteger, Enum
from sqlalchemy.orm import relationship
from database import Base

AVATAR_COLORS = [
    "#E17055","#00B894","#0984E3","#6C5CE7",
    "#FDCB6E","#E84393","#00CEC9","#55EFC4",
    "#74B9FF","#A29BFE","#FD79A8","#FAB1A0",
]

def _random_color():
    return random.choice(AVATAR_COLORS)

class User(Base):
    __tablename__ = "users"
    id              = Column(Integer, primary_key=True, index=True)
    username        = Column(String(32),  unique=True, index=True, nullable=False)
    email           = Column(String(100), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    display_name    = Column(String(64),  nullable=True)
    bio             = Column(String(200), nullable=True)
    avatar_color    = Column(String(7),   default=_random_color, nullable=False)
    avatar_img      = Column(Text,        nullable=True)
    is_online       = Column(Boolean, default=False, nullable=False)
    last_seen       = Column(DateTime, nullable=True)
    is_group        = Column(Boolean, default=False, nullable=True)  # для обратной совместимости с БД
    avatar_img       = Column(Text, nullable=True)      # аватарка группы/канала (base64)
    avatar_color     = Column(String(7), nullable=True)  # цвет аватарки группы
    created_at      = Column(DateTime, default=datetime.utcnow, nullable=False)

    sent_messages   = relationship("Message", foreign_keys="Message.sender_id",
                                   back_populates="sender", lazy="select")
    memberships     = relationship("RoomMember", back_populates="user", lazy="select")


class Room(Base):
    """
    room_type:
      'chat'    — личный чат 1:1
      'group'   — групповой чат
      'channel' — канал (только owner/admins пишут)
      'ai'      — чат с ИИ (один у каждого юзера)
    """
    __tablename__ = "rooms"
    id              = Column(Integer, primary_key=True, index=True)
    name            = Column(String(64), nullable=True)
    description     = Column(String(200), nullable=True)
    room_type       = Column(String(16), default='chat', nullable=False)
    created_by      = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    pinned_msg_id   = Column(Integer, nullable=True)
    is_group        = Column(Boolean, default=False, nullable=True)  # для обратной совместимости с БД
    avatar_img       = Column(Text, nullable=True)      # аватарка группы/канала (base64)
    avatar_color     = Column(String(7), nullable=True)  # цвет аватарки группы
    created_at      = Column(DateTime, default=datetime.utcnow, nullable=False)

    messages        = relationship("Message", back_populates="room",
                                   order_by="Message.created_at", lazy="select")
    members         = relationship("RoomMember", back_populates="room", lazy="joined")




class RoomMember(Base):
    __tablename__ = "room_members"
    id       = Column(Integer, primary_key=True, index=True)
    room_id  = Column(Integer, ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id  = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    is_admin = Column(Boolean, default=False, nullable=False)

    room = relationship("Room", back_populates="members")
    user = relationship("User", back_populates="memberships", lazy="joined")


class Message(Base):
    __tablename__ = "messages"
    id           = Column(Integer, primary_key=True, index=True)
    msg_type     = Column(String(16), default="text", nullable=False)
    content      = Column(Text, nullable=False)
    media_data   = Column(Text, nullable=True)
    media_mime   = Column(String(64), nullable=True)
    media_size   = Column(BigInteger, nullable=True)
    sender_id    = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    room_id      = Column(Integer, ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False, index=True)
    reply_to_id  = Column(Integer, ForeignKey("messages.id", ondelete="SET NULL"), nullable=True)
    is_read      = Column(Boolean, default=False, nullable=False)
    is_deleted   = Column(Boolean, default=False, nullable=False)
    is_pinned    = Column(Boolean, default=False, nullable=False)
    edited_at    = Column(DateTime, nullable=True)
    created_at   = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    sender    = relationship("User", foreign_keys=[sender_id],
                             back_populates="sent_messages", lazy="joined")
    room      = relationship("Room", back_populates="messages")
    reply_to  = relationship("Message", foreign_keys=[reply_to_id], remote_side="Message.id",
                             lazy="joined")
    reactions = relationship("MessageReaction", back_populates="message",
                             lazy="joined", cascade="all, delete-orphan")

    __table_args__ = (Index("ix_messages_room_created", "room_id", "created_at"),)


class MessageReaction(Base):
    __tablename__ = "message_reactions"
    id         = Column(Integer, primary_key=True, index=True)
    message_id = Column(Integer, ForeignKey("messages.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id    = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    emoji      = Column(String(32), nullable=False)
    message    = relationship("Message", back_populates="reactions")
    __table_args__ = (UniqueConstraint("message_id", "user_id", "emoji"),)


class Call(Base):
    """История звонков"""
    __tablename__ = "calls"
    id           = Column(Integer, primary_key=True, index=True)
    room_id      = Column(Integer, ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False, index=True)
    caller_id    = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    call_type    = Column(String(8), default='voice', nullable=False)  # voice | video
    status       = Column(String(16), default='missed', nullable=False)  # missed | answered | declined
    started_at   = Column(DateTime, default=datetime.utcnow, nullable=False)
    ended_at     = Column(DateTime, nullable=True)
    duration_sec = Column(Integer, nullable=True)
