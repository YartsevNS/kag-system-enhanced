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
    uploaded_by = Column(String, ForeignKey("users.id"), nullable=True)
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
