"""
SQLAlchemy models for User and Group management.

Implements:
- User model (table: users)
- Group model (table: groups)
- Many-to-many relationship via user_groups association table
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, String, Boolean, DateTime, ForeignKey, Table
from sqlalchemy.orm import relationship

from src.database.models import Base

# Association table for many-to-many User <-> Group
user_groups = Table(
    "user_groups",
    Base.metadata,
    Column("user_id", String, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("group_id", String, ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True),
)


class User(Base):
    """User model with admin flag, active status, and group memberships."""

    __tablename__ = "users"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    username = Column(String, unique=True, nullable=False)
    email = Column(String, nullable=True)
    hashed_password = Column(String, nullable=False)
    is_admin = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    groups = relationship("Group", secondary=user_groups, back_populates="users")


class Group(Base):
    """Group model for organizing users (roles, teams, etc.)."""

    __tablename__ = "groups"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, unique=True, nullable=False)
    description = Column(String, nullable=True)
    created_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    users = relationship("User", secondary=user_groups, back_populates="groups")
