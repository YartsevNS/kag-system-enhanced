"""Что модель возвращает и что из этого выживает: один фрагмент документа.

Печатает поля payload точки Qdrant, затем сырой ответ модели (типы сущностей и связи)
и результат _collect (сколько записей прошло валидацию и почему отброшены).
"""
import asyncio
import json
from collections import Counter

from src.indexing import typedb_writer
from src.indexing.embeddings_service import embeddings_service

DOC = "fb4d9a84-e723-41fa-970d-2ed75683674d"


async def main():
    await embeddings_service.initialize()
    client = embeddings_service._qdrant_client
    pts = client.scroll(collection_name="kag_documents",
                        scroll_filter={"must": [{"key": "document_id", "match": {"value": DOC}}]},
                        limit=2, with_payload=True, with_vectors=False)[0]
    if not pts:
        print("нет точек"); return
    payload = pts[0].payload or {}
    print("поля payload:", sorted(payload.keys())[:12])
    text = payload.get("text") or payload.get("content") or payload.get("page_content") or ""
    print("длина текста:", len(text))
    if not text:
        return

    prompt = typedb_writer._render_prompt(text, document="fstek-2014-mery-zashchity-gis.pdf")
    raw = await typedb_writer._call_llm(prompt, "probe2")
    if not raw:
        print("модель не вернула JSON"); return
    ents = raw.get("entities") or []
    rels = raw.get("relations") or []
    print("сырых сущностей:", len(ents), "связей:", len(rels))
    print("типы сущностей:", Counter(str(e.get("type")) for e in ents if isinstance(e, dict)).most_common())
    for e in ents[:4]:
        if isinstance(e, dict):
            print("  пример:", json.dumps(e, ensure_ascii=False)[:220])

    entities, relations, stats = typedb_writer._collect(raw, "probe-chunk", DOC)
    print("после _collect: сущностей", len(entities), "связей", len(relations), "stats", stats)


asyncio.run(main())
