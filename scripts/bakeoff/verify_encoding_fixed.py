"""Проверка, что после перезаливки текст документа читается как русский — в векторах и в графе.

Запуск на стенде:
    docker exec kag-api python /app/data/verify_encoding_fixed.py --filename 732.txt
    docker exec kag-api python /app/data/verify_encoding_fixed.py --all
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/data")

MOJI = ("Ð°", "Ð¾", "Ñ€", "Ñ‚")


def cyr(text: str) -> float:
    letters = [c for c in (text or "") if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if re.match(r"[а-яА-ЯёЁ]", c)) / len(letters)


def graph_sample(filename: str, limit: int = 3) -> list[str]:
    from neo4j import GraphDatabase

    drv = GraphDatabase.driver(
        os.environ.get("NEO4J_URI", "bolt://neo4j:7687"),
        auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "")),
    )
    with drv.session() as s:
        rows = [r["t"] for r in s.run(
            "MATCH (d:Document {filename: $f})-[:HAS_CHUNK]->(c:Chunk) "
            "RETURN c.text AS t LIMIT $n", f=filename, n=limit)]
        total = s.run(
            "MATCH (d:Document {filename: $f})-[:HAS_CHUNK]->(c:Chunk) RETURN count(c) AS n",
            f=filename).single()["n"]
        bad = s.run(
            "MATCH (d:Document {filename: $f})-[:HAS_CHUNK]->(c:Chunk) "
            "WHERE c.text CONTAINS $m1 OR c.text CONTAINS $m2 OR c.text CONTAINS $m3 "
            "RETURN count(c) AS n", f=filename, m1=MOJI[0], m2=MOJI[1], m3=MOJI[2]).single()["n"]
    drv.close()
    print(f"  граф: фрагментов {total}, испорченных {bad}")
    return rows


async def _service():
    """Служба векторов бывает и синглтоном, и фабрикой — берём то, что есть."""
    from src.indexing.embeddings_service import embeddings_service

    service = embeddings_service
    if callable(service) and not hasattr(service, "initialize"):
        service = service()
    if not service.is_initialized():
        await service.initialize()
    return service


async def _embed(service, text: str):
    """Вектор запроса: имя метода различается между версиями, поэтому пробуем по очереди."""
    import inspect

    for name in ("generate", "embed", "encode", "get_embedding", "create_embedding"):
        fn = getattr(service, name, None)
        if not fn:
            continue
        try:
            result = fn(text)
            if inspect.isawaitable(result):
                result = await result
        except Exception:
            continue
        if isinstance(result, dict):
            for key in ("embedding", "vector", "embeddings", "data"):
                if key in result:
                    result = result[key]
                    break
        if isinstance(result, list) and result and isinstance(result[0], list):
            result = result[0]
        if isinstance(result, list) and result and isinstance(result[0], (int, float)):
            return result
    return None


async def run_query(query: str, limit: int = 5) -> None:
    """Настоящая проверка поиска: вектор запроса → Qdrant → что нашлось."""
    from qdrant_client.http import models as qm

    service = await _service()
    vector = await _embed(service, query)
    if not vector:
        print("  не удалось получить вектор запроса (проверьте метод embeddings)")
        return
    points = service._qdrant_client.query_points(
        collection_name=service.collection_name, query=vector, limit=limit, with_payload=True
    ).points
    print(f"  запрос: «{query}»")
    for p in points:
        payload = p.payload or {}
        text = payload.get("content") or payload.get("text") or ""
        print(f"    {round(p.score, 3):>5} {str(payload.get('filename'))[:42]:44s} {text[:70]!r}")


async def qdrant_sample(filename: str, limit: int = 3) -> None:
    from src.api.services.document_repository import get_doc_repo

    docs = get_doc_repo().get_all() or {}
    target = None
    for did, doc in docs.items():
        name = doc.get("filename") if isinstance(doc, dict) else getattr(doc, "filename", None)
        if name == filename:
            target = did
            break
    if not target:
        print("  документа нет в базе")
        return

    service = await _service()

    from qdrant_client.http import models as qm

    points, _ = service._qdrant_client.scroll(
        collection_name=service.collection_name, limit=limit,
        scroll_filter=qm.Filter(must=[qm.FieldCondition(
            key="document_id", match=qm.MatchValue(value=target))]),
        with_payload=True, with_vectors=False)

    print(f"  вектор: документ {target[:8]}, точек показано {len(points)}")
    for p in points:
        payload = p.payload or {}
        text = payload.get("content") or payload.get("text") or ""
        print(f"  вектор: кириллицы {cyr(text):.2f} — {text[:90]!r}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--filename", default="", help="какой документ проверить по векторам и графу")
    ap.add_argument("--query", default="", help="поисковый запрос: вектор → Qdrant, что найдётся")
    args = ap.parse_args()

    if args.query:
        print(f"=== ПРОВЕРКА ПОИСКА ===")
        await run_query(args.query)
        return 0

    if not args.filename:
        print("нужен --filename или --query")
        return 2

    print(f"=== ПРОВЕРКА ЧТЕНИЯ ТЕКСТА: {args.filename} ===")
    for text in graph_sample(args.filename):
        print(f"  граф: кириллицы {cyr(text):.2f} — {text[:90]!r}")
    await qdrant_sample(args.filename)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
