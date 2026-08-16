"""Репозиторий для работы с документами через SQL (вместо config_store)."""

import json
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session

from src.database.document_models import Document, Base

# Поля, хранящиеся в SQL как JSON-строка.
_JSON_FIELDS = {"group_ids", "topics", "source_metadata"}
# Поля, хранящиеся как datetime (при записи допускаем строку ISO либо datetime).
_DATETIME_FIELDS = {"created_at", "updated_at", "delayed_until"}


def _serialize_value(key: str, val: Any) -> Any:
    """Привести значение к типу SQL-колонки."""
    if val is None:
        return None
    if key in _JSON_FIELDS:
        if isinstance(val, str):
            return val  # уже сериализовано
        return json.dumps(val, ensure_ascii=False)
    if key in _DATETIME_FIELDS:
        if isinstance(val, datetime):
            return val
        if isinstance(val, str):
            try:
                dt = datetime.fromisoformat(val)
            except ValueError:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        return None
    return val


def _deserialize_value(key: str, val: Any) -> Any:
    """Привести значение из SQL-колонки к dict-представлению (как в config_store)."""
    if val is None:
        return None
    if key in _JSON_FIELDS:
        if isinstance(val, str):
            try:
                return json.loads(val)
            except Exception:
                return None
        return val
    if key in _DATETIME_FIELDS:
        if isinstance(val, datetime):
            return val.isoformat()
        return val
    return val


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
        """Создать или обновить документ. Список/dict → JSON, строки ISO → datetime."""
        with self._session() as s:
            doc = s.query(Document).filter(Document.id == doc_id).first()
            if not doc:
                doc = Document(id=doc_id)
                s.add(doc)
            for key, val in data.items():
                if hasattr(doc, key):
                    setattr(doc, key, _serialize_value(key, val))
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

    # ── Dict-совместимый интерфейс (замена config_store "documents") ────

    @staticmethod
    def to_dict(doc: Document) -> Dict[str, Any]:
        """Преобразовать ORM-объект в dict в формате, как раньше хранил config_store."""
        cols = [c.name for c in Document.__table__.columns]
        result = {}
        for name in cols:
            if name == "id":
                continue
            result[name] = _deserialize_value(name, getattr(doc, name, None))
        return result

    def get_dict(self, document_id: str) -> Optional[Dict[str, Any]]:
        doc = self.get(document_id)
        if not doc:
            return None
        return self.to_dict(doc)

    def get_all(self) -> Dict[str, Dict[str, Any]]:
        """Все документы как {id: dict} (аналог config_store.get_all("documents"))."""
        with self._session() as s:
            docs = s.query(Document).all()
            return {d.id: self.to_dict(d) for d in docs}

    def migrate_from_config_store(self):
        """Перенос данных из config_store в SQL (однократно)."""
        from src.api.services.config_store import config_store
        old = config_store.get_all("documents") or {}
        count = 0
        for doc_id, data in old.items():
            if not isinstance(data, dict):
                continue
            self.upsert(doc_id, data)
            count += 1
        return count


# Глобальный экземпляр (ленивая инициализация)
_doc_repo: Optional[DocumentRepository] = None


def get_doc_repo() -> DocumentRepository:
    global _doc_repo
    if _doc_repo is None:
        from src.config import get_settings
        settings = get_settings()
        db_url = settings.KAG_DB_URL or settings.DATABASE_URL
        _doc_repo = DocumentRepository(db_url)
    return _doc_repo


def reset_doc_repo() -> None:
    """Сбросить кэш репозитория (после смены KAG_DB_URL)."""
    global _doc_repo
    _doc_repo = None
