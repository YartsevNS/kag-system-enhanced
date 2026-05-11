# Enterprise Document Management System — Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Transform kag-system into a multi-user enterprise document management system with Russian OCR, document tracking, change detection, and group-based access control.

**Architecture:** Add user/group RBAC layer, Occular-ocr integration for Russian documents, document versioning with original storage, web/folder watchers with notification pipeline. Backend stays FastAPI + PostgreSQL + Qdrant + Redis + Ollama.

**Tech Stack:** Python 3.11, FastAPI, PostgreSQL (user/groups/audit), Qdrant (vectors), Redis (queues/cache), Occular-ocr (ONNX), watchdog (folder monitoring), httpx (web monitoring)

**Estimated:** 32 tasks, ~3-4 hours total

---

## Phase 0: Foundation (Server Setup & Dependencies)

### Task 0.1: Redeploy with proper resource limits

**Objective:** Update docker-compose.yml for 8-CPU/15GB server and redeploy

**Files:**
- Modify: `docker-compose.yml`

**Steps:**
1. Read current docker-compose.yml on server
2. Set appropriate resource limits:
   - api: 2 CPU, 2 GB
   - worker: 2 CPU, 4 GB
   - qdrant: 2 CPU, 4 GB
   - redis: 1 CPU, 1 GB
   - keycloak: 1 CPU, 2 GB
   - keycloak-db: 1 CPU, 1 GB
3. Redeploy: `sg docker -c 'docker-compose --profile dev up -d --build'`
4. Verify all services healthy
5. Commit

### Task 0.2: Install Occular-ocr on server

**Objective:** Clone and install Occular-ocr in the Docker API container

**Files:**
- Modify: `Dockerfile` (add Occular-ocr as dependency)

**Steps:**
1. Add to Dockerfile: `RUN pip install git+https://github.com/Bodhi42/Occular-ocr.git`
2. Add ONNX Runtime deps if needed
3. Rebuild API container
4. Verify: `docker exec kag-api python -c "from ocr_skel import ocr; print('OK')"`
5. Commit

---

## Phase 1: Multi-User & Group RBAC (Core Architecture)

### Task 1.1: Create User model with groups

**Objective:** Add User and Group models to PostgreSQL with relationships

**Files:**
- Create: `src/database/user_models.py`
- Modify: `src/database/models.py` (import new models)

**Implementation:**
```python
# src/database/user_models.py
from sqlalchemy import Column, String, Boolean, DateTime, Table, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from .models import Base

user_groups = Table(
    'user_groups', Base.metadata,
    Column('user_id', String, ForeignKey('users.id'), primary_key=True),
    Column('group_id', String, ForeignKey('groups.id'), primary_key=True)
)

class User(Base):
    __tablename__ = 'users'
    id = Column(String, primary_key=True)
    username = Column(String, unique=True, nullable=False)
    email = Column(String)
    hashed_password = Column(String, nullable=False)
    is_admin = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    groups = relationship('Group', secondary=user_groups, back_populates='users')

class Group(Base):
    __tablename__ = 'groups'
    id = Column(String, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    description = Column(String)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    users = relationship('User', secondary=user_groups, back_populates='groups')
```

1. Write model code
2. Create Alembic migration
3. Run migration
4. Test: create user, group, assign user to group
5. Commit

### Task 1.2: Add auth endpoints (register/login/JWT)

**Objective:** Create authentication API with JWT tokens

**Files:**
- Create: `src/api/routes/auth.py`
- Modify: `src/api/main.py` (register auth routes)

**Endpoints:**
- POST `/api/v1/auth/register` — create user
- POST `/api/v1/auth/login` — return JWT
- GET `/api/v1/auth/me` — current user info

1. Write auth routes with password hashing (bcrypt) and JWT generation
2. Add auth middleware that extracts user from JWT
3. Test: register → login → access protected endpoint
4. Commit

### Task 1.3: Group-based document access control

**Objective:** Restrict document search results by user's group membership

**Files:**
- Modify: `src/indexing/qdrant_service.py` (add group filter)
- Modify: `src/api/services/document_service.py` (pass user context)
- Modify: `src/api/routes/upload.py` (tag documents with group)

**Implementation:**
1. Add `group_ids` field to Qdrant document payload
2. On document upload, tag with uploader's group IDs
3. On search, filter by `group_ids` intersecting user's groups
4. Test: user in Group A can't see Group B's documents
5. Commit

---

## Phase 2: Document Tracking & Original Storage

### Task 2.1: Document versioning model

**Objective:** Track every version of uploaded documents with original storage

**Files:**
- Create: `src/database/document_models.py`

**Implementation:**
```python
class Document(Base):
    __tablename__ = 'documents'
    id = Column(String, primary_key=True)
    filename = Column(String, nullable=False)
    original_path = Column(String, nullable=False)  # path to original file
    file_hash = Column(String, nullable=False)  # SHA-256 of original
    mime_type = Column(String)
    file_size = Column(Integer)
    status = Column(String, default='processing')  # processing/ready/error/deleted
    group_id = Column(String, ForeignKey('groups.id'))
    uploaded_by = Column(String, ForeignKey('users.id'))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, onupdate=lambda: datetime.now(timezone.utc))
    versions = relationship('DocumentVersion', back_populates='document')

class DocumentVersion(Base):
    __tablename__ = 'document_versions'
    id = Column(String, primary_key=True)
    document_id = Column(String, ForeignKey('documents.id'))
    version_number = Column(Integer)
    file_hash = Column(String, nullable=False)
    original_path = Column(String)  # snapshot of original at this version
    change_description = Column(String)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    document = relationship('Document', back_populates='versions')
```

1. Write models
2. Create migration
3. Test: upload doc → creates Document + DocumentVersion(v1)
4. Commit

### Task 2.2: Original file storage service

**Objective:** Store original files with SHA-256 hashing for change detection

**Files:**
- Create: `src/api/services/storage_service.py`

**Implementation:**
```python
import hashlib
import shutil
from pathlib import Path

class StorageService:
    def __init__(self, base_path: str = "/app/user_data/originals"):
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)

    def store_original(self, file_content: bytes, filename: str, doc_id: str) -> tuple[str, str]:
        """Store original file, return (path, sha256)."""
        sha256 = hashlib.sha256(file_content).hexdigest()
        subdir = self.base_path / doc_id[:2]
        subdir.mkdir(exist_ok=True)
        dest = subdir / f"{doc_id}_{filename}"
        dest.write_bytes(file_content)
        return str(dest), sha256

    def get_original(self, path: str) -> bytes:
        return Path(path).read_bytes()

    def compare_hash(self, path: str, expected_hash: str) -> bool:
        current = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        return current == expected_hash
```

1. Write storage service
2. Integrate with upload endpoint
3. Test: upload doc → original stored, hash verified
4. Commit

### Task 2.3: Document re-upload & re-index trigger

**Objective:** When same document is re-uploaded (same filename/hash mismatch), create new version and re-index

**Files:**
- Modify: `src/api/routes/upload.py` (detect re-upload)
- Modify: `src/api/services/document_service.py` (re-index logic)

**Logic:**
1. On upload, check if document with same filename exists in user's group
2. If exists and hash differs → create new DocumentVersion, re-index, notify
3. If exists and hash same → skip (no change)
4. If new → normal upload flow

1. Implement re-upload detection
2. Add Celery task for re-indexing on version change
3. Test: upload same doc twice → v2 created, old vectors removed, new indexed
4. Commit

---

## Phase 3: Occular-ocr Integration

### Task 3.1: Occular-ocr wrapper service

**Objective:** Create OCR service using Occular-ocr with Tesseract fallback

**Files:**
- Create: `src/indexing/ocr_service.py`
- Modify: `src/indexing/__init__.py`

**Implementation:**
```python
from ocr_skel import OCRPipeline

class OCRService:
    def __init__(self):
        self.ocular = OCRPipeline(onnx=True, gpu=False)
        self.fallback_langs = ['rus', 'eng']

    def ocr_image(self, image_path: str) -> str:
        """OCR single image, returns text."""
        try:
            results = self.ocular.process_image(image_path)
            return '\n'.join(r['text'] for r in results)
        except Exception:
            return self._tesseract_fallback(image_path)

    def ocr_pdf(self, pdf_path: str, dpi: int = 300) -> list[str]:
        """OCR PDF, returns list of page texts."""
        try:
            results = self.ocular.process_pdf(pdf_path, dpi=dpi)
            return [r['text'] if isinstance(r, dict) else r for r in results]
        except Exception:
            return self._tesseract_fallback_pdf(pdf_path)

    def _tesseract_fallback(self, image_path: str) -> str:
        import pytesseract
        return pytesseract.image_to_string(image_path, lang='+'.join(self.fallback_langs))

    def _tesseract_fallback_pdf(self, pdf_path: str) -> list[str]:
        # Use pdf2image + pytesseract
        pass
```

1. Write OCR service
2. Test with Russian PDF: verify accuracy > 90%
3. Test fallback when Occular fails
4. Commit

### Task 3.2: Integrate OCR into document processing pipeline

**Objective:** Replace/upgrade the document processing pipeline to use OCR service

**Files:**
- Modify: `src/indexing/tasks.py` (use OCRService)
- Modify: `src/indexing/parsers.py` (OCR-first approach)

**Flow:**
1. Upload → detect if scanned PDF/image
2. If scanned: OCR via Occular → text
3. If digital: extract text directly
4. Both: chunk → embed → index in Qdrant

1. Modify Celery tasks
2. Add scan detection (check if PDF has text layer)
3. Test: upload scanned Russian PDF → OCR'd and indexed
4. Commit

---

## Phase 4: Web Page Monitoring

### Task 4.1: Web page watcher service

**Objective:** Monitor URLs for content changes with periodic checks

**Files:**
- Create: `src/monitoring/web_watcher.py`

**Implementation:**
```python
import httpx
import hashlib
from datetime import datetime, timezone
from typing import Optional

class WebPageWatcher:
    def __init__(self, db_session):
        self.db = db_session
        self.client = httpx.AsyncClient(timeout=30)

    async def check_page(self, url: str) -> Optional[dict]:
        """Check single URL for changes. Returns change info if changed."""
        try:
            resp = await self.client.get(url)
            resp.raise_for_status()
            current_hash = hashlib.sha256(resp.content).hexdigest()

            # Compare with stored hash
            stored = await self._get_stored_hash(url)
            if stored and stored.hash == current_hash:
                return None  # No change

            return {
                'url': url,
                'old_hash': stored.hash if stored else None,
                'new_hash': current_hash,
                'changed_at': datetime.now(timezone.utc),
                'content_preview': resp.text[:500]
            }
        except Exception as e:
            return {'url': url, 'error': str(e)}

    async def watch_urls(self, urls: list[str]) -> list[dict]:
        """Check multiple URLs, return list of changes."""
        changes = []
        for url in urls:
            result = await self.check_page(url)
            if result:
                changes.append(result)
        return changes
```

1. Write watcher service
2. Add DB table for watched URLs
3. Add API endpoints: add/remove/list watched URLs
4. Integrate with scheduler for periodic checks
5. Test: watch a URL, change it, detect change
6. Commit

### Task 4.2: Web change notification pipeline

**Objective:** Notify admin when watched pages change and auto-reindex

**Files:**
- Modify: `src/monitoring/web_watcher.py` (add notification)
- Create: `src/api/services/notification_service.py`

**Flow:**
1. Web watcher detects change
2. Extract new content → OCR if image → text
3. Index/re-index in Qdrant
4. Notify admin (log + optional email/Telegram)

1. Write notification service
2. Connect watcher → notification → re-index pipeline
3. Add notification preferences to User model
4. Test end-to-end: URL changes → document updated in search
5. Commit

---

## Phase 5: Local Folder Monitoring

### Task 5.1: Folder watcher service (watchdog)

**Objective:** Monitor local/network folders for new/changed files

**Files:**
- Create: `src/monitoring/folder_watcher.py`

**Implementation:**
```python
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import hashlib
from pathlib import Path

class FolderMonitor(FileSystemEventHandler):
    def __init__(self, callback):
        self.callback = callback  # async function to call on changes
        self._debounce = {}  # path → last event time

    def on_created(self, event):
        if not event.is_directory:
            self._trigger(event.src_path, 'created')

    def on_modified(self, event):
        if not event.is_directory:
            self._trigger(event.src_path, 'modified')

    def _trigger(self, path: str, action: str):
        # Debounce: ignore events within 5 seconds of last for same file
        now = time.time()
        if path in self._debounce and now - self._debounce[path] < 5:
            return
        self._debounce[path] = now

        file_hash = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        asyncio.run(self.callback({
            'path': path,
            'action': action,
            'hash': file_hash,
            'timestamp': datetime.now(timezone.utc)
        }))

class FolderWatcherService:
    def __init__(self):
        self.observer = Observer()
        self.watched_folders = {}

    def add_folder(self, folder_path: str, group_id: str, callback):
        """Start watching a folder for a specific group."""
        handler = FolderMonitor(callback)
        self.observer.schedule(handler, folder_path, recursive=True)
        self.watched_folders[folder_path] = {
            'group_id': group_id,
            'handler': handler
        }

    def start(self):
        self.observer.start()

    def stop(self):
        self.observer.stop()
        self.observer.join()
```

1. Add watchdog to requirements.txt
2. Write folder watcher service
3. Add DB table for watched folders (path, group_id, recursive)
4. Add API endpoints: add/remove/list watched folders
5. Test: add file to watched folder → detected, indexed
6. Commit

### Task 5.2: Folder change → document pipeline

**Objective:** Auto-ingest files from watched folders into document system

**Files:**
- Modify: `src/monitoring/folder_watcher.py` (connect to ingestion)
- Modify: `src/indexing/tasks.py` (add folder-based ingestion task)

**Flow:**
1. Folder watcher detects new/modified file
2. Check against document DB: is this file already tracked?
3. If new → upload, OCR, index
4. If modified → create new version, re-index
5. Notify admin

1. Connect folder watcher to document ingestion pipeline
2. Add startup hook: process existing files on first watch
3. Test: drop PDF in watched folder → appears in search
4. Commit

---

## Phase 6: Admin Notifications & Dashboard

### Task 6.1: Unified notification service

**Objective:** Single notification pipeline for all change events

**Files:**
- Create: `src/api/services/notification_service.py`

**Endpoints:**
- GET `/api/v1/notifications` — list notifications
- POST `/api/v1/notifications/read` — mark as read
- GET `/api/v1/admin/alerts` — admin-only alerts

**Notification types:**
- `document_updated` — document re-uploaded
- `web_page_changed` — watched URL changed
- `folder_file_added` — new file in watched folder
- `folder_file_modified` — file changed in watched folder
- `ocr_completed` — OCR processing finished
- `indexing_completed` — document indexed

1. Write notification model + service
2. Wire up all sources (upload, web watcher, folder watcher, OCR)
3. Test: trigger each notification type
4. Commit

### Task 6.2: Admin API endpoints

**Objective:** Admin endpoints for managing users, groups, watchers

**Files:**
- Create: `src/api/routes/admin_v2.py`

**Endpoints:**
- GET/POST `/api/v1/admin/users` — manage users
- GET/POST `/api/v1/admin/groups` — manage groups
- POST `/api/v1/admin/groups/{id}/users` — assign user to group
- GET `/api/v1/admin/watched-urls` — list watched URLs
- POST `/api/v1/admin/watched-urls` — add URL to watch
- GET `/api/v1/admin/watched-folders` — list watched folders
- POST `/api/v1/admin/watched-folders` — add folder to watch
- GET `/api/v1/admin/audit-log` — document change history

1. Write admin endpoints
2. Add admin-only middleware check
3. Test: admin creates group, assigns user, adds watch
4. Commit

---

## Phase 7: Integration & Polish

### Task 7.1: End-to-end test scenario

**Objective:** Verify full pipeline: watched folder → OCR → multi-user search → re-upload → re-index

**Scenario:**
1. Admin creates groups "Finance" and "HR"
2. User A (Finance) uploads scanned Russian PDF
3. System OCRs via Occular, indexes with group="Finance"
4. User B (HR) searches → doesn't see Finance doc
5. Admin adds folder watch `/data/finance_docs`
6. PDF dropped in folder → auto-ingested, OCR'd, indexed
7. Same PDF re-uploaded with changes → v2 created, re-indexed
8. Admin gets notification

1. Write integration test (pytest)
2. Run test, verify all steps pass
3. Fix any issues found
4. Commit

### Task 7.2: Deploy updated system to server

**Objective:** Push code, rebuild containers, deploy to 192.168.50.18

**Steps:**
1. Push all commits to GitHub
2. SSH to server, pull latest code
3. Rebuild containers: `docker-compose --profile dev up -d --build`
4. Run migrations
5. Verify all services healthy
6. Test basic flow: upload doc → search → find

### Task 7.3: Documentation

**Objective:** Update README with new features and architecture

**Files:**
- Modify: `README.md`

1. Add multi-user / RBAC section
2. Add Occular-ocr integration notes
3. Add web/folder monitoring setup guide
4. Add API documentation for new endpoints
5. Commit

---

## Success Criteria

- [ ] Users can register, login, and belong to groups
- [ ] Documents are tagged by group; search respects group boundaries
- [ ] Original documents stored with SHA-256 hashing
- [ ] Re-upload of same document creates new version and triggers re-index
- [ ] Russian PDF scans OCR'd via Occular-ocr with >90% accuracy
- [ ] Web pages monitored for changes; changes trigger re-index + notification
- [ ] Local/network folders monitored; new files auto-ingested
- [ ] Admin dashboard shows users, groups, watchers, audit log
- [ ] All notifications delivered to admin

---

*Plan version: 1.0 | 2026-05-11 | Hermes Agent*
