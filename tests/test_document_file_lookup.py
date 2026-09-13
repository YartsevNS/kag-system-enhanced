"""Поиск файла документа: точный путь вместо перебора каталога.

Раньше каждый показ превью/миниатюры перебирал ВСЕ файлы трёх каталогов
(iterdir + startswith) — на 10 000 документов это 10 000 системных вызовов на
запрос. Файлы лежат как `<document_id>_<имя>`, поэтому путь вычисляется.
"""
from src.api.services.document_service import document_service


def test_finds_file_by_exact_name(tmp_path, monkeypatch):
    monkeypatch.setattr(document_service, "_upload_dir", tmp_path)
    target = tmp_path / "doc-1_отчёт.pdf"
    target.write_bytes(b"contents")
    assert document_service.find_file("doc-1", "отчёт.pdf") == target
    assert document_service.find_file("doc-1") == target


def test_fallback_for_legacy_names(tmp_path, monkeypatch):
    """Файл со старым именем (без точного совпадения) находится перебором."""
    monkeypatch.setattr(document_service, "_upload_dir", tmp_path)
    legacy = tmp_path / "doc-2_старое_имя.pdf"
    legacy.write_bytes(b"legacy")
    assert document_service.find_file("doc-2", "другое_имя.pdf") == legacy


def test_missing_file_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(document_service, "_upload_dir", tmp_path)
    assert document_service.find_file("нет-такого", "x.pdf") is None


def test_other_document_is_not_substituted(tmp_path, monkeypatch):
    """Префиксный поиск не должен отдавать чужой файл (doc-1 vs doc-10)."""
    monkeypatch.setattr(document_service, "_upload_dir", tmp_path)
    (tmp_path / "doc-10_other.pdf").write_bytes(b"other")
    assert document_service.find_file("doc-1", "other.pdf") is None
