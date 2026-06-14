"""
Tests for monitoring features: web watcher, folder watcher, notifications.

Requires a running FastAPI app (uses TestClient).
"""

import os
import tempfile
from unittest.mock import patch, Mock, AsyncMock

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.database.session import get_db
from src.database.models import Base
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


# ──────────────────────────────────────────────
# In-memory SQLite for tests
# ──────────────────────────────────────────────

@pytest.fixture(scope="function")
def test_db():
    """Create an in-memory SQLite database for testing."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client(test_db):
    """Test client with in-memory DB override."""
    def override_get_db():
        try:
            yield test_db
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


# ──────────────────────────────────────────────
# A. Web page watcher tests
# ──────────────────────────────────────────────

class TestWebWatcherAPI:
    """Tests for URL watching endpoints."""

    def test_list_empty(self, client):
        """List watched URLs when none exist."""
        response = client.get("/api/v1/watchers/urls")
        assert response.status_code == 200
        assert response.json() == []

    def test_add_url(self, client):
        """Add a URL to watch list."""
        response = client.post(
            "/api/v1/watchers/urls",
            json={"url": "https://example.com", "group_id": None},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["url"] == "https://example.com"
        assert "id" in data

    def test_add_duplicate_url(self, client):
        """Adding duplicate URL returns 409."""
        client.post("/api/v1/watchers/urls", json={"url": "https://example.com"})
        response = client.post("/api/v1/watchers/urls", json={"url": "https://example.com"})
        assert response.status_code == 409

    def test_list_urls(self, client):
        """List watched URLs after adding."""
        client.post("/api/v1/watchers/urls", json={"url": "https://example.com"})
        client.post("/api/v1/watchers/urls", json={"url": "https://test.org"})

        response = client.get("/api/v1/watchers/urls")
        assert response.status_code == 200
        assert len(response.json()) == 2

    def test_delete_url(self, client):
        """Remove a watched URL."""
        add_resp = client.post(
            "/api/v1/watchers/urls", json={"url": "https://example.com"}
        )
        url_id = add_resp.json()["id"]

        response = client.delete(f"/api/v1/watchers/urls/{url_id}")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

        # Verify it's gone
        list_resp = client.get("/api/v1/watchers/urls")
        assert len(list_resp.json()) == 0

    def test_delete_nonexistent_url(self, client):
        """Delete non-existent URL returns 404."""
        response = client.delete("/api/v1/watchers/urls/nonexistent-id")
        assert response.status_code == 404


class TestWebWatcherService:
    """Tests for the WebWatcher service directly."""

    @pytest.mark.asyncio
    async def test_compute_hash(self):
        """SHA-256 computation works."""
        from src.monitoring.web_watcher import WebWatcher
        h = WebWatcher._compute_hash(b"hello")
        assert len(h) == 64
        assert h == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"

    @pytest.mark.asyncio
    async def test_check_page_no_record(self, test_db):
        """check_page returns None when URL is not in DB."""
        from src.monitoring.web_watcher import web_watcher
        result = await web_watcher.check_page("https://not-in-db.com", test_db)
        assert result is None

    @pytest.mark.asyncio
    async def test_check_page_unchanged(self, test_db):
        """check_page returns None when content hasn't changed."""
        from src.monitoring.web_watcher import web_watcher, WatchedURL
        import hashlib

        content = b"test content"
        test_hash = hashlib.sha256(content).hexdigest()

        record = WatchedURL(
            url="https://example.com",
            last_hash=test_hash,
        )
        test_db.add(record)
        test_db.commit()

        # Mock HTTP response to return same content
        mock_response = Mock()
        mock_response.content = content
        mock_response.raise_for_status = Mock()

        with patch.object(web_watcher, "_get_client") as mock_client:
            mock_client.return_value.get = AsyncMock(return_value=mock_response)
            result = await web_watcher.check_page("https://example.com", test_db)

        assert result is None

    @pytest.mark.asyncio
    async def test_check_page_changed(self, test_db):
        """check_page returns change info when content changed."""
        from src.monitoring.web_watcher import web_watcher, WatchedURL
        import hashlib

        old_content = b"old content"
        old_hash = hashlib.sha256(old_content).hexdigest()

        record = WatchedURL(
            url="https://example.com",
            last_hash=old_hash,
        )
        test_db.add(record)
        test_db.commit()

        new_content = b"new content"
        mock_response = Mock()
        mock_response.content = new_content
        mock_response.raise_for_status = Mock()

        with patch.object(web_watcher, "_get_client") as mock_client:
            mock_client.return_value.get = AsyncMock(return_value=mock_response)
            result = await web_watcher.check_page("https://example.com", test_db)

        assert result is not None
        assert result["url"] == "https://example.com"
        assert result["old_hash"] == old_hash
        assert "new_hash" in result


# ──────────────────────────────────────────────
# B. Folder watcher tests
# ──────────────────────────────────────────────

class TestFolderWatcherAPI:
    """Tests for folder watching endpoints."""

    def test_list_empty(self, client):
        """List watched folders when none exist."""
        response = client.get("/api/v1/watchers/folders")
        assert response.status_code == 200
        assert response.json() == []

    def test_add_folder(self, client):
        """Add a folder to watch list."""
        with tempfile.TemporaryDirectory() as tmpdir:
            response = client.post(
                "/api/v1/watchers/folders",
                json={"path": tmpdir, "group_id": None, "recursive": True},
            )
            assert response.status_code == 200
            data = response.json()
            assert "id" in data
            assert data["recursive"] is True

    def test_add_nonexistent_folder(self, client):
        """Adding non-existent folder returns 400."""
        response = client.post(
            "/api/v1/watchers/folders",
            json={"path": "/nonexistent/path/xyz", "group_id": None, "recursive": True},
        )
        assert response.status_code == 400

    def test_add_duplicate_folder(self, client):
        """Adding duplicate folder returns 409."""
        with tempfile.TemporaryDirectory() as tmpdir:
            client.post(
                "/api/v1/watchers/folders",
                json={"path": tmpdir, "recursive": True},
            )
            response = client.post(
                "/api/v1/watchers/folders",
                json={"path": tmpdir, "recursive": True},
            )
            assert response.status_code == 409

    def test_delete_folder(self, client):
        """Remove a watched folder."""
        with tempfile.TemporaryDirectory() as tmpdir:
            add_resp = client.post(
                "/api/v1/watchers/folders",
                json={"path": tmpdir, "recursive": True},
            )
            folder_id = add_resp.json()["id"]

            response = client.delete(f"/api/v1/watchers/folders/{folder_id}")
            assert response.status_code == 200

    def test_delete_nonexistent_folder(self, client):
        """Delete non-existent folder returns 404."""
        response = client.delete("/api/v1/watchers/folders/nonexistent-id")
        assert response.status_code == 404


class TestFolderWatcherService:
    """Tests for the FolderWatcher service."""

    def test_compute_hash(self):
        """SHA-256 computation for files works."""
        from src.monitoring.folder_watcher import FolderWatcherHandler
        import hashlib

        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"test file content")
            tmp_path = f.name

        try:
            file_hash = FolderWatcherHandler._compute_hash(tmp_path)
            expected = hashlib.sha256(b"test file content").hexdigest()
            assert file_hash == expected
        finally:
            os.unlink(tmp_path)

    def test_start_stop(self):
        """FolderWatcher can be started and stopped."""
        from src.monitoring.folder_watcher import FolderWatcher

        watcher = FolderWatcher()
        assert not watcher.is_running

        with tempfile.TemporaryDirectory() as tmpdir:
            watcher.add_folder(tmpdir, recursive=False)
            watcher.start()
            assert watcher.is_running
            watcher.stop()
            assert not watcher.is_running

    def test_double_start_safe(self):
        """Calling start() twice is safe."""
        from src.monitoring.folder_watcher import FolderWatcher

        watcher = FolderWatcher()
        with tempfile.TemporaryDirectory() as tmpdir:
            watcher.add_folder(tmpdir, recursive=False)
            watcher.start()
            watcher.start()  # Should log warning but not crash
            watcher.stop()

    def test_add_remove_folder_when_running(self):
        """Adding/removing folders while running works."""
        from src.monitoring.folder_watcher import FolderWatcher

        watcher = FolderWatcher()
        with tempfile.TemporaryDirectory() as tmpdir:
            watcher.add_folder(tmpdir, recursive=False)
            watcher.start()

            with tempfile.TemporaryDirectory() as tmpdir2:
                watcher.add_folder(tmpdir2, recursive=False)
                watcher.remove_folder(tmpdir2)

            watcher.stop()


# ──────────────────────────────────────────────
# C. Notification tests
# ──────────────────────────────────────────────

class TestNotificationAPI:
    """Tests for notification endpoints."""

    def test_list_empty(self, client):
        """List notifications when none exist."""
        response = client.get("/api/v1/notifications/")
        assert response.status_code == 200
        assert response.json() == []

    def test_list_after_creation(self, test_db, client):
        """List shows created notifications."""
        from src.api.services.notification_service import create_notification

        create_notification(test_db, "web_changed", "Test notification 1")
        create_notification(test_db, "file_detected", "Test notification 2")

        response = client.get("/api/v1/notifications/")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        # Newest first
        assert data[0]["message"] == "Test notification 2"
        assert data[0]["read"] is False

    def test_unread_only_filter(self, test_db, client):
        """Filtering by unread_only works."""
        from src.api.services.notification_service import create_notification

        n1 = create_notification(test_db, "web_changed", "Unread notif")
        n2 = create_notification(test_db, "file_detected", "Read notif")

        # Mark n2 as read
        from src.api.services.notification_service import mark_read
        mark_read(test_db, n2.id)

        response = client.get("/api/v1/notifications/?unread_only=true")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["id"] == n1.id

    def test_mark_read(self, test_db, client):
        """Mark a notification as read."""
        from src.api.services.notification_service import create_notification

        n = create_notification(test_db, "web_changed", "To be marked read")

        response = client.post(f"/api/v1/notifications/{n.id}/read")
        assert response.status_code == 200
        assert response.json()["read"] is True

    def test_mark_read_nonexistent(self, client):
        """Marking non-existent notification returns 404."""
        response = client.post("/api/v1/notifications/nonexistent/read")
        assert response.status_code == 404

    def test_mark_all_read(self, test_db, client):
        """Mark all notifications as read."""
        from src.api.services.notification_service import create_notification

        create_notification(test_db, "web_changed", "Notif 1")
        create_notification(test_db, "web_changed", "Notif 2")
        create_notification(test_db, "file_detected", "Notif 3")

        response = client.post("/api/v1/notifications/read-all")
        assert response.status_code == 200
        assert response.json()["count"] == 3

        # Verify all are read
        list_resp = client.get("/api/v1/notifications/")
        for n in list_resp.json():
            assert n["read"] is True


class TestNotificationService:
    """Tests for the notification service functions."""

    def test_create_notification(self, test_db):
        """create_notification stores in DB."""
        from src.api.services.notification_service import create_notification

        n = create_notification(test_db, "web_changed", "Page changed!")
        assert n.id is not None
        assert n.type == "web_changed"
        assert n.message == "Page changed!"
        assert n.read is False

    def test_list_notifications(self, test_db):
        """list_notifications returns newest first."""
        from src.api.services.notification_service import (
            create_notification,
            list_notifications,
        )

        create_notification(test_db, "web_changed", "First")
        create_notification(test_db, "file_detected", "Second")

        results = list_notifications(test_db, limit=10)
        assert len(results) == 2
        assert results[0].message == "Second"  # newest first
        assert results[1].message == "First"

    def test_list_unread_only(self, test_db):
        """list_notifications with unread_only filter."""
        from src.api.services.notification_service import (
            create_notification,
            list_notifications,
            mark_read,
        )

        create_notification(test_db, "web_changed", "Unread")
        n2 = create_notification(test_db, "web_changed", "Read")
        mark_read(test_db, n2.id)

        results = list_notifications(test_db, unread_only=True)
        assert len(results) == 1
        assert results[0].message == "Unread"

    def test_mark_read(self, test_db):
        """mark_read sets read=True."""
        from src.api.services.notification_service import create_notification, mark_read

        n = create_notification(test_db, "web_changed", "Test")
        assert n.read is False

        updated = mark_read(test_db, n.id)
        assert updated is not None
        assert updated.read is True

    def test_mark_read_nonexistent(self, test_db):
        """mark_read returns None for missing ID."""
        from src.api.services.notification_service import mark_read

        result = mark_read(test_db, "nonexistent-id")
        assert result is None

    def test_mark_all_read(self, test_db):
        """mark_all_read updates all unread."""
        from src.api.services.notification_service import (
            create_notification,
            mark_all_read,
        )

        create_notification(test_db, "web_changed", "1")
        create_notification(test_db, "web_changed", "2")

        count = mark_all_read(test_db)
        assert count == 2
