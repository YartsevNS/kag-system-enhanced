"""Собрать фрагменты для разметки: разнообразные прозаические куски из коллекции документов.

Отбор: только текстовые фрагменты (без таблиц и служебных карточек), длиной 300–1200 знаков,
не более 3 на документ — чтобы не размечать десять кусков одного ГОСТа.

Печатает JSON в файл: /app/data/jev_label_chunks.json
"""
import json
import os
from collections import defaultdict

from qdrant_client import QdrantClient

q = os.environ.get("QDRANT_HOST", "qdrant").replace("https://", "").replace("http://", "")
cl = QdrantClient(url=f"http://{q}:6333", api_key=os.environ.get("QDRANT_API_KEY") or None)

points, offset = [], None
while len(points) < 3000:
    batch, offset = cl.scroll(collection_name="kag_documents", limit=500, with_payload=True,
                              with_vectors=False, offset=offset)
    points.extend(batch)
    if offset is None or not batch:
        break

per_doc = defaultdict(int)
picked = []
for p in points:
    pl = p.payload or {}
    text = " ".join(str(pl.get("content") or pl.get("text") or "").split())
    cid = str(pl.get("chunk_id") or "")
    did = str(pl.get("document_id") or "")
    if not text or "chunk" not in cid:
        continue
    if (pl.get("chunk_type") or "") == "table":
        continue
    if not (300 <= len(text) <= 1200):
        continue
    if per_doc[did] >= 3:
        continue
    per_doc[did] += 1
    picked.append({"text": text, "chunk_id": cid, "document_id": did,
                   "filename": " ".join(str(pl.get("filename") or "").split())[:80]})

out = "/app/data/jev_label_chunks.json"
with open(out, "w", encoding="utf-8") as f:
    json.dump(picked, f, ensure_ascii=False)
print("собрано фрагментов:", len(picked), "| документов:", len(per_doc))
print("пример:", picked[0]["filename"][:60] if picked else "—")
print("файл:", out)
