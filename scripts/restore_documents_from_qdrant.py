#!/usr/bin/env python3
"""Восстановление таблицы documents из карточек Qdrant + дампа 29.08 + файлов на диске.

Запуск ВНУТРИ контейнера kag-api:
    docker exec kag-api python /app/data/restore_docs/restore_documents.py          # dry-run
    docker exec kag-api python /app/data/restore_docs/restore_documents.py --apply   # запись

Источники:
  1) карточки Qdrant (level=document) — id, title, type, domain, summary, topics, права, visibility;
  2) файлы /app/data/uploads — имя вида {doc_id}_{исходное имя}; из него берём filename,
     размер, sha256 (это же имя собирает приложение: uploads/{doc_id}_{filename});
  3) дамп 29.08 (/app/data/restore_docs/dump_docs.json) — mime_type, uploaded_by, created_at, version
     и резерв для file_hash, если файла нет.
Кол-во чанков считается по точкам Qdrant с этим document_id.
"""
import hashlib
import json
import os
import pathlib
import sys
from datetime import datetime, timezone

from qdrant_client import QdrantClient
from sqlalchemy import create_engine, text

APPLY = "--apply" in sys.argv
DUMP_JSON = pathlib.Path("/app/data/restore_docs/dump_docs.json")
UPLOADS = pathlib.Path("/app/data/uploads")
ADMIN_ID = "01c8d804-005c-4955-8c6c-5bd850291e72"  # admin: подставляется, если автор из дампа неизвестен
COLLECTION = "kag_documents"
STATS_USERS = {"автор_из_дампа": 0, "автор_подменён": 0}


def qdrant() -> QdrantClient:
    host = os.environ.get("QDRANT_HOST") or "kag-qdrant"
    url = os.environ.get("QDRANT_URL") or f"http://{host}:6333"
    return QdrantClient(url=url, api_key=os.environ.get("QDRANT_API_KEY") or None)


def qdrant_data(client: QdrantClient):
    cards, chunks = {}, {}
    offset = None
    while True:
        batch, offset = client.scroll(COLLECTION, limit=1000, with_payload=True, offset=offset)
        for point in batch:
            payload = point.payload or {}
            doc_id = payload.get("document_id")
            if not doc_id:
                continue
            if payload.get("level") == "document":
                cards[doc_id] = payload
            else:
                chunks[doc_id] = chunks.get(doc_id, 0) + 1
        if offset is None:
            break
    return cards, chunks


def file_map():
    """doc_id (первые 36 символов имени файла) -> (путь, имя файла без префикса)."""
    out = {}
    if UPLOADS.exists():
        for path in UPLOADS.iterdir():
            if not path.is_file():
                continue
            key = path.name[:36]
            name = path.name[37:] if path.name[36:37] == "_" else path.name
            out.setdefault(key, (path, name))
    return out


def sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def as_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def build_rows():
    cards, chunks = qdrant_data(qdrant())
    dump = json.loads(DUMP_JSON.read_text(encoding="utf-8")) if DUMP_JSON.exists() else {}
    files = file_map()
    engine = create_engine(os.environ["KAG_DB_URL"])
    with engine.connect() as conn:
        valid_users = {r[0] for r in conn.execute(text("SELECT id FROM users")).all()}
        fallback_author = ADMIN_ID if ADMIN_ID in valid_users else (sorted(valid_users)[0] if valid_users else None)

    rows = []
    stats = {"с_файлом": 0, "без_файла": [], "из_дампа": 0, "чанков": 0, "хеш_из_файла": 0, "хеш_из_дампа": 0}
    for doc_id, card in sorted(cards.items(), key=lambda kv: (kv[1].get("filename") or "")):
        entry = files.get(doc_id)
        path, filename = entry if entry else (None, card.get("filename") or "")
        old = dump.get(filename) or dump.get(card.get("filename")) or {}

        if path is not None:
            stats["с_файлом"] += 1
            stats["хеш_из_файла"] += 1
            size = path.stat().st_size
            file_hash = sha256(path)
            created = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        else:
            stats["без_файла"].append(filename or doc_id)
            size = old.get("file_size")
            if old.get("file_hash"):
                stats["хеш_из_дампа"] += 1
            file_hash = old.get("file_hash") or f"restored-no-file:{doc_id}"
            created = as_dt(old.get("created_at")) or datetime.now(timezone.utc)
        if old:
            stats["из_дампа"] += 1
        n_chunks = chunks.get(doc_id, 0)
        stats["чанков"] += n_chunks

        created = as_dt(old.get("created_at")) or created
        author = old.get("uploaded_by")
        if author in valid_users:
            STATS_USERS["автор_из_дампа"] += 1
        else:
            author = fallback_author
            STATS_USERS["автор_подменён"] += 1
        rows.append({
            "id": doc_id,
            "filename": filename,
            "file_type": (pathlib.Path(filename).suffix.lstrip(".").lower() or None),
            "file_size": int(size) if size else None,
            "file_hash": file_hash,
            "mime_type": old.get("mime_type"),
            "status": "completed",
            "progress": 1.0,
            "chunks_count": n_chunks,
            "version": int(old.get("version") or 1),
            "is_active": True,
            "visibility": card.get("visibility") or old.get("visibility") or "public",
            "group_ids": old.get("group_ids"),
            "allow_group_ids": card.get("allow_group_ids") or old.get("allow_group_ids"),
            "deny_group_ids": card.get("deny_group_ids") or old.get("deny_group_ids"),
            "allow_user_ids": card.get("allow_user_ids") or old.get("allow_user_ids"),
            "deny_user_ids": card.get("deny_user_ids") or old.get("deny_user_ids"),
            "uploaded_by": author,
            "document_type": card.get("document_type") or old.get("document_type"),
            "recognized_title": card.get("title") or old.get("recognized_title"),
            "summary": card.get("summary") or old.get("summary"),
            "topics": card.get("topics") or old.get("topics"),
            "domain": card.get("domain") or old.get("domain"),
            "created_at": created,
            "updated_at": datetime.now(timezone.utc),
        })
    return rows, stats


INSERT = text("""
INSERT INTO documents (id, filename, file_type, file_size, file_hash, mime_type, status, progress,
    chunks_count, version, is_active, visibility, group_ids, allow_group_ids, deny_group_ids,
    allow_user_ids, deny_user_ids, uploaded_by, document_type, recognized_title, summary, topics,
    domain, created_at, updated_at)
VALUES (:id, :filename, :file_type, :file_size, :file_hash, :mime_type, :status, :progress,
    :chunks_count, :version, :is_active, :visibility, :group_ids, :allow_group_ids, :deny_group_ids,
    :allow_user_ids, :deny_user_ids, :uploaded_by, :document_type, :recognized_title, :summary,
    :topics, :domain, :created_at, :updated_at)
ON CONFLICT (id) DO NOTHING
""")


def main() -> int:
    rows, stats = build_rows()
    print(f"карточек в Qdrant: {len(rows)} | чанков у них: {stats['чанков']}")
    print(f"файл найден: {stats['с_файлом']} | без файла: {len(stats['без_файла'])}")
    for name in stats["без_файла"]:
        print("   без файла:", name[:80])
    print(f"хеш из файла: {stats['хеш_из_файла']} | из дампа (файла нет): {stats['хеш_из_дампа']} | "
          f"строк из дампа: {stats['из_дампа']}")
    print(f"автор из дампа: {STATS_USERS['автор_из_дампа']} | подменён на admin (нет такого пользователя): "
          f"{STATS_USERS['автор_подменён']}")

    engine = create_engine(os.environ["KAG_DB_URL"])
    with engine.begin() as conn:
        before = conn.execute(text("SELECT count(*) FROM documents")).scalar()
    print(f"строк в documents до: {before}")

    if not APPLY:
        print("DRY-RUN: ничего не записано (нужен --apply)")
        for row in rows[:5]:
            print("   ", row["id"][:8], "|", row["filename"][:45], "|", row["chunks_count"], "чанков |",
                  row["domain"], "|", row["document_type"], "|", row["file_size"], "байт")
        return 0

    with engine.begin() as conn:
        for row in rows:
            conn.execute(INSERT, row)
        after = conn.execute(text("SELECT count(*) FROM documents")).scalar()
        done = conn.execute(text("SELECT count(*), coalesce(sum(chunks_count),0) FROM documents "
                                 "WHERE status = 'completed'")).one()
        domains = conn.execute(text("SELECT domain, count(*) FROM documents GROUP BY domain "
                                    "ORDER BY 2 DESC")).all()
    print(f"строк в documents после: {after} (добавлено {after - before})")
    print(f"completed: {done[0]}, сумма chunks_count: {done[1]}")
    print("домены:", [(d, c) for d, c in domains])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
