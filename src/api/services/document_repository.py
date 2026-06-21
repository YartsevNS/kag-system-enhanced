"""Репозиторий для работы с документами через SQL (вместо config_store)."""

import json
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session

from src.database.document_models import Document, Base


class DocumentRepository:
    """Тонкий слой над SQL для документов. Пагинация, индексы, prefetch."""

    def __init__(self, db_url: str):
        self._engine = create_engine(db_url, pool_pre_ping=True, pool_size=5)
        self._Session = sessionmaker(bind=self._engine)
        Base.metadata.create_all(self._engine)

    def _session(self) -> Session:
        return self._Session()

    # ── CRUD ────────────────────────────────────────────────────────────

    def get(self, document_id: str) -> Optional[Document]:
        with self._session() as s:
            return s.query(Document).filter(Document.id == document_id).first()

    def list(self, limit: int = 50, offset: int = 0, status: Optional[str] = None) -> tuple[List[Document], int]:
        """Возвращает (documents, total_count) с пагинацией."""
        with self._session() as s:
            q = s.query(Document)
            if status:
                q = q.filter(Document.status == status)
            total = q.count()
            docs = q.order_by(Document.created_at.desc()).limit(limit).offset(offset).all()
            return docs, total

    def upsert(self, doc_id: str, data: Dict[str, Any]) -> Document:
        """Создать или обновить документ."""
        with self._session() as s:
            doc = s.query(Document).filter(Document.id == doc_id).first()
            if not doc:
                doc = Document(id=doc_id)
                s.add(doc)
            for key, val in data.items():
                if hasattr(doc, key):
                    setattr(doc, key, val)
            doc.updated_at = datetime.now(timezone.utc)
            s.commit()
            s.refresh(doc)
            return doc

    def delete(self, doc_id: str) -> bool:
        with self._session() as s:
            doc = s.query(Document).filter(Document.id == doc_id).first()
            if doc:
                s.delete(doc)
                s.commit()
                return True
            return False

    def find_by_hash(self, file_hash: str) -> Optional[Document]:
        with self._session() as s:
            return s.query(Document).filter(Document.file_hash == file_hash).first()

    def count_by_status(self) -> Dict[str, int]:
        with self._session() as s:
            from sqlalchemy import func
            rows = s.query(Document.status, func.count(Document.id)).group_by(Document.status).all()
            return {r[0] or "unknown": r[1] for r in rows}

    def migrate_from_config_store(self):
        """Перенос данных из config_store в SQL (однократно)."""
        from src.api.services.config_store import config_store
        old = config_store.get_all("documents") or {}
        count = 0
        for doc_id, data in old.items():
            if not isinstance(data, dict):
                continue
            self.upsert(doc_id, {
                "filename": data.get("filename", "unknown"),
                "file_type": data.get("file_type", ""),
                "file_size": data.get("file_size", 0),
                "file_hash": data.get("file_hash", ""),
                "status": data.get("status", "pending"),
                "progress": data.get("progress", 0),
                "error": data.get("error", ""),
                "chunks_count": data.get("chunks_count", 0),
                "version": data.get("version", 1),
                "group_ids": json.dumps(data.get("group_ids", [])),
                "uploaded_by": data.get("uploaded_by"),
            })
            count += 1
        return count


# Глобальный экземпляр (ленивая инициализация)
_doc_repo: Optional[DocumentRepository] = None


def get_doc_repo() -> DocumentRepository:
    global _doc_repo
    if _doc_repo is None:
        from src.config import get_settings
        settings = get_settings()
        _doc_repo = DocumentRepository(settings.KAG_DB_URL)
    return _doc_repo
