"""
Хранилище сессий чата (SQL, привязка к пользователю).

Зачем: раньше сессии жили в localStorage браузера — при нескольких вкладках
каждая перезаписывала общий localStorage, диалоги терялись. Серверное
хранение:
- сессия привязана к user_id — каждый пользователь видит только свои диалоги
- история доступна с любого устройства/вкладки (нет гонки localStorage)
- сообщения сохраняются на сервере, экспорт читает их из БД
"""

import json
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any

from sqlalchemy.orm import Session

from src.database.chat_models import ChatSession, ChatMessage


class ChatStorage:
    """CRUD для сессий и сообщений чата."""

    def _session_factory(self):
        # Ленивый импорт: get_doc_repo уже создаёт engine; используем его,
        # чтобы не плодить подключения. Фабрика сессий — из DocumentRepository.
        from src.api.services.document_repository import get_doc_repo
        return get_doc_repo()._Session()

    # ── Сессии ──────────────────────────────────────────────────────────

    def list_sessions(self, user_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Список сессий пользователя (новые сверху)."""
        with self._session_factory() as s:
            rows = (
                s.query(ChatSession)
                .filter(ChatSession.user_id == user_id)
                .order_by(ChatSession.updated_at.desc())
                .limit(limit)
                .all()
            )
            return [self._session_to_dict(r) for r in rows]

    def get_session(self, user_id: str, session_id: str) -> Optional[Dict[str, Any]]:
        """Сессия пользователя (с проверкой владельца)."""
        with self._session_factory() as s:
            row = (
                s.query(ChatSession)
                .filter(
                    ChatSession.id == session_id,
                    ChatSession.user_id == user_id,
                )
                .first()
            )
            return self._session_to_dict(row) if row else None

    def create_session(self, user_id: str, title: str = "Новый диалог") -> Dict[str, Any]:
        """Создать сессию."""
        with self._session_factory() as s:
            row = ChatSession(user_id=user_id, title=title or "Новый диалог")
            s.add(row)
            s.commit()
            s.refresh(row)
            return self._session_to_dict(row)

    def rename_session(self, user_id: str, session_id: str, title: str) -> bool:
        """Переименовать сессию (только владелец)."""
        with self._session_factory() as s:
            row = (
                s.query(ChatSession)
                .filter(ChatSession.id == session_id, ChatSession.user_id == user_id)
                .first()
            )
            if not row:
                return False
            row.title = title[:200]
            row.updated_at = datetime.now(timezone.utc)
            s.commit()
            return True

    def delete_session(self, user_id: str, session_id: str) -> bool:
        """Удалить сессию с сообщениями (только владелец)."""
        with self._session_factory() as s:
            row = (
                s.query(ChatSession)
                .filter(ChatSession.id == session_id, ChatSession.user_id == user_id)
                .first()
            )
            if not row:
                return False
            s.delete(row)  # cascade удалит chat_messages
            s.commit()
            return True

    # ── Сообщения ───────────────────────────────────────────────────────

    def list_messages(self, user_id: str, session_id: str) -> List[Dict[str, Any]]:
        """Сообщения сессии (проверка владельца сессии)."""
        with self._session_factory() as s:
            sess = (
                s.query(ChatSession)
                .filter(ChatSession.id == session_id, ChatSession.user_id == user_id)
                .first()
            )
            if not sess:
                return []
            rows = (
                s.query(ChatMessage)
                .filter(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.created_at.asc())
                .all()
            )
            return [self._message_to_dict(m) for m in rows]

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Добавить сообщение и обновить updated_at сессии."""
        with self._session_factory() as s:
            sess = s.query(ChatSession).filter(ChatSession.id == session_id).first()
            if not sess:
                raise ValueError(f"Сессия {session_id} не найдена")
            row = ChatMessage(
                session_id=session_id,
                role=role,
                content=content,
                metadata_json=json.dumps(metadata, ensure_ascii=False) if metadata else None,
            )
            sess.updated_at = datetime.now(timezone.utc)
            s.add(row)
            s.commit()
            s.refresh(row)
            return self._message_to_dict(row)

    # ── Сериализация ────────────────────────────────────────────────────

    def _session_to_dict(self, row: ChatSession) -> Dict[str, Any]:
        return {
            "id": row.id,
            "user_id": row.user_id,
            "title": row.title,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    def _message_to_dict(self, row: ChatMessage) -> Dict[str, Any]:
        meta = None
        if row.metadata_json:
            try:
                meta = json.loads(row.metadata_json)
            except (ValueError, TypeError):
                meta = None
        return {
            "id": row.id,
            "session_id": row.session_id,
            "role": row.role,
            "content": row.content,
            "metadata": meta,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }


chat_storage = ChatStorage()
