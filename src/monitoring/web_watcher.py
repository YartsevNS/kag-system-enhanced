"""
Web page monitoring service.

Watches URLs for content changes by:
1. Fetching URL content via httpx
2. Computing SHA-256 hash of response body
3. Comparing against stored hash from last check
4. Triggering notifications on detected changes
"""

import hashlib
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone

import httpx
from loguru import logger
from sqlalchemy.orm import Session

from src.database.session import get_db
from src.database.monitoring_models import WatchedURL


class WebWatcher:
    """
    Monitors web pages for content changes using SHA-256 comparison.

    Usage:
        watcher = WebWatcher()
        changes = await watcher.check_all()
    """

    def __init__(self):
        self._http_client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Lazy-init HTTP client for connection reuse."""
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=30.0,
                follow_redirects=True,
                headers={"User-Agent": "KAG-WebWatcher/1.0"},
            )
        return self._http_client

    async def close(self):
        """Close the HTTP client."""
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

    @staticmethod
    def _compute_hash(content: bytes) -> str:
        """Compute SHA-256 hex digest of content."""
        return hashlib.sha256(content).hexdigest()

    async def check_page(self, url: str, db: Session) -> Optional[Dict[str, Any]]:
        """
        Check a single URL for changes.

        Args:
            url: The URL to check.
            db: SQLAlchemy session.

        Returns:
            None if content unchanged, or dict with change details:
                {url, old_hash, new_hash, preview, checked_at}
        """
        # Look up stored record
        record = db.query(WatchedURL).filter(WatchedURL.url == url).first()
        if not record:
            logger.warning(f"URL not in watch list: {url}")
            return None

        try:
            client = await self._get_client()
            response = await client.get(url)
            response.raise_for_status()
        except Exception as e:
            logger.error(f"Failed to fetch {url}: {e}")
            return None

        content = response.content
        new_hash = self._compute_hash(content)
        old_hash = record.last_hash

        # Update last_checked regardless
        record.last_checked = datetime.now(timezone.utc)

        if old_hash == new_hash:
            db.commit()
            return None  # No change

        # Update stored hash
        record.last_hash = new_hash
        db.commit()

        # Build preview: first 500 chars of text content
        preview = None
        try:
            text = content.decode("utf-8", errors="replace")[:500]
            # Strip HTML tags for cleaner preview
            import re
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text).strip()
            preview = text[:300]
        except Exception:
            preview = f"<binary content, {len(content)} bytes>"

        change_info = {
            "url": url,
            "old_hash": old_hash,
            "new_hash": new_hash,
            "preview": preview,
            "checked_at": record.last_checked.isoformat(),
        }
        logger.info(f"Change detected for {url}")
        return change_info

    async def check_all(self, db: Session) -> List[Dict[str, Any]]:
        """
        Check all watched URLs and return list of changes.

        Args:
            db: SQLAlchemy session.

        Returns:
            List of change dicts (one per changed URL).
        """
        records = db.query(WatchedURL).all()
        if not records:
            logger.debug("No URLs to watch")
            return []

        changes = []
        for record in records:
            try:
                change = await self.check_page(record.url, db)
                if change:
                    changes.append(change)
                    self._notify(change, db)
            except Exception as e:
                logger.error(f"Error checking {record.url}: {e}")

        logger.info(f"Checked {len(records)} URLs, {len(changes)} changed")
        return changes

    def _notify(self, change: Dict[str, Any], db: Session):
        """Create a notification for a detected change."""
        try:
            from src.api.services.notification_service import create_notification
            create_notification(
                db=db,
                type="web_changed",
                message=f"Web page changed: {change['url']} "
                        f"(preview: {change.get('preview', 'N/A')[:100]}...)",
            )
        except Exception as e:
            logger.warning(f"Failed to create notification: {e}")


# Global singleton
web_watcher = WebWatcher()
