"""Автоочистка реестра документов при старте — защита от массового удаления.

Инцидент 19.09.2026: `DocumentService._cleanup_stale_records()` на старте удаляет из БД
записи, для которых нет файла на диске. Если каталог `/app/data/uploads` недоступен, сервис
уходит в fallback `/tmp/kag_uploads`, список «существующих» файлов получается пустым — и
удалялись ВСЕ документы (векторы в Qdrant и файлы при этом оставались целы). Восстанавливать
пришлось вручную из карточек Qdrant.

Тесты фиксируют три запрета и одно разрешение:
  - каталог недоступен      → не удаляем ничего;
  - каталог пуст           → не удаляем ничего;
  - под удаление попадает больше половины записей (и больше пяти) → не удаляем ничего;
  - единичная пропажа файла → удаляем ровно эту запись.
"""
import pathlib

import pytest


def _make_service(monkeypatch, upload_dir: pathlib.Path, docs: int):
    """DocumentService без обращения к БД, с подставленным каталогом загрузок."""
    from src.api.services import document_service as ds_module
    from src.api.services.document_service import DocumentRecord

    monkeypatch.setattr(ds_module.DocumentService, "_load_documents_from_db", lambda self: None)
    service = ds_module.DocumentService(upload_dir=str(upload_dir))

    service._documents = {}
    for i in range(docs):
        doc_id = f"doc{i:033d}"  # 36 символов — как настоящие UUID
        service._documents[doc_id] = DocumentRecord(
            document_id=doc_id, filename=f"file-{i}.pdf", file_hash=f"hash{i}", status="completed",
            progress=1.0, chunks_count=1, file_size=10, version=1,
        )

    deleted = []
    fake_repo = type("Repo", (), {"delete": lambda self, did: deleted.append(did)})()
    monkeypatch.setattr(
        "src.api.services.document_repository.get_doc_repo", lambda: fake_repo
    )
    return service, deleted


def test_missing_uploads_dir_deletes_nothing(monkeypatch, tmp_path):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    service, deleted = _make_service(monkeypatch, uploads, docs=8)
    # каталог исчез (не смонтирован / смонтирован позже контейнера)
    pathlib.Path(uploads).rmdir()
    service._cleanup_stale_records()
    assert deleted == [], f"удалено при недоступном каталоге: {deleted}"


def test_empty_uploads_dir_deletes_nothing(monkeypatch, tmp_path):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    service, deleted = _make_service(monkeypatch, uploads, docs=8)
    service._cleanup_stale_records()
    assert deleted == [], f"удалено при пустом каталоге: {deleted}"


def test_mass_mismatch_deletes_nothing(monkeypatch, tmp_path):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    service, deleted = _make_service(monkeypatch, uploads, docs=10)
    # файл есть только у одного документа из десяти — похоже на сбой, а не на реальную пропажу
    only = list(service._documents)[0]
    (uploads / f"{only}_file-0.pdf").write_text("x")
    service._cleanup_stale_records()
    assert deleted == [], f"удалено больше половины реестра: {deleted}"


def test_single_missing_file_is_deleted(monkeypatch, tmp_path):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    service, deleted = _make_service(monkeypatch, uploads, docs=10)
    ids = list(service._documents)
    for i, doc_id in enumerate(ids[1:], start=1):  # файлы есть у всех, кроме первого
        (uploads / f"{doc_id}_file-{i}.pdf").write_text("x")
    service._cleanup_stale_records()
    assert deleted == [ids[0]], f"ожидали удаление одной записи, получили: {deleted}"


def test_no_documents_is_noop(monkeypatch, tmp_path):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    service, deleted = _make_service(monkeypatch, uploads, docs=0)
    service._cleanup_stale_records()
    assert deleted == []
