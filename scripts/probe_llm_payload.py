"""Что именно возвращает модель для меры и требования — сырой ответ на один фрагмент.

Диагностика ключей: в базе у 200 мер одинаковый measure_key, у 393 требований —
11 разных requirement_key. Нужно увидеть, что модель кладёт в эти поля, а не гадать.
Берём текст фрагмента из Qdrant по document_id и прогоняем тот же промпт, что в потоке.
"""
import asyncio
import json

from src.indexing.embeddings_service import embeddings_service
from src.indexing import typedb_writer

DOC = "fb4d9a84-e723-41fa-970d-2ed75683674d"


async def main():
    await embeddings_service.initialize()
    client = getattr(embeddings_service, '_qdrant_client', None)
    print('client:', type(client).__name__)
    points = client.scroll(
        collection_name="kag_documents",
        scroll_filter={"must": [{"key": "document_id", "match": {"value": DOC}}]},
        limit=3, with_payload=True, with_vectors=False,
    )[0]
    if not points:
        print("фрагментов не найдено")
        return
    text = (points[0].payload or {}).get("text") or ""
    print("фрагмент:", len(text), "символов")
    prompt = typedb_writer._render_prompt(text, document="fstek-2014-mery-zashchity-gis.pdf")
    payload = await typedb_writer._call_llm(prompt, "probe")
    if not payload:
        print("модель не вернула JSON")
        return
    ents = payload.get("entities") or []
    print("сущностей:", len(ents))
    seen = set()
    for e in ents:
        t = e.get("type")
        if t in seen:
            continue
        seen.add(t)
        print(json.dumps(e, ensure_ascii=False)[:400])


try:
    asyncio.run(main())
except Exception as e:
    import traceback; traceback.print_exc()
