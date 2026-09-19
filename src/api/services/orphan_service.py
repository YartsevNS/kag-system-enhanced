"""Сироты в векторном хранилище: точки, у которых нет документа в Postgres.

Зачем сервис, а не автоочистка. Сироты появляются, когда обработка продолжает писать
векторы после удаления документа (проверено экспериментально: после удаления при 24
точках задача дописала ещё 524 и завершилась) либо когда база правилась в обход API.
Но на стенде штатно бывают паузы обработки — блокировка из админки, пополнение счёта,
перезапуск после снятия питания, — поэтому автоматически удалять нельзя: сначала
показываем администратору, удаляем только по его команде.

Данные для админки лежат в config_store:
    orphans/scan     — последний скан: время, сколько найдено, список (с first_seen)
    orphans/ignored  — список id, которые администратор решил не трогать
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

SCAN_NAMESPACE = "orphans"
SCAN_KEY = "scan"
IGNORE_KEY = "ignored"
SCAN_LIMIT = 200  # столько сирот показываем в админке (все считаем всегда)


def _collection() -> str:
    return os.environ.get("QDRANT_COLLECTION", "kag_documents")


def _qdrant():
    """Клиент Qdrant по тем же переменным, что у сервиса эмбеддингов."""
    from qdrant_client import QdrantClient

    host = os.environ.get("QDRANT_HOST", "kag-qdrant")
    port = os.environ.get("QDRANT_PORT", "6333")
    api_key = os.environ.get("QDRANT_API_KEY") or None
    if not host.startswith("http"):
        host = f"http://{host}:{port}"
    return QdrantClient(url=host, api_key=api_key)


def count_by_document() -> Dict[str, Dict[str, Any]]:
    """Сколько точек у каждого document_id и какое имя файла в payload.

    Идём scroll'ом, а не facet'ом: facet требует payload-индекса по полю, а его на
    старых коллекциях может не быть. 5 тыс. точек — это доли секунды.
    """
    client = _qdrant()
    per_doc: Dict[str, Dict[str, Any]] = {}
    offset = None
    while True:
        points, offset = client.scroll(
            _collection(), limit=1000, offset=offset,
            with_payload=["document_id", "filename", "source"], with_vectors=False,
        )
        for p in points:
            payload = p.payload or {}
            did = str(payload.get("document_id") or "")
            if not did:
                continue
            item = per_doc.setdefault(did, {"points": 0, "filename": ""})
            item["points"] += 1
            if not item["filename"]:
                item["filename"] = str(payload.get("filename") or payload.get("source") or "")
        if offset is None:
            return per_doc
        if len(per_doc) > 20000:  # предохранитель от бесконечного обхода
            return per_doc


def known_document_ids() -> Optional[set]:
    """id документов из Postgres. None — не смогли прочитать (тогда скан не сохраняем)."""
    try:
        from src.api.services.document_repository import get_doc_repo

        with get_doc_repo()._session() as s:  # type: ignore[attr-defined]
            from src.database.document_models import Document

            return {str(row[0]) for row in s.query(Document.id).all()}
    except Exception:
        try:
            from sqlalchemy import create_engine, text

            pwd = os.environ.get("KAG_DB_PASSWORD", "")
            url = os.environ.get("KAG_DB_URL") or f"postgresql://kag:{pwd}@kag-db:5432/kag"
            with create_engine(url).connect() as conn:
                return {str(r[0]) for r in conn.execute(text("select id from documents")).fetchall()}
        except Exception:
            return None


def ignored() -> List[str]:
    from src.api.services.config_store import config_store

    value = config_store.get(SCAN_NAMESPACE, IGNORE_KEY)
    if isinstance(value, list):
        return [str(x) for x in value]
    return []


def set_ignored(ids: List[str]) -> List[str]:
    from src.api.services.config_store import config_store

    config_store.set(SCAN_NAMESPACE, IGNORE_KEY, sorted({str(x) for x in ids}))
    return ignored()


def last_scan() -> Dict[str, Any]:
    from src.api.services.config_store import config_store

    value = config_store.get(SCAN_NAMESPACE, SCAN_KEY)
    return value if isinstance(value, dict) else {}


def scan(save: bool = True) -> Dict[str, Any]:
    """Найти сирот: точки есть, документа в Postgres нет.

    Автоматически НИЧЕГО не удаляет. `first_seen` сохраняется от прошлого скана,
    чтобы в админке было видно, с какого момента сирота висит.
    """
    per_doc = count_by_document()
    docs = known_document_ids()
    if docs is None:
        return {"status": "error", "message": "не удалось прочитать список документов из Postgres"}

    previous = {str(item.get("document_id")): item.get("first_seen")
                for item in (last_scan().get("items") or [])}
    ignored_ids = set(ignored())
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    orphans: List[Dict[str, Any]] = []
    for did, info in per_doc.items():
        if did in docs or did in ignored_ids:
            continue
        orphans.append({
            "document_id": did,
            "points": info["points"],
            "filename": info["filename"],
            "first_seen": previous.get(did) or now,
        })
    orphans.sort(key=lambda x: -int(x["points"]))
    total_points = sum(int(x["points"]) for x in orphans)

    result = {
        "ts": now,
        "documents_in_db": len(docs),
        "document_ids_in_qdrant": len(per_doc),
        "points_in_qdrant": sum(int(i["points"]) for i in per_doc.values()),
        "orphans_count": len(orphans),
        "orphans_points": total_points,
        "items": orphans[:SCAN_LIMIT],
        "truncated": len(orphans) > SCAN_LIMIT,
        "ignored": sorted(ignored_ids),
    }
    if save:
        from src.api.services.config_store import config_store

        config_store.set(SCAN_NAMESPACE, SCAN_KEY, result)
    return result


def cleanup(document_ids: List[str]) -> Dict[str, Any]:
    """Удалить точки указанных документов. Вызывается ТОЛЬКО по команде админа."""
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    client = _qdrant()
    deleted_ids: List[str] = []
    errors: List[str] = []
    for did in document_ids:
        did = str(did)
        try:
            client.delete(
                _collection(),
                points_selector=Filter(must=[FieldCondition(key="document_id",
                                                            match=MatchValue(value=did))]),
            )
            deleted_ids.append(did)
        except Exception as e:
            errors.append(f"{did}: {type(e).__name__}: {str(e)[:80]}")
    return {"status": "ok" if not errors else "partial", "deleted_ids": deleted_ids, "errors": errors}
