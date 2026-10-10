"""SQLAlchemy models for document tracking and versioning."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, Text, Float, Boolean
from sqlalchemy.orm import relationship

from src.database.models import Base


class Document(Base):
    __tablename__ = "documents"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    filename = Column(String, nullable=False, index=True)
    file_type = Column(String, default="")
    file_size = Column(Integer, default=0)
    file_hash = Column(String, nullable=False, index=True)  # SHA-256
    mime_type = Column(String)
    status = Column(String, default="pending", index=True)
    progress = Column(Float, default=0.0)
    error = Column(Text, default="")
    chunks_count = Column(Integer, default=0)
    version = Column(Integer, default=1)
    is_active = Column(Boolean, default=True)
    delayed_until = Column(DateTime(timezone=True), nullable=True)
    group_ids = Column(Text, default="[]")  # JSON list
    # ── Права доступа (ACL) ──────────────────────────────────────────────
    # visibility: public | restricted («всем запрещено, избранным разрешено»)
    # allow_*/deny_* — JSON списки id групп/пользователей. Чанки наследуют
    # права при индексации (payload access). Настраивается при загрузке/
    # редактировании документа (страница «Документы»).
    visibility = Column(String, default="public")
    allow_group_ids = Column(Text, default="[]")  # JSON list
    deny_group_ids = Column(Text, default="[]")   # JSON list
    allow_user_ids = Column(Text, default="[]")   # JSON list
    deny_user_ids = Column(Text, default="[]")    # JSON list
    uploaded_by = Column(String, ForeignKey("users.id"), nullable=True)
    # Классификация (заполняется document_analyzer / type_watchdog)
    document_type = Column(String, default="")
    # Коллекция векторов: '' — основная (kag_documents), 'news' — новости монитора (kag_news).
    # Явный признак нужен потому, что раньше маршрут определялся по ВИДУ документа (type == 'news'),
    # а вид — это разметка: при смене словаря маршрут молча менялся, и новости уезжали в общую
    # коллекцию к ГОСТам. Маршрут — свойство ИСТОЧНИКА, а не вида (утверждено 10.10.2026).
    collection = Column(String, default="")
    recognized_title = Column(String, default="")
    summary = Column(Text, default="")
    topics = Column(Text, default="[]")  # JSON list
    domain = Column(String(32), default="")  # ЛЕГАСИ: домен прежней схемы (infosec/accounting/legal/universal)
    # Темы (рубрики) — вторая ось описания документа: о чём он. JSON-список кодов словаря
    # (src/indexing/document_topics.py), многозначный. Мягкая по умолчанию: в поиске по ней
    # фильтровать нельзя (стража — tests/test_document_topics.py).
    rubrics = Column(Text, default="[]")
    # Происхождение разметки: JSON-словарь по полям (тема, каждый фасет) — источник значения
    # (model/manual), уверенность модели и пометка «правка человека». Хранить это рядом со значением
    # обязательно: иначе правку специалиста затрёт следующий прогон модели, и никто не заметит.
    label_provenance = Column(Text, default="{}")
    # Издатель (кто выпустил документ): Росстандарт, ФСТЭК, Банк России, организация…
    issuer = Column(String, default="")
    # Фасеты — независимые перечни: предмет защиты, этап, нормативная сила, гриф.
    # Хранятся JSON-объектом (значение может быть списком — фасет многозначный).
    facets = Column(Text, default="{}")
    # Версия схемы, по которой документ размечен: при смене словаря видно, что переразметить.
    schema_version = Column(String, default="")
    # Версионность и контекст
    previous_hash = Column(String, default="")
    # Прежняя редакция документа (идентификатор): нужен для связи редакций в графе
    previous_document_id = Column(String, default="")
    original_text = Column(Text, default=None)
    source_metadata = Column(Text, default=None)  # JSON dict
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    versions = relationship("DocumentVersion", back_populates="document", cascade="all, delete-orphan")


class DocumentVersion(Base):
    __tablename__ = "document_versions"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    document_id = Column(String, ForeignKey("documents.id"))
    version_number = Column(Integer)
    file_hash = Column(String, nullable=False)
    original_path = Column(String)
    change_description = Column(String)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    document = relationship("Document", back_populates="versions")
