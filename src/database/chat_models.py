"""
SQLAlchemy models for chat sessions and messages.

Зачем серверное хранение (а не localStorage браузера):
- localStorage общий для всех вкладок одного домена: при 2+ открытых вкладках
  каждая держит свой массив сессий в памяти и перезаписывает localStorage —
  диалоги теряются (гонка вкладок).
- Привязка сессий к пользователю (user_id): каждый пользователь видит только
  свои диалоги, доступны с любого устройства.
- История не теряется при очистке кэша браузера.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, String, Integer, Text, DateTime, ForeignKey, Index
from sqlalchemy.orm import relationship

from src.database.models import Base


class ChatSession(Base):
    """Сессия (диалог) чата, привязанная к пользователю."""

    __tablename__ = "chat_sessions"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(
        String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title = Column(String, default="Новый диалог")
    created_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    messages = relationship(
        "ChatMessage",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="ChatMessage.created_at",
    )

    __table_args__ = (
        Index("ix_chat_sessions_user_updated", "user_id", "updated_at"),
    )


class ChatMessage(Base):
    """Одно сообщение в сессии чата."""

    __tablename__ = "chat_messages"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id = Column(
        String, ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role = Column(String, nullable=False)  # user | assistant | system
    content = Column(Text, nullable=False)
    # Метаданные ответа (модель, intent, sources_count и т.п.) — JSON-строка,
    # чтобы фронтенд мог восстановить источники/ссылку на документы.
    metadata_json = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    session = relationship("ChatSession", back_populates="messages")

    __table_args__ = (
        Index("ix_chat_messages_session_created", "session_id", "created_at"),
    )
