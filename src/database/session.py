"""
Database session management for FastAPI.

Provides a `get_db` dependency that yields SQLAlchemy sessions
and ensures tables are created.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from src.database.models import Base
from src.database.user_models import User, Group  # noqa: F401 - register models
from src.database.document_models import Document, DocumentVersion  # noqa: F401 - register models
from src.config import get_settings

_engine = None
_SessionLocal = None


def _get_engine():
    """Lazily create the SQLAlchemy engine."""
    global _engine, _SessionLocal
    if _engine is None:
        settings = get_settings()
        connect_args = {}
        if "sqlite" in settings.DATABASE_URL:
            connect_args["check_same_thread"] = False
        _engine = create_engine(
            settings.DATABASE_URL,
            connect_args=connect_args,
            pool_pre_ping=True,
        )
        _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
        Base.metadata.create_all(bind=_engine)
    return _engine


def get_db() -> Session:
    """
    FastAPI dependency that yields a SQLAlchemy session.

    Usage:
        @router.get("/items")
        def list_items(db: Session = Depends(get_db)):
            ...
    """
    _get_engine()
    db = _SessionLocal()
    try:
        yield db
    finally:
        db.close()
