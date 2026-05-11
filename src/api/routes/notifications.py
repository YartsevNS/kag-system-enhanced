"""
API routes for admin notifications.

Endpoints:
- List notifications (with unread filter)
- Mark single notification as read
- Mark all notifications as read
"""

from typing import List, Optional

from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.database.session import get_db
from src.api.services.notification_service import (
    list_notifications,
    mark_read,
    mark_all_read,
)

router = APIRouter()


class NotificationResponse(BaseModel):
    id: str
    type: str
    message: str
    read: bool
    created_at: str


@router.get(
    "/",
    response_model=List[NotificationResponse],
    summary="List notifications",
)
def get_notifications(
    limit: int = Query(100, ge=1, le=500),
    unread_only: bool = Query(False),
    db: Session = Depends(get_db),
):
    """
    Get list of notifications, newest first.

    - **limit**: Maximum number of results (1-500).
    - **unread_only**: If true, return only unread notifications.
    """
    notifications = list_notifications(db, limit=limit, unread_only=unread_only)
    return [
        NotificationResponse(
            id=n.id,
            type=n.type,
            message=n.message,
            read=n.read,
            created_at=n.created_at.isoformat() if n.created_at else "",
        )
        for n in notifications
    ]


@router.post(
    "/{notification_id}/read",
    response_model=NotificationResponse,
    summary="Mark notification as read",
)
def mark_notification_read(
    notification_id: str,
    db: Session = Depends(get_db),
):
    """
    Mark a single notification as read.

    - **notification_id**: ID of the notification.
    """
    notification = mark_read(db, notification_id)
    if not notification:
        raise HTTPException(status_code=404, detail="Notification not found")

    return NotificationResponse(
        id=notification.id,
        type=notification.type,
        message=notification.message,
        read=notification.read,
        created_at=notification.created_at.isoformat() if notification.created_at else "",
    )


@router.post(
    "/read-all",
    summary="Mark all notifications as read",
)
def mark_all_notifications_read(
    db: Session = Depends(get_db),
):
    """Mark all unread notifications as read."""
    count = mark_all_read(db)
    return {"status": "ok", "count": count}
