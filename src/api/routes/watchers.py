"""
API routes for web page watchers and folder watchers.

Endpoints:
- Watched URLs: list, add, remove, trigger check
- Watched Folders: list, add, remove
"""

from typing import Optional, List
from pathlib import Path

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from loguru import logger
from sqlalchemy.orm import Session

from src.database.session import get_db
from src.database.monitoring_models import WatchedURL, WatchedFolder
from src.monitoring.web_watcher import web_watcher
from src.monitoring.folder_watcher import folder_watcher

router = APIRouter()


# ──────────────────────────────────────────────
# Pydantic schemas
# ──────────────────────────────────────────────

class WatchedURLResponse(BaseModel):
    id: str
    url: str
    group_id: Optional[str] = None
    last_hash: Optional[str] = None
    last_checked: Optional[str] = None
    created_at: str


class AddURLRequest(BaseModel):
    url: str = Field(..., description="URL to watch")
    group_id: Optional[str] = Field(None, description="Group ID for access control")


class WatchedFolderResponse(BaseModel):
    id: str
    path: str
    group_id: Optional[str] = None
    recursive: bool = True
    created_at: str


class AddFolderRequest(BaseModel):
    path: str = Field(..., description="Absolute path to folder")
    group_id: Optional[str] = Field(None, description="Group ID for access control")
    recursive: bool = Field(True, description="Watch subdirectories recursively")


class ChangeInfo(BaseModel):
    url: str
    old_hash: Optional[str] = None
    new_hash: str
    preview: Optional[str] = None
    checked_at: str


# ──────────────────────────────────────────────
# URL Watcher endpoints
# ──────────────────────────────────────────────

@router.get(
    "/urls",
    response_model=List[WatchedURLResponse],
    summary="List watched URLs",
)
def list_watched_urls(
    db: Session = Depends(get_db),
):
    """Get all watched URLs and their last-check status."""
    records = db.query(WatchedURL).order_by(WatchedURL.created_at.desc()).all()
    return [
        WatchedURLResponse(
            id=r.id,
            url=r.url,
            group_id=r.group_id,
            last_hash=r.last_hash,
            last_checked=r.last_checked.isoformat() if r.last_checked else None,
            created_at=r.created_at.isoformat() if r.created_at else "",
        )
        for r in records
    ]


@router.post(
    "/urls",
    response_model=WatchedURLResponse,
    summary="Add URL to watch list",
)
def add_watched_url(
    body: AddURLRequest,
    db: Session = Depends(get_db),
):
    """Add a new URL to the monitoring list."""
    # Check duplicate
    existing = db.query(WatchedURL).filter(WatchedURL.url == body.url).first()
    if existing:
        raise HTTPException(status_code=409, detail="URL already watched")

    record = WatchedURL(
        url=body.url,
        group_id=body.group_id,
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    logger.info(f"Added URL to watch: {body.url}")
    return WatchedURLResponse(
        id=record.id,
        url=record.url,
        group_id=record.group_id,
        last_hash=record.last_hash,
        last_checked=None,
        created_at=record.created_at.isoformat() if record.created_at else "",
    )


@router.delete(
    "/urls/{url_id}",
    summary="Remove URL from watch list",
)
def remove_watched_url(
    url_id: str,
    db: Session = Depends(get_db),
):
    """Stop watching a URL."""
    record = db.query(WatchedURL).filter(WatchedURL.id == url_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="URL not found")

    db.delete(record)
    db.commit()
    return {"status": "ok", "id": url_id}


@router.post(
    "/urls/check",
    response_model=List[ChangeInfo],
    summary="Check all watched URLs for changes",
)
async def check_all_urls(
    db: Session = Depends(get_db),
):
    """Trigger an immediate check of all watched URLs."""
    try:
        changes = await web_watcher.check_all(db)
        return [ChangeInfo(**c) for c in changes]
    except Exception as e:
        logger.error(f"Error checking URLs: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/urls/{url_id}/check",
    response_model=Optional[ChangeInfo],
    summary="Check a single URL for changes",
)
async def check_single_url(
    url_id: str,
    db: Session = Depends(get_db),
):
    """Trigger an immediate check of a single watched URL."""
    record = db.query(WatchedURL).filter(WatchedURL.id == url_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="URL not found")

    try:
        change = await web_watcher.check_page(record.url, db)
        if change:
            return ChangeInfo(**change)
        return None
    except Exception as e:
        logger.error(f"Error checking {record.url}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ──────────────────────────────────────────────
# Folder Watcher endpoints
# ──────────────────────────────────────────────

@router.get(
    "/folders",
    response_model=List[WatchedFolderResponse],
    summary="List watched folders",
)
def list_watched_folders(
    db: Session = Depends(get_db),
):
    """Get all watched folders."""
    records = db.query(WatchedFolder).order_by(WatchedFolder.created_at.desc()).all()
    return [
        WatchedFolderResponse(
            id=r.id,
            path=r.path,
            group_id=r.group_id,
            recursive=r.recursive,
            created_at=r.created_at.isoformat() if r.created_at else "",
        )
        for r in records
    ]


@router.post(
    "/folders",
    response_model=WatchedFolderResponse,
    summary="Add folder to watch list",
)
def add_watched_folder(
    body: AddFolderRequest,
    db: Session = Depends(get_db),
):
    """Add a new folder to monitoring."""
    # Validate path exists
    folder_path = str(Path(body.path).resolve())
    if not Path(folder_path).is_dir():
        raise HTTPException(status_code=400, detail="Folder does not exist")

    # Check duplicate
    existing = db.query(WatchedFolder).filter(WatchedFolder.path == folder_path).first()
    if existing:
        raise HTTPException(status_code=409, detail="Folder already watched")

    record = WatchedFolder(
        path=folder_path,
        group_id=body.group_id,
        recursive=body.recursive,
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    # Add to live watcher if running
    if folder_watcher.is_running:
        folder_watcher.add_folder(
            folder_path=folder_path,
            recursive=body.recursive,
            db_factory=get_db,
        )

    logger.info(f"Added folder to watch: {folder_path}")
    return WatchedFolderResponse(
        id=record.id,
        path=record.path,
        group_id=record.group_id,
        recursive=record.recursive,
        created_at=record.created_at.isoformat() if record.created_at else "",
    )


@router.delete(
    "/folders/{folder_id}",
    summary="Remove folder from watch list",
)
def remove_watched_folder(
    folder_id: str,
    db: Session = Depends(get_db),
):
    """Stop watching a folder."""
    record = db.query(WatchedFolder).filter(WatchedFolder.id == folder_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="Folder not found")

    folder_path = record.path
    db.delete(record)
    db.commit()

    # Remove from live watcher if running
    if folder_watcher.is_running:
        folder_watcher.remove_folder(folder_path)

    logger.info(f"Removed folder from watch: {folder_path}")
    return {"status": "ok", "id": folder_id}
