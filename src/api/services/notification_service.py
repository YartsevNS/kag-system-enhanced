"""
Admin notification service.

Simple log-based notification system that stores events in the database.
Notifications are created by other services (web watcher, folder watcher, etc.)
and consumed by the admin UI / API.
"""

from typing import List, Optional
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from src.database.monitoring_models import Notification


def create_notification(
    db: Session,
    type: str,
    message: str,
) -> Notification:
    """
    Create a new notification record.

    Args:
        db: SQLAlchemy session.
        type: Notification type (document_updated, web_changed, file_detected).
        message: Human-readable message.

    Returns:
        The created Notification object.
    """
    notification = Notification(
        type=type,
        message=message,
        read=False,
        created_at=datetime.now(timezone.utc),
    )
    db.add(notification)
    db.commit()
    db.refresh(notification)
    logger.debug(f"Notification created: [{type}] {message[:80]}")
    return notification


def list_notifications(
    db: Session,
    limit: int = 100,
    unread_only: bool = False,
) -> List[Notification]:
    """
    List notifications, newest first.

    Args:
        db: SQLAlchemy session.
        limit: Maximum number of results.
        unread_only: If True, return only unread notifications.

    Returns:
        List of Notification objects.
    """
    query = db.query(Notification)
    if unread_only:
        query = query.filter(Notification.read == False)  # noqa: E712
    return query.order_by(Notification.created_at.desc()).limit(limit).all()


def mark_read(db: Session, notification_id: str) -> Optional[Notification]:
    """
    Mark a single notification as read.

    Args:
        db: SQLAlchemy session.
        notification_id: ID of the notification.

    Returns:
        Updated Notification or None if not found.
    """
    notification = db.query(Notification).filter(
        Notification.id == notification_id
    ).first()
    if notification:
        notification.read = True
        db.commit()
        db.refresh(notification)
    return notification


def mark_all_read(db: Session) -> int:
    """
    Mark all notifications as read.

    Args:
        db: SQLAlchemy session.

    Returns:
        Number of notifications updated.
    """
    count = (
        db.query(Notification)
        .filter(Notification.read == False)  # noqa: E712
        .update({"read": True})
    )
    db.commit()
    logger.info(f"Marked {count} notifications as read")
    return count
