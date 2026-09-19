"""Сироты: логика скана, контракт роутов и раздел в админке.

Проверяем три вещи, которые легко потерять:
 1) скан НИЧЕГО не удаляет сам — только считает;
 2) игнорируемые id не попадают в список, а first_seen сохраняется от прошлого скана;
 3) удаление возможно только по явному списку id (в теле запроса), а не «удалить всё
    без спроса»; в админке есть кнопки проверки и удаления.
"""
import importlib

import pytest

orphan_service = importlib.import_module("src.api.services.orphan_service")
config_store_module = importlib.import_module("src.api.services.config_store")


class FakeStore:
    def __init__(self, data=None):
        self.data = dict(data or {})

    def get(self, ns, key):
        return self.data.get(f"{ns}/{key}")

    def set(self, ns, key, value):
        self.data[f"{ns}/{key}"] = value


@pytest.fixture()
def fake_store(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(config_store_module, "config_store", store)
    return store


def _patch_scan(monkeypatch, per_doc, docs):
    monkeypatch.setattr(orphan_service, "count_by_document", lambda: per_doc)
    monkeypatch.setattr(orphan_service, "known_document_ids", lambda: set(docs))


def test_scan_finds_orphans_and_keeps_first_seen(monkeypatch, fake_store):
    per_doc = {
        "doc-a": {"points": 10, "filename": "a.pdf"},
        "doc-b": {"points": 5, "filename": "b.pdf"},
        "gone-1": {"points": 7, "filename": "старый.pdf"},
    }
    _patch_scan(monkeypatch, per_doc, {"doc-a", "doc-b"})

    first = orphan_service.scan()
    assert first["orphans_count"] == 1
    assert first["orphans_points"] == 7
    assert first["items"][0]["document_id"] == "gone-1"
    assert first["items"][0]["filename"] == "старый.pdf"
    seen = first["items"][0]["first_seen"]
    assert seen

    # второй скан: тот же сирота, время первого обнаружения не переписывается
    second = orphan_service.scan()
    assert second["items"][0]["first_seen"] == seen


def test_scan_respects_ignore_list(monkeypatch, fake_store):
    _patch_scan(monkeypatch, {"gone-1": {"points": 3, "filename": "x.pdf"}}, set())
    orphan_service.set_ignored(["gone-1"])
    assert orphan_service.scan()["orphans_count"] == 0
    orphan_service.set_ignored([])
    assert orphan_service.scan()["orphans_count"] == 1


def test_scan_does_not_delete_points(monkeypatch, fake_store):
    """Скан обязан быть только чтением: удаление — отдельная функция по команде админа."""
    _patch_scan(monkeypatch, {"gone-1": {"points": 3, "filename": "x.pdf"}}, set())
    called = {"delete": 0}
    monkeypatch.setattr(orphan_service, "_qdrant", lambda: type(
        "Q", (), {"delete": lambda *a, **k: called.__setitem__("delete", called["delete"] + 1)})())
    orphan_service.scan()
    assert called["delete"] == 0


def test_scan_reports_error_when_db_unreadable(monkeypatch, fake_store):
    monkeypatch.setattr(orphan_service, "count_by_document",
                        lambda: {"x": {"points": 1, "filename": ""}})
    monkeypatch.setattr(orphan_service, "known_document_ids", lambda: None)
    result = orphan_service.scan()
    assert result["status"] == "error"
    assert "Postgres" in result["message"]


def test_routes_and_panel_contract():
    from pathlib import Path

    admin = Path("src/api/routes/admin_models.py").read_text(encoding="utf-8")
    page = Path("src/api/static/admin.html").read_text(encoding="utf-8")
    tasks = Path("src/indexing/tasks.py").read_text(encoding="utf-8")
    celery = Path("src/indexing/celery_app.py").read_text(encoding="utf-8")

    for route in ('@router.get("/orphans"', '@router.post("/orphans/scan"',
                  '@router.post("/orphans/cleanup"', '@router.post("/orphans/ignore"',
                  '@router.post("/orphans/unignore"'):
        assert route in admin, f"нет роута {route}"
    # синхронные клиенты Qdrant/SQL не должны блокировать event loop
    assert admin.count("await asyncio.to_thread(scan)") >= 2
    # удаление — только по переданному списку
    assert "if not ids:" in admin
    assert "class OrphanCleanupRequest(BaseModel)" in admin

    # ежедневный скан в расписании beat
    assert "'scan-orphans'" in celery and "src.indexing.tasks.scan_orphans" in celery
    assert "def scan_orphans" in tasks

    # раздел в админке и его кнопки
    for marker in ('id="orphans-summary"', 'id="orphans-table"', "scanOrphans()",
                   "cleanupAllOrphans()", "loadOrphans()"):
        assert marker in page, f"в админке нет {marker}"
    # URL собирается из шаблонной строки с API_URL, поэтому проверяем путь без кавычек
    assert "/orphans/cleanup" in page and "/orphans/scan" in page
    assert "/orphans/ignore" in page
    # автоудаления в панели быть не должно: только по нажатию
    assert "confirm(" in page
