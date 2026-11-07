"""Выбор файла для обработки: бэкап версии не должен подменять рабочий файл.

Инцидент 07.11.2026: рядом с рабочим `*.md` лежали `*.md.v1.bak` и
`*.md.v1.bak.v1.bak`. Обработчик брал первый файл, начинающийся с document_id,
попадал в `.bak`, парсер падал с «Неподдерживаемый формат», Celery повторял
задачу, а документ оставался в `processing` — наблюдение: 135 попыток за сутки,
документ `8ef6b9c4` висел с 12.09.2026.
"""
from pathlib import Path

from src.api.services.document_service import document_service


def _pick(tmp_path, monkeypatch, document_id: str, filename: str):
    monkeypatch.setattr(document_service, "_upload_dir", tmp_path)
    return document_service._find_file(document_id, filename)


def test_backup_near_working_file_is_ignored(tmp_path, monkeypatch):
    """Тот самый случай: .bak лежит первым по алфавиту, брать нужно .md."""
    doc = "8ef6b9c4-af34"
    working = tmp_path / f"{doc}_gost_50922.md"
    working.write_text("текст", encoding="utf-8")
    (tmp_path / f"{doc}_gost_50922.md.v1.bak").write_text("старое", encoding="utf-8")
    (tmp_path / f"{doc}_gost_50922.md.v1.bak.v1.bak").write_text("ещё старее", encoding="utf-8")

    assert _pick(tmp_path, monkeypatch, doc, "gost_50922.md") == working


def test_only_backups_means_no_file(tmp_path, monkeypatch):
    """Если остались одни бэкапы — честный None, а не попытка парсить .bak."""
    doc = "doc-bak"
    (tmp_path / f"{doc}_x.md.v1.bak").write_text("старое", encoding="utf-8")
    assert _pick(tmp_path, monkeypatch, doc, "x.md") is None


def test_exact_name_has_priority(tmp_path, monkeypatch):
    """При нескольких подходящих файлах точное `{id}_{filename}` выигрывает."""
    doc = "doc-2"
    exact = tmp_path / f"{doc}_нужный.pdf"
    other = tmp_path / f"{doc}_другой.pdf"
    exact.write_bytes(b"exact")
    other.write_bytes(b"other")

    assert _pick(tmp_path, monkeypatch, doc, "нужный.pdf") == exact


def test_legacy_name_still_found(tmp_path, monkeypatch):
    """Старое имя (без точного совпадения) находится перебором, как раньше."""
    doc = "doc-3"
    legacy = tmp_path / f"{doc}_старое_имя.pdf"
    legacy.write_bytes(b"legacy")

    assert _pick(tmp_path, monkeypatch, doc, "другое_имя.pdf") == legacy
