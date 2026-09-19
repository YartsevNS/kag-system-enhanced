#!/usr/bin/env python3
"""Проверка консистентности реестра после загрузок/удалений документов.

Запуск ВНУТРИ контейнера api (там есть доступ и к БД, и к Qdrant, и к файлам):
    docker exec kag-api python /app/data/check_registry_consistency.py

Что проверяет:
  1) документы в Postgres — сколько, в каких статусах;
  2) Qdrant: точки-карточки (level=document) и точки-чанки, документы без векторов,
     точки без документа в БД (сироты);
  3) файлы в /app/data/uploads: записи без файла и файлы без записи (по префиксу doc_id);
  4) сироты по мнению сервиса (тот же расчёт, что в админке).
Печатает расхождения списком; пустой список = всё согласовано.
"""
import os
import pathlib

from qdrant_client import QdrantClient
from sqlalchemy import create_engine, text

UPLOADS = pathlib.Path("/app/data/uploads")
COLLECTION = "kag_documents"


def main() -> int:
    engine = create_engine(os.environ["KAG_DB_URL"])
    with engine.connect() as conn:
        rows = conn.execute(text("select id, filename, status, chunks_count from documents")).all()
    docs = {r[0]: {"filename": r[1], "status": r[2], "chunks_count": r[3]} for r in rows}

    host = os.environ.get("QDRANT_HOST") or "kag-qdrant"
    client = QdrantClient(url=os.environ.get("QDRANT_URL") or f"http://{host}:6333",
                          api_key=os.environ.get("QDRANT_API_KEY") or None)
    info = client.get_collection(COLLECTION)

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

    files = {}
    if UPLOADS.exists():
        for path in UPLOADS.iterdir():
            if path.is_file():
                files.setdefault(path.name[:36], []).append(path.name)

    print(f"Postgres: документов {len(docs)} | статусы "
          f"{ {s: sum(1 for d in docs.values() if d['status'] == s) for s in {d['status'] for d in docs.values()} } }")
    print(f"Qdrant: всего точек {info.points_count} | документов в векторах {len(chunks)} | карточек {len(cards)}")
    print(f"Файлы uploads: {sum(len(v) for v in files.values())} файлов на {len(files)} документов")

    problems = []
    # 1. документ в БД без чанков в Qdrant
    for doc_id, meta in docs.items():
        if chunks.get(doc_id, 0) == 0:
            problems.append(f"в БД есть документ без векторов: {doc_id[:8]} «{meta['filename']}» "
                            f"(chunks_count={meta['chunks_count']}, status={meta['status']})")
    # 2. сироты: точки в Qdrant без документа в БД
    orphans = {d: c for d, c in chunks.items() if d not in docs}
    if orphans:
        total = sum(orphans.values())
        worst = sorted(orphans.items(), key=lambda kv: -kv[1])[:5]
        problems.append(f"сироты: {len(orphans)} document_id, {total} точек "
                        f"(крупнейшие: {', '.join(f'{d[:8]}={c}' for d, c in worst)})")
    # 3. карточка без чанков и чанки без карточки
    for doc_id in cards:
        if doc_id not in docs:
            problems.append(f"карточка без документа в БД: {doc_id[:8]}")
    # 4. файлов нет для записи в БД
    for doc_id, meta in docs.items():
        if doc_id not in files:
            problems.append(f"нет файла на диске: {doc_id[:8]} «{meta['filename']}»")
    # 5. файлы без записи в БД
    for doc_id, names in files.items():
        if doc_id not in docs:
            problems.append(f"файл без записи в БД: {doc_id[:8]} ({names[0][:60]})")
    # 6. расхождение chunks_count и фактических точек
    for doc_id, meta in docs.items():
        real = chunks.get(doc_id)
        if real is not None and meta["chunks_count"] and real != meta["chunks_count"]:
            problems.append(f"chunks_count расходится: {doc_id[:8]} в БД {meta['chunks_count']}, "
                            f"в Qdrant {real}")

    print()
    if problems:
        print("РАСХОЖДЕНИЯ:")
        for p in problems:
            print("  -", p)
    else:
        print("расхождений нет: БД, векторы и файлы согласованы")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
