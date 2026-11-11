"""Поиск возможных дублей документа: ключ, название, близость текста.

Точный дубль (тот же SHA-256) отсекается на загрузке отдельно и здесь не участвует.
Здесь — «тот же документ, другие байты»: та же тройка издатель+вид+номер, либо то же
название (для новостей из разных RSS), либо очень близкий текст.
"""
import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Знаки, которые не меняют смысл названия, но ломают сравнение.
_PUNCT = re.compile(r"[«»\"'`.,;:!?()\[\]{}<>*]+")
_DASHES = re.compile(r"[—–−―‐‑]")
_SPACES = re.compile(r"\s+")


def normalize_title(value: str) -> str:
    """Название для сравнения: регистр, пунктуация, тире, лишние пробелы."""
    text = (value or "").lower().replace("ё", "е")
    text = _DASHES.sub("-", text)
    text = _PUNCT.sub(" ", text)
    text = re.sub(r"\b№\s*", " ", text)
    text = _SPACES.sub(" ", text).strip()
    return text


def metadata_key(meta: Dict[str, Any]) -> str:
    """Ключ документа из метаданных: издатель|вид|номер. Пустая строка — собрать нельзя."""
    if not isinstance(meta, dict):
        return ""
    number = str(meta.get("number") or "").strip()
    if not number:
        return ""
    try:
        from src.indexing import doc_context

        return doc_context.build_document_key(
            str(meta.get("issuer") or ""), str(meta.get("doc_kind") or ""), number,
            str(meta.get("doc_title") or ""),
        )
    except Exception as exc:  # noqa: BLE001 — сравнение не должно ломать загрузку
        logger.debug("ключ для сравнения не собран: %s", str(exc)[:120])
        return ""


def _json(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        return json.loads(value)
    except Exception:  # noqa: BLE001
        return {}


def find_key_matches(db, new_meta: Dict[str, Any], exclude_id: str = "") -> List[Dict[str, Any]]:
    """Документы с тем же ключом издатель|вид|номер, но другим файлом.

    Перебор по таблице, а не SQL-запрос: ключ собирается тем же кодом, что и в конвейере
    (нормализация номера, код вида), и повторять эту логику в SQL — значит завести второй
    источник правды. При корпусе в тысячи документов перебор дешёвый; при десятках тысяч
    это надо заменить на хранимую колонку document_key.
    """
    key = metadata_key(new_meta)
    if not key:
        return []
    out: List[Dict[str, Any]] = []
    try:
        from src.database.document_models import Document

        rows = db.query(Document.id, Document.filename, Document.file_hash,
                        Document.source_metadata, Document.recognized_title,
                        Document.collection, Document.created_at).all()
    except Exception as exc:  # noqa: BLE001
        logger.warning("поиск дублей по ключу не выполнен: %s", str(exc)[:160])
        return []
    for doc_id, filename, _hash, smeta, rtitle, collection, created in rows:
        if doc_id == exclude_id:
            continue
        other = metadata_key(_json(smeta))
        if other and other == key:
            out.append({
                "document_id": doc_id, "filename": filename,
                "recognized_title": rtitle or "", "collection": collection or "",
                "created_at": created.isoformat() if created else "",
                "key": other,
            })
    return out


def find_title_matches(db, title: str, collection: str = "", exclude_id: str = "") -> List[Dict[str, Any]]:
    """Документы с тем же нормализованным названием.

    Для новостей это и есть дубль: одна и та же статья приходит из разных RSS с разной
    разметкой, и держать три копии в индексе бессмысленно.
    """
    norm = normalize_title(title)
    if len(norm) < 12:
        return []
    out: List[Dict[str, Any]] = []
    try:
        from src.database.document_models import Document

        q = db.query(Document.id, Document.filename, Document.recognized_title,
                     Document.collection, Document.created_at)
        if collection:
            q = q.filter(Document.collection == collection)
        rows = q.all()
    except Exception as exc:  # noqa: BLE001
        logger.warning("поиск дублей по названию не выполнен: %s", str(exc)[:160])
        return []
    for doc_id, filename, rtitle, coll, created in rows:
        if doc_id == exclude_id:
            continue
        if normalize_title(rtitle or filename) == norm:
            out.append({
                "document_id": doc_id, "filename": filename,
                "recognized_title": rtitle or "", "collection": coll or "",
                "created_at": created.isoformat() if created else "",
                "title": norm,
            })
    return out


async def find_text_matches(document_id: str, sample_text: str,
                            threshold: float = 0.95) -> List[Dict[str, Any]]:
    """Документы с очень близким текстом (косинус выше порога).

    Проба — первые фрагменты нового документа. Поиск по ним даёт соседей по смыслу;
    своего документа в выдаче не учитываем.
    """
    sample = (sample_text or "").strip()
    if len(sample) < 200:
        return []
    try:
        from src.indexing.embeddings_service import embeddings_service

        hits = await embeddings_service.search(query=sample[:2000], limit=8, is_admin=True)
    except Exception as exc:  # noqa: BLE001
        logger.debug("поиск похожих по тексту не выполнен: %s", str(exc)[:160])
        return []
    out: List[Dict[str, Any]] = []
    seen = set()
    for hit in hits or []:
        payload = hit.get("payload") or {}
        other_id = payload.get("document_id") or hit.get("document_id")
        score = float(hit.get("score") or 0.0)
        if not other_id or other_id == document_id or other_id in seen:
            continue
        if score < threshold:
            continue
        seen.add(other_id)
        out.append({
            "document_id": other_id,
            "filename": payload.get("filename") or "",
            "score": round(score, 4),
            "chunk_id": payload.get("chunk_id") or "",
        })
    return out


def sample_text_from_chunks(chunks: List[Dict[str, Any]], limit: int = 3) -> str:
    """Первые фрагменты документа как проба для сравнения."""
    parts = []
    for chunk in (chunks or [])[:limit]:
        text = chunk.get("content") or chunk.get("text") or ""
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def describe(new_doc: Dict[str, Any], other: Dict[str, Any], reason: str) -> str:
    """Человеческая строка для страницы: что с чем сравнивали."""
    name = new_doc.get("filename") or new_doc.get("recognized_title") or "новый документ"
    existing = other.get("recognized_title") or other.get("filename") or "существующий документ"
    if reason == "key_match":
        return f"«{name}» совпадает по обозначению с «{existing}» ({other.get('key') or ''})"
    if reason == "title_match":
        return f"«{name}» совпадает по названию с «{existing}»"
    return f"«{name}» текстово близок к «{existing}» (сходство {other.get('score')})"
