"""
SQLAlchemy models for document tracking and versioning.

Implements:
- Document model (table: documents)
- DocumentVersion model (table: document_versions)
- Tracks document lifecycle from upload through processing
- SHA-256 hashing for content integrity verification
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, String, Integer, DateTime, ForeignKey
from sqlalchemy.orm import relationship

from src.database.models import Base


class Document(Base):
    """
    Core document record tracking metadata and status.

    Linked to groups for access control and users for audit.
    """

    __tablename__ = "documents"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    filename = Column(String, nullable=False)
    original_path = Column(String, nullable=False)
    file_hash = Column(String, nullable=False)  # SHA-256
    mime_type = Column(String)
    file_size = Column(Integer)
    status = Column(String, default="processing")
    group_id = Column(String, ForeignKey("groups.id"))
    uploaded_by = Column(String, ForeignKey("users.id"))
    created_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    versions = relationship("DocumentVersion", back_populates="document")


class DocumentVersion(Base):
    """
    Version history for documents.
    
    Each version records the hash and path of a specific revision,
    allowing rollback and audit trail.
    """

    __tablename__ = "document_versions"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    document_id = Column(String, ForeignKey("documents.id"))
    version_number = Column(Integer)
    file_hash = Column(String, nullable=False)
    original_path = Column(String)
    change_description = Column(String)
    created_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    document = relationship("Document", back_populates="versions")
