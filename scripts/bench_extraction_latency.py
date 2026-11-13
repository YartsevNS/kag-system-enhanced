"""Замер: латентность извлечения и доля фрагментов с реальным вкладом.

Шаг 0 + шаг 1 из плана (docs/guides/ontology-extraction-speed.md):
  - сколько фрагментов вообще дают факты (raw, а не «новых»);
  - как ведёт себя вызов при параллельности 3 / 6 / 12: p50, p95, ошибки, 429, таймауты.

Ничего не пишет в TypeDB: только читает фрагменты из Qdrant и вызывает модель.
"""
import asyncio
import json
import statistics
import time

from src.indexing import typedb_writer
from src.indexing.embeddings_service import embeddings_service

DOC = "fb4d9a84-e723-41fa-970d-2ed75683674d"
SAMPLE = 30          # фрагментов на каждый уровень параллельности


async def one(text: str, idx: int) -> dict:
    prompt = typedb_writer._render_prompt(text, document="fstek-2014-mery-zashchity-gis.pdf")
    t0 = time.monotonic()
    try:
        payload = await asyncio.wait_for(typedb_writer._call_llm(prompt, f"bench-{idx}"), timeout=90)
        dt = time.monotonic() - t0
        ents = (payload or {}).get("entities") or []
        rels = (payload or {}).get("relations") or []
        return {"s": dt, "ents": len(ents), "rels": len(rels), "err": ""}
    except asyncio.TimeoutError:
        return {"s": time.monotonic() - t0, "ents": 0, "rels": 0, "err": "timeout"}
    except Exception as exc:  # noqa: BLE001
        return {"s": time.monotonic() - t0, "ents": 0, "rels": 0, "err": str(exc)[:80]}


async def level(chunks, n: int) -> dict:
    sem = asyncio.Semaphore(n)
    t0 = time.monotonic()

    async def run(i, text):
        async with sem:
            return await one(text, i)

    res = await asyncio.gather(*[run(i, c) for i, c in enumerate(chunks)])
    wall = time.monotonic() - t0
    times = sorted(r["s"] for r in res)
    p50 = statistics.median(times)
    p95 = times[int(len(times) * 0.95) - 1] if times else 0
    errs = [r["err"] for r in res if r["err"]]
    with_rels = sum(1 for r in res if r["rels"] > 0)
    with_ents = sum(1 for r in res if r["ents"] > 0)
    return {
        "параллельность": n,
        "запросов": len(res),
        "общее время, с": round(wall, 1),
        "p50, с": round(p50, 1),
        "p95, с": round(p95, 1),
        "теоретически, с": round(sum(times) / n, 1),
        "ошибок": len(errs),
        "примеры ошибок": errs[:3],
        "с сущностями": with_ents,
        "со связями": with_rels,
    }


async def main():
    await embeddings_service.initialize()
    pts = embeddings_service._qdrant_client.scroll(
        collection_name="kag_documents",
        scroll_filter={"must": [{"key": "document_id", "match": {"value": DOC}}]},
        limit=SAMPLE, with_payload=True, with_vectors=False)[0]
    chunks = [(p.payload or {}).get("content", "") for p in pts]
    chunks = [c for c in chunks if c]
    print("фрагментов в выборке:", len(chunks))

    out = []
    for n in (3, 6, 12):
        out.append(await level(chunks, n))
    print(json.dumps(out, ensure_ascii=False, indent=1))


asyncio.run(main())
