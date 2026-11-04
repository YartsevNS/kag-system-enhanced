#!/usr/bin/env python3
"""Сверка counts: документы в БД против точек в Qdrant.

ВАЖНО: коллекция определяется is_news_document (как в пайплайне). Иначе документы
с collection=news считаются в kag_documents и дают ложные аномалии (1 вместо N).
Запуск в контейнере: docker exec kag-system_worker_1 python /tmp/audit_qdrant.py
"""
"""Аудит Qdrant vs БД: расхождения counts и сироты. Только чтение.

Для каждого документа: exact count точек по document_id vs chunks_count из БД.
Плюс: document_id в Qdrant, которых нет в реестре (сироты).
"""
import os
from collections import Counter

from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue

key = os.environ.get("QDRANT_API_KEY", "")
c = QdrantClient(url=os.environ.get("QDRANT_URL", "http://kag-qdrant:6333"), api_key=key or None)

from src.api.services.document_repository import get_doc_repo
from src.indexing.embeddings_service import is_news_document
repo = get_doc_repo()
docs, total = repo.list(limit=1000)
db_ids = {d.id: d for d in docs}
print(f"документов в БД: {total}")


def coll_for(d):
    """Коллекция документа: НЕ всегда kag_documents. При пустом collection маршрут
    определяется метаданными источника (web_monitor), поэтому спрашиваем ту же
    функцию, что и пайплайн — иначе аудит считает нули там, где точки в kag_news."""
    return "kag_news" if is_news_document({"collection": d.collection, "source_metadata": d.source_metadata}) else "kag_documents"


mismatch = []
for d in docs:
    coll = coll_for(d)
    try:
        res = c.count(collection_name=coll,
                      count_filter=Filter(must=[FieldCondition(key="document_id", match=MatchValue(value=d.id))]),
                      exact=True)
        in_q = res.count
    except Exception as e:
        mismatch.append((d.id, d.chunks_count, f"ERR {e}"))
        continue
    exp = d.chunks_count or 0
    if in_q != exp:
        mismatch.append((d.id, exp, in_q, f"[{coll}] {(d.filename or '')[:40]}"))

print(f"расхождений counts: {len(mismatch)}")
# В коллекции кроме чанков лежит отдельная точка-карточка документа (chunk_id='card',
# level='document'), которая НЕ входит в chunks_count. Поэтому норма — diff = +1.
normal = [m for m in mismatch if len(m) == 4 and isinstance(m[2], int) and m[2] - m[1] == 1]
anomaly = [m for m in mismatch if not (len(m) == 4 and isinstance(m[2], int) and m[2] - m[1] == 1)]
print(f"  из них норма (diff=+1, точка карточки): {len(normal)}")
print(f"  АНОМАЛИЙ: {len(anomaly)}")
for m in anomaly[:25]:
    print("  ", m)

# сироты: сканируем Qdrant, собираем document_id, которых нет в БД
seen = Counter()
offset = None
while True:
    pts, offset = c.scroll(collection_name="kag_documents", limit=1024, offset=offset,
                           with_payload=["document_id"], with_vectors=False)
    if not pts:
        break
    for p in pts:
        did = (p.payload or {}).get("document_id")
        if did:
            seen[did] += 1
    if offset is None:
        break

orphans = {k: v for k, v in seen.items() if k not in db_ids}
print(f"\nуникальных document_id в Qdrant: {len(seen)}, точек: {sum(seen.values())}")
print(f"сирот (нет в БД): {len(orphans)}, точек у них: {sum(orphans.values())}")
for k, v in list(orphans.items())[:10]:
    print(f"   {k[:8]} — {v} точек")
