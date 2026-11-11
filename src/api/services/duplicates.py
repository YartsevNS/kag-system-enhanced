"""Очередь возможных дублей: запись кандидатов и решения человека.

Правила: без решения человека записи не сливаются, версии не создаются, ничего не удаляется
автоматически. Точный дубль по SHA-256 сюда не попадает — он отсекается на загрузке.
"""
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _json(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        return json.loads(value)
    except Exception:  # noqa: BLE001
        return {}


def record_candidates(db, new_doc: Dict[str, Any], candidates: List[Dict[str, Any]],
                      reason: str, score: float = 0.0) -> int:
    """Записать кандидатов в очередь. Возвращает число новых записей."""
    from src.database.duplicate_models import DuplicateCandidate

    added = 0
    for other in candidates or []:
        existing_id = other.get("document_id")
        if not existing_id or existing_id == new_doc.get("document_id"):
            continue
        already = (db.query(DuplicateCandidate)
                     .filter(DuplicateCandidate.new_document_id == new_doc["document_id"],
                             DuplicateCandidate.existing_document_id == existing_id,
                             DuplicateCandidate.reason == reason)
                     .first())
        if already:
            continue
        row = DuplicateCandidate(
            new_document_id=new_doc["document_id"],
            existing_document_id=existing_id,
            reason=reason,
            score=float(score or other.get("score") or 0.0),
            status="pending",
            details=json.dumps({
                "new_filename": new_doc.get("filename") or "",
                "new_title": new_doc.get("recognized_title") or "",
                "existing_filename": other.get("filename") or "",
                "existing_title": other.get("recognized_title") or "",
                "key": other.get("key") or "",
                "created_at_existing": other.get("created_at") or "",
            }, ensure_ascii=False),
        )
        db.add(row)
        added += 1
    if added:
        db.commit()
        logger.info("очередь дублей: добавлено %s кандидатов (%s) для %s",
                    added, reason, new_doc.get("document_id"))
    return added


def list_candidates(db, status: str = "pending", limit: int = 100) -> List[Dict[str, Any]]:
    """Список кандидатов с именами файлов для страницы."""
    from src.database.document_models import Document
    from src.database.duplicate_models import DuplicateCandidate

    q = db.query(DuplicateCandidate)
    if status:
        q = q.filter(DuplicateCandidate.status == status)
    rows = q.order_by(DuplicateCandidate.created_at.desc()).limit(limit).all()

    ids = {r.new_document_id for r in rows} | {r.existing_document_id for r in rows}
    names: Dict[str, Dict[str, Any]] = {}
    if ids:
        for doc in db.query(Document.id, Document.filename, Document.recognized_title,
                            Document.file_hash, Document.created_at).filter(Document.id.in_(ids)):
            names[doc.id] = {
                "filename": doc.filename or "",
                "title": doc.recognized_title or "",
                "hash": (doc.file_hash or "")[:12],
                "created_at": doc.created_at.isoformat() if doc.created_at else "",
            }

    out: List[Dict[str, Any]] = []
    for r in rows:
        details = _json(r.details)
        out.append({
            "id": r.id,
            "reason": r.reason,
            "score": r.score,
            "status": r.status,
            "created_at": r.created_at.isoformat() if r.created_at else "",
            "decided_by": r.decided_by or "",
            "decided_at": r.decided_at.isoformat() if r.decided_at else "",
            "new": {"document_id": r.new_document_id, **names.get(r.new_document_id, {}),
                    "filename": details.get("new_filename") or names.get(r.new_document_id, {}).get("filename", ""),
                    "title": details.get("new_title") or names.get(r.new_document_id, {}).get("title", "")},
            "existing": {"document_id": r.existing_document_id,
                         **names.get(r.existing_document_id, {}),
                         "filename": details.get("existing_filename") or names.get(r.existing_document_id, {}).get("filename", ""),
                         "title": details.get("existing_title") or names.get(r.existing_document_id, {}).get("title", "")},
            "key": details.get("key") or "",
        })
    return out


def find_candidate(db, candidate_id: str):
    from src.database.duplicate_models import DuplicateCandidate

    return db.query(DuplicateCandidate).filter(DuplicateCandidate.id == candidate_id).first()


def _mark(row, action: str, user: str) -> None:
    row.status = action
    row.decided_by = user or ""
    row.decided_at = datetime.now(timezone.utc)


def decide(db, candidate_id: str, action: str, user: str = "") -> Dict[str, Any]:
    """Решение человека по кандидату.

    action:
      new_version — новый файл это НОВАЯ РЕДАКЦИЯ существующего документа:
                    связываем (supersedes_id), версия +1; связь в онтологии появится
                    при переобработке нового файла.
      copy        — это КОПИЯ существующего документа из другого источника:
                    источник дописывается к существующему, новая запись удаляется.
      distinct    — это ДРУГОЙ документ: ничего не меняем, кандидат закрыт.
    """
    from src.database.document_models import Document

    row = find_candidate(db, candidate_id)
    if not row:
        return {"status": "error", "message": "кандидат не найден"}
    if row.status != "pending":
        return {"status": "error", "message": f"кандидат уже обработан: {row.status}"}
    if action not in ("new_version", "copy", "distinct"):
        return {"status": "error", "message": f"неизвестное действие: {action}"}

    new_doc = db.query(Document).filter(Document.id == row.new_document_id).first()
    old_doc = db.query(Document).filter(Document.id == row.existing_document_id).first()
    if action != "distinct" and (not new_doc or not old_doc):
        return {"status": "error", "message": "документ из пары не найден"}

    result: Dict[str, Any] = {"status": "ok", "action": action}
    if action == "new_version":
        meta = _json(new_doc.source_metadata)
        meta["supersedes_id"] = old_doc.id
        new_doc.source_metadata = json.dumps(meta, ensure_ascii=False)
        new_doc.previous_document_id = old_doc.id
        new_doc.version = int(old_doc.version or 1) + 1
        _mark(row, action, user)
        db.commit()
        result["message"] = ("Отмечено как новая редакция. Связь supersedes и пометка "
                             "прежней редакции появятся при обработке этого файла.")
        result["reprocess"] = new_doc.id
    elif action == "copy":
        meta = _json(old_doc.source_metadata)
        sources = meta.get("sources") if isinstance(meta.get("sources"), list) else []
        new_meta = _json(new_doc.source_metadata)
        sources.append({
            "document_id": new_doc.id,
            "filename": new_doc.filename,
            "source_name": new_meta.get("source_name") or "",
            "source_url": new_meta.get("source_url") or "",
            "file_hash": new_doc.file_hash or "",
            "added_by": user or "",
            "added_at": datetime.now(timezone.utc).isoformat(),
        })
        meta["sources"] = sources
        old_doc.source_metadata = json.dumps(meta, ensure_ascii=False)
        _mark(row, action, user)
        db.commit()
        # Строка в БД обновлена, но у сервиса документов свой кэш записей: он перезапишет
        # source_metadata прежним значением при следующем сохранении. Пишем ещё и через
        # репозиторий и обновляем кэш — иначе источник «допишется» и тут же пропадёт.
        try:
            from src.api.services.document_repository import get_doc_repo

            get_doc_repo().upsert(old_doc.id, {"source_metadata": old_doc.source_metadata})
        except Exception as exc:  # noqa: BLE001
            logger.warning("дубли: источник не записан в БД: %s", str(exc)[:140])
        try:
            from src.api.services.document_service import document_service as _svc

            _rec = _svc._documents.get(old_doc.id)
            if _rec is not None:
                _rec.source_metadata = json.loads(old_doc.source_metadata)
        except Exception:  # noqa: BLE001
            pass
        result["message"] = ("Копия: источник дописан к существующему документу. "
                             "Новая запись будет удалена вместе с векторами.")
        result["delete"] = new_doc.id
    else:
        _mark(row, action, user)
        db.commit()
        result["message"] = "Отмечено как отдельный документ."

    logger.info("очередь дублей: %s → %s (%s)", candidate_id, action, user or "—")
    return result


def link_superseded_chunks(document_id: str) -> None:
    """Заглушка-напоминание: пометка прежней редакции делается в конвейере по supersedes_id."""
    logger.debug("пометка прежней редакции выполняется конвейером для %s", document_id)
