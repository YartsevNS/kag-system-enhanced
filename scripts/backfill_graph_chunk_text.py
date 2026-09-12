#!/usr/bin/env python3
"""Бэкфилл Chunk.text в Neo4j из Qdrant (без переиндексации, эмбеддингов и LLM).

Зачем: граф должен отдавать фрагменты чанков, а не «первые 500 символов».
Источник правды по тексту — payload.content в Qdrant; в узле Chunk теперь
хранится ПОЛНАЯ копия (c.text), но у чанков, записанных старым кодом, в графе
лежит только text_preview (500 символов). Скрипт берёт текст из Qdrant точечно
по qdrant_point_id (или вычисляет его из chunk_id) и перезаписывает c.text.

Запуск (нужен neo4j-драйвер — он есть в образах api/worker):

    docker cp scripts/backfill_graph_chunk_text.py kag-api:/tmp/
    docker exec kag-api python /tmp/backfill_graph_chunk_text.py            # dry-run
    docker exec kag-api python /tmp/backfill_graph_chunk_text.py --apply    # запись

Опции:
    --apply           записывать (без флага — только посчитать, что будет изменено)
    --only-empty      трогать только узлы без c.text (по умолчанию — все, включая
                      обрезанные до 500 символов)
    --only-short N    трогать узлы, где c.text короче N символов (критерий «обрезано»)
    --batch N         размер пачки при чтении из Qdrant и записи в Neo4j (200)
    --limit N         ограничить число узлов (для пробного прогона)

Проверка после прогона:
    docker exec kag-neo4j cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
      "MATCH (c:Chunk) WHERE c.text IS NOT NULL RETURN count(c), min(size(c.text)), max(size(c.text))"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

CHUNK_NAMESPACE_PREFIX = "kag-chunk:"


def point_id_for_chunk(chunk_id: str, document_id: str | None = None) -> str:
    """Тот же расчёт, что src/indexing/ids.py (чтобы не зависеть от версии образа)."""
    if not chunk_id:
        raise ValueError("chunk_id обязателен")
    key = f"{document_id}:{chunk_id}" if document_id else chunk_id
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{CHUNK_NAMESPACE_PREFIX}{key}"))


def qdrant_get_contents(url: str, api_key: str, collection: str, ids: list[str],
                        timeout: float = 120.0) -> dict:
    """Точечный retrieve: {point_id: content} (без векторов, только payload)."""
    body = json.dumps({"ids": ids, "with_payload": ["content"], "with_vector": False}).encode()
    req = urllib.request.Request(f"{url.rstrip('/')}/collections/{collection}/points",
                                 data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("api-key", api_key)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    out = {}
    for p in data.get("result") or []:
        pl = p.get("payload") or {}
        if pl.get("content") is not None:
            out[str(p.get("id"))] = pl["content"]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Бэкфилл Chunk.text в Neo4j из Qdrant")
    ap.add_argument("--apply", action="store_true", help="записывать в Neo4j (иначе dry-run)")
    ap.add_argument("--only-empty", action="store_true", help="только узлы без c.text")
    ap.add_argument("--only-short", type=int, default=0,
                    help="только узлы, где size(c.text) < N (например 500 — «обрезанные»)")
    ap.add_argument("--batch", type=int, default=200)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--neo4j-uri", default=os.environ.get("NEO4J_URI", "bolt://neo4j:7687"))
    ap.add_argument("--neo4j-user", default=os.environ.get("NEO4J_USER", "neo4j"))
    ap.add_argument("--neo4j-password", default=os.environ.get("NEO4J_PASSWORD", ""))
    ap.add_argument("--qdrant-url", default=None)
    ap.add_argument("--qdrant-key", default=os.environ.get("QDRANT_API_KEY", ""))
    ap.add_argument("--collection", default=os.environ.get("QDRANT_COLLECTION", "kag_documents"))
    args = ap.parse_args()

    if args.qdrant_url is None:
        host = os.environ.get("QDRANT_HOST", "kag-qdrant")
        port = os.environ.get("QDRANT_PORT", "6333")
        args.qdrant_url = f"http://{host}:{port}"

    try:
        from neo4j import GraphDatabase
    except ImportError:
        print("Нет neo4j-драйвера: запускайте внутри контейнера api/worker "
              "(docker cp + docker exec), а не на хосте.")
        return 2

    driver = GraphDatabase.driver(args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_password))
    driver.verify_connectivity()
    print(f"Neo4j: {args.neo4j_uri} | Qdrant: {args.qdrant_url} | "
          f"режим: {'ЗАПИСЬ' if args.apply else 'dry-run'}")

    where = []
    if args.only_empty:
        where.append("(c.text IS NULL OR size(c.text) = 0)")
    if args.only_short:
        where.append(f"size(coalesce(c.text, '')) < {int(args.only_short)}")
    where_str = ("WHERE " + " AND ".join(where)) if where else ""
    limit_str = f"LIMIT {int(args.limit)}" if args.limit else ""

    with driver.session() as session:
        rows = [dict(r) for r in session.run(
            f"""
            MATCH (d:Document)-[:HAS_CHUNK]->(c:Chunk)
            {where_str}
            RETURN c.id AS chunk_id, c.qdrant_point_id AS pid,
                   d.id AS doc_id, size(coalesce(c.text, '')) AS cur_len
            ORDER BY c.id
            {limit_str}
            """)]
        total_tail = session.run("MATCH (c:Chunk) RETURN count(c) AS n").single()["n"]

    print(f"Чанк-узлов всего в графе: {total_tail}; попало под условие: {len(rows)}")
    if not rows:
        print("Нечего делать.")
        return 0

    # Кому не хватает qdrant_point_id — считаем детерминированно из chunk_id + document_id
    computed = 0
    for r in rows:
        if not r.get("pid"):
            r["pid"] = point_id_for_chunk(r["chunk_id"], r.get("doc_id"))
            computed += 1
    if computed:
        print(f"  qdrant_point_id отсутствовал у {computed} узлов — вычислен из chunk_id/doc_id")

    updated = skipped_missing = same = 0
    added_bytes = 0
    samples = []
    t0 = time.time()

    for start in range(0, len(rows), args.batch):
        chunk = rows[start:start + args.batch]
        by_pid = {r["pid"]: r for r in chunk}
        try:
            contents = qdrant_get_contents(args.qdrant_url, args.qdrant_key,
                                           args.collection, list(by_pid))
        except urllib.error.HTTPError as e:
            print(f"  Qdrant HTTP {e.code}: {e.read().decode()[:150]}")
            skipped_missing += len(chunk)
            continue

        writes = []
        for pid, r in by_pid.items():
            content = contents.get(pid)
            if content is None:
                skipped_missing += 1
                continue
            if len(content) == r["cur_len"] and r["cur_len"] > 0:
                same += 1
                continue
            writes.append({"id": r["chunk_id"], "text": content})
            added_bytes += max(0, len(content) - r["cur_len"])
            if len(samples) < 5:
                samples.append((r["chunk_id"], r["cur_len"], len(content)))

        if args.apply and writes:
            with driver.session() as session:
                session.run(
                    """
                    UNWIND $rows AS row
                    MATCH (c:Chunk {id: row.id})
                    SET c.text = row.text, c.updated_at = datetime()
                    """, rows=writes)
        updated += len(writes)
        print(f"  обработано {min(start + args.batch, len(rows))}/{len(rows)}: "
              f"к записи {len(writes)}, без изменений {same}, нет точки в Qdrant {skipped_missing}")

    if args.apply:
        with driver.session() as session:
            # Старое свойство-дубль больше не нужно: text_preview — префикс c.text.
            removed = session.run(
                "MATCH (c:Chunk) WHERE c.text_preview IS NOT NULL "
                "REMOVE c.text_preview RETURN count(c)").single()[0]
            stats = session.run(
                "MATCH (c:Chunk) WHERE c.text IS NOT NULL "
                "RETURN count(c) AS with_text, min(size(c.text)) AS min_len, "
                "max(size(c.text)) AS max_len").single()
        print(f"\nСнято устаревшее свойство text_preview: {removed}")
        print(f"Узлов с текстом: {stats['with_text']}, длина min={stats['min_len']} "
              f"max={stats['max_len']}")

    print(f"\nИтог: {'обновлено' if args.apply else 'будет обновлено'} {updated}; "
          f"без изменений {same}; нет точки в Qdrant {skipped_missing}; "
          f"прирост текста ~{added_bytes / 1024:.0f} КБ; {time.time() - t0:.1f} с")
    if samples:
        print("Примеры (chunk_id, было, станет):")
        for cid, before, after in samples:
            print(f"  {cid}: {before} -> {after}")
    if not args.apply:
        print("\nЭто dry-run. Для записи повторите с --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
