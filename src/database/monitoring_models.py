"""
SQLAlchemy models for monitoring: web page watching, folder watching, notifications.

Tables:
- watched_urls: tracked URLs for content change detection
- watched_folders: tracked folders for filesystem monitoring
- notifications: admin notification log
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, String, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import relationship

from src.database.models import Base


class WatchedURL(Base):
    """URL being monitored for content changes."""

    __tablename__ = "watched_urls"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    url = Column(String, nullable=False, unique=True)
    group_id = Column(String, ForeignKey("groups.id"), nullable=True)
    last_hash = Column(String, nullable=True)
    last_checked = Column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    group = relationship("Group", backref="watched_urls", lazy="selectin")


class WatchedFolder(Base):
    """Folder being monitored for new/modified files."""

    __tablename__ = "watched_folders"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    path = Column(String, nullable=False, unique=True)
    group_id = Column(String, ForeignKey("groups.id"), nullable=True)
    recursive = Column(Boolean, default=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    group = relationship("Group", backref="watched_folders", lazy="selectin")


class Notification(Base):
    """Admin notification for system events."""

    __tablename__ = "notifications"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    type = Column(String, nullable=False)  # document_updated, web_changed, file_detected
    message = Column(String, nullable=False)
    read = Column(Boolean, default=False)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
