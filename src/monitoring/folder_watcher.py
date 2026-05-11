"""
Folder monitoring service using watchdog.

Watches folders for new/modified files, computes SHA-256 hash,
and triggers document ingestion via callback.

Features:
- Debounce (5 seconds) to avoid duplicate events
- Recursive/non-recursive watching
- SHA-256 hashing for file identity
- Notification integration
"""

import hashlib
import threading
import time
from pathlib import Path
from typing import Optional, Callable, Dict, Any
from datetime import datetime, timezone

from loguru import logger
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from sqlalchemy.orm import Session


class FolderWatcherHandler(FileSystemEventHandler):
    """
    Watchdog event handler with debounced file processing.
    """

    def __init__(
        self,
        folder_path: str,
        callback: Optional[Callable] = None,
        debounce_seconds: float = 5.0,
        db_factory: Optional[Callable[[], Session]] = None,
    ):
        super().__init__()
        self.folder_path = folder_path
        self.callback = callback
        self.debounce_seconds = debounce_seconds
        self.db_factory = db_factory
        self._pending: Dict[str, float] = {}  # path -> first seen time
        self._lock = threading.Lock()

    def on_created(self, event):
        if not event.is_directory:
            self._handle_event(event.src_path, "created")

    def on_modified(self, event):
        if not event.is_directory:
            self._handle_event(event.src_path, "modified")

    def _handle_event(self, file_path: str, event_type: str):
        """Debounced event handler."""
        now = time.time()
        with self._lock:
            first_seen = self._pending.get(file_path)
            if first_seen is not None and (now - first_seen) < self.debounce_seconds:
                # Still within debounce window; update timestamp
                self._pending[file_path] = now
                return
            self._pending[file_path] = now

        # Schedule processing after debounce
        threading.Timer(
            self.debounce_seconds,
            self._process_file,
            args=[file_path, event_type],
        ).start()

    def _process_file(self, file_path: str, event_type: str):
        """Process a file after debounce period."""
        with self._lock:
            last_seen = self._pending.pop(file_path, None)
            if last_seen is None:
                return  # Already processed
            if time.time() - last_seen < self.debounce_seconds - 0.1:
                return  # Another event arrived; skip this one

        file_path_obj = Path(file_path)
        if not file_path_obj.exists():
            logger.debug(f"File no longer exists: {file_path}")
            return

        try:
            file_hash = self._compute_hash(file_path)
        except Exception as e:
            logger.error(f"Failed to hash {file_path}: {e}")
            return

        logger.info(f"File {event_type}: {file_path} (SHA-256: {file_hash[:12]}...)")

        # Trigger callback for document ingestion
        if self.callback:
            try:
                self.callback(file_path)
            except Exception as e:
                logger.error(f"Callback error for {file_path}: {e}")

        # Create notification
        if self.db_factory:
            try:
                db = next(self.db_factory())
                from src.api.services.notification_service import create_notification
                create_notification(
                    db=db,
                    type="file_detected",
                    message=f"File {event_type}: {file_path}",
                )
                db.close()
            except Exception as e:
                logger.warning(f"Failed to create notification: {e}")

    @staticmethod
    def _compute_hash(file_path: str) -> str:
        """Compute SHA-256 hash of a file."""
        sha256 = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha256.update(chunk)
        return sha256.hexdigest()


class FolderWatcher:
    """
    Monitors folders for new/modified files and triggers ingestion.

    Usage:
        watcher = FolderWatcher()
        watcher.add_folder("/path/to/docs", group_id="g1")
        watcher.start()
        # ... later
        watcher.stop()
    """

    def __init__(self, ingestion_callback: Optional[Callable] = None):
        """
        Initialize folder watcher.

        Args:
            ingestion_callback: Callable(file_path) called when new/modified file is detected.
        """
        self._observer: Optional[Observer] = None
        self._handlers: Dict[str, FolderWatcherHandler] = {}
        self._ingestion_callback = ingestion_callback
        self._running = False
        self._lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        return self._running

    def set_ingestion_callback(self, callback: Callable):
        """Set or replace the ingestion callback."""
        self._ingestion_callback = callback

    def add_folder(
        self,
        folder_path: str,
        recursive: bool = True,
        db_factory: Optional[Callable] = None,
    ):
        """
        Add a folder to watch.

        Args:
            folder_path: Absolute path to folder.
            recursive: Watch subdirectories recursively.
            db_factory: Callable returning a SQLAlchemy session for notifications.
        """
        folder_path = str(Path(folder_path).resolve())
        if folder_path in self._handlers:
            logger.warning(f"Already watching: {folder_path}")
            return

        handler = FolderWatcherHandler(
            folder_path=folder_path,
            callback=self._ingestion_callback,
            debounce_seconds=5.0,
            db_factory=db_factory,
        )
        self._handlers[folder_path] = handler

        if self._observer and self._running:
            self._observer.schedule(handler, folder_path, recursive=recursive)
            logger.info(f"Added watch: {folder_path} (recursive={recursive})")

    def remove_folder(self, folder_path: str):
        """
        Stop watching a folder.

        Args:
            folder_path: Folder path to remove.
        """
        folder_path = str(Path(folder_path).resolve())
        handler = self._handlers.pop(folder_path, None)
        if handler and self._observer:
            self._observer.unschedule(handler)
            logger.info(f"Stopped watching: {folder_path}")

    def start(self):
        """Start the watchdog observer."""
        if self._running:
            logger.warning("FolderWatcher already running")
            return

        with self._lock:
            if self._running:
                return
            self._observer = Observer()
            for folder_path, handler in self._handlers.items():
                self._observer.schedule(handler, folder_path, recursive=True)
            self._observer.start()
            self._running = True
            logger.info(
                f"FolderWatcher started, watching {len(self._handlers)} folders"
            )

    def stop(self):
        """Stop the watchdog observer."""
        with self._lock:
            if self._observer:
                self._observer.stop()
                self._observer.join(timeout=10)
                self._observer = None
            self._running = False
            logger.info("FolderWatcher stopped")

    def reload_folders(self, db: Session):
        """
        Reload watched folders from the database and sync with observer.

        Args:
            db: SQLAlchemy session.
        """
        from src.database.monitoring_models import WatchedFolder

        records = db.query(WatchedFolder).all()

        # Remove folders no longer in DB
        db_paths = {r.path for r in records}
        for path in list(self._handlers.keys()):
            if path not in db_paths:
                self.remove_folder(path)

        # Add new folders from DB
        for record in records:
            if record.path not in self._handlers:
                self.add_folder(
                    folder_path=record.path,
                    recursive=record.recursive,
                )


# Global singleton
folder_watcher = FolderWatcher()
