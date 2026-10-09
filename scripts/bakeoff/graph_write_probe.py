"""Прямой прогон извлечения по документу: берём его фрагмент сам и проверяем версии у фактов.

Отвечает на вопрос: если вызвать ту же функцию, что зовёт конвейер, появятся ли у фактов
schema_version/extractor_version и chunk_id. Если да — конвейер до записи не доходит (кэш/ветка),
и искать надо там; если нет — отличается путь записи.

Запуск: docker exec -e DOC_ID=<id> kag-system_worker_1 python /app/data/graph_write_probe.py
"""
import asyncio
import os

from neo4j import GraphDatabase
from qdrant_client import QdrantClient

from src.indexing.entity_extractor import entity_extractor

DOC = os.environ.get("DOC_ID", "")


def _drv():
    uri = os.environ.get("NEO4J_URI") or "bolt://neo4j:7687"
    return GraphDatabase.driver(uri, auth=(os.environ.get("NEO4J_USER", "neo4j"),
                                           os.environ.get("NEO4J_PASSWORD", "")))


def _qdrant():
    q = os.environ.get("QDRANT_HOST", "qdrant").replace("https://", "").replace("http://", "")
    return QdrantClient(url=f"http://{q}:6333", api_key=os.environ.get("QDRANT_API_KEY") or None)


async def main():
    d = _drv()
    with d.session() as s:
        rows = list(s.run("MATCH (doc:Document)-[:HAS_CHUNK]->(c:Chunk) WHERE doc.id = $doc "
                          "RETURN c.id AS id, c.chunk_seq AS seq LIMIT 3", doc=DOC))
    print("фрагменты документа:", [(r["id"][-12:], r["seq"]) for r in rows])
    if not rows:
        print("у документа нет узлов Chunk в графе")
        return
    cid = rows[0]["id"]

    c = _qdrant()
    text = ""
    try:
        pts, _ = c.scroll(collection_name="kag_documents", limit=500, with_payload=True, with_vectors=False)
        for p in pts:
            pl = p.payload or {}
            if str(pl.get("chunk_id") or "") == cid:
                text = " ".join(str(pl.get("content") or pl.get("text") or "").split())
                break
    except Exception as e:  # noqa: BLE001
        print("не смог прочитать текст из Qdrant:", str(e)[:120])
    print("текст найден:", len(text), "знаков")

    before = list(s.run("MATCH (c:Chunk {id: $cid})-[:MENTIONS]->(e:Entity) RETURN e.name AS n, keys(e) AS k",
                        cid=cid)) if False else []
    async def run():
        return await entity_extractor.extract_and_store(DOC, cid, text, 1, "probe")
    res = await run()
    print("результат извлечения:", {k: (len(v) if isinstance(v, list) else v) for k, v in (res or {}).items()})

    with d.session() as s:
        rows2 = list(s.run("MATCH (c:Chunk {id: $cid})-[:MENTIONS]->(e:Entity) RETURN e.name AS n, keys(e) AS k LIMIT 5",
                           cid=cid))
        print("сущности после прямого вызова:")
        for r in rows2:
            print(f"   {str(r['n'])[:36]:<36} версия: {'schema_version' in r['k']}")
        rel = list(s.run("MATCH (c:Chunk {id: $cid})-[:MENTIONS]->(:Entity)<-[r]-(:Entity) "
                         "RETURN type(r) AS t, keys(r) AS k LIMIT 3", cid=cid))
        for r in rel:
            print(f"   связь {r['t']}: фрагмент {'chunk_id' in r['k']}, версия {'schema_version' in r['k']}")
    d.close()


if __name__ == "__main__":
    asyncio.run(main())
