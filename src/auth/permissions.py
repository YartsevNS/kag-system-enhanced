"""
Object-level permissions for documents.

Inspired by paperless-ngx + Django Guardian:
- Each document has an owner and group
- Permissions: view, edit, delete, share
- Users inherit permissions from group membership
- Admin users bypass all permission checks
"""

from enum import Enum
from typing import List, Optional
from dataclasses import dataclass

from sqlalchemy import Column, String, Boolean, DateTime, ForeignKey, Enum as SAEnum
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
import uuid

from src.database.models import Base


class Permission(str, Enum):
    VIEW = "view"
    EDIT = "edit"
    DELETE = "delete"
    SHARE = "share"


class DocumentPermission(Base):
    """Explicit permission grant for a user on a specific document."""
    __tablename__ = 'document_permissions'
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    document_id = Column(String, ForeignKey('documents.id'), nullable=False, index=True)
    user_id = Column(String, ForeignKey('users.id'), nullable=False, index=True)
    permission = Column(String, nullable=False)  # view/edit/delete/share
    granted_by = Column(String, ForeignKey('users.id'))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class DocumentAccessControl:
    """
    Checks whether a user can perform an action on a document.
    
    Permission resolution order:
    1. Admin users — full access to everything
    2. Document owner — full access (view, edit, delete, share)
    3. Explicit permission grant (DocumentPermission)
    4. Group-based: if document belongs to user's group → view access
    5. Deny by default
    """
    
    def __init__(self, db_session):
        self.db = db_session
    
    def can_view(self, user, document_record) -> bool:
        """Check if user can view the document."""
        if not user or not document_record:
            return False
        
        # Admin sees everything
        if getattr(user, 'is_admin', False):
            return True
        
        # Owner sees their documents
        if document_record.uploaded_by == user.id:
            return True
        
        # Check explicit permission
        if self._has_permission(user.id, document_record.document_id, Permission.VIEW):
            return True
        
        # Group-based: document tagged with user's group
        doc_groups = getattr(document_record, 'group_ids', []) or []
        user_groups = [g.id for g in getattr(user, 'groups', [])]
        if set(doc_groups) & set(user_groups):
            return True
        
        return False
    
    def can_edit(self, user, document_record) -> bool:
        """Check if user can edit/re-upload the document."""
        if not user:
            return False
        if getattr(user, 'is_admin', False):
            return True
        if document_record.uploaded_by == user.id:
            return True
        return self._has_permission(user.id, document_record.document_id, Permission.EDIT)
    
    def can_delete(self, user, document_record) -> bool:
        """Check if user can delete the document."""
        if not user:
            return False
        if getattr(user, 'is_admin', False):
            return True
        if document_record.uploaded_by == user.id:
            return True
        return self._has_permission(user.id, document_record.document_id, Permission.DELETE)
    
    def can_share(self, user, document_record) -> bool:
        """Check if user can share the document with other users/groups."""
        if not user:
            return False
        if getattr(user, 'is_admin', False):
            return True
        if document_record.uploaded_by == user.id:
            return True
        return self._has_permission(user.id, document_record.document_id, Permission.SHARE)
    
    def grant_permission(self, document_id: str, user_id: str, permission: Permission, granted_by: str):
        """Grant a permission to a user on a document."""
        existing = self.db.query(DocumentPermission).filter(
            DocumentPermission.document_id == document_id,
            DocumentPermission.user_id == user_id,
            DocumentPermission.permission == permission.value
        ).first()
        
        if existing:
            return existing
        
        perm = DocumentPermission(
            document_id=document_id,
            user_id=user_id,
            permission=permission.value,
            granted_by=granted_by
        )
        self.db.add(perm)
        self.db.commit()
        return perm
    
    def revoke_permission(self, document_id: str, user_id: str, permission: Permission):
        """Revoke a permission from a user on a document."""
        self.db.query(DocumentPermission).filter(
            DocumentPermission.document_id == document_id,
            DocumentPermission.user_id == user_id,
            DocumentPermission.permission == permission.value
        ).delete()
        self.db.commit()
    
    def get_permissions(self, document_id: str) -> List[DocumentPermission]:
        """Get all explicit permissions for a document."""
        return self.db.query(DocumentPermission).filter(
            DocumentPermission.document_id == document_id
        ).all()
    
    def _has_permission(self, user_id: str, document_id: str, permission: Permission) -> bool:
        """Check explicit permission grant."""
        return self.db.query(DocumentPermission).filter(
            DocumentPermission.document_id == document_id,
            DocumentPermission.user_id == user_id,
            DocumentPermission.permission == permission.value
        ).first() is not None


# FastAPI dependency for permission checking
from fastapi import Depends, HTTPException, status


async def require_view_permission(
    document_id: str,
    current_user = Depends("get_current_user"),
    db = Depends("get_db")
):
    """FastAPI dependency: requires view permission on document."""
    from src.api.services.document_service import document_service
    doc = document_service.get_document(document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    
    acl = DocumentAccessControl(db)
    if not acl.can_view(current_user, doc):
        raise HTTPException(status_code=403, detail="Access denied")
    return doc
