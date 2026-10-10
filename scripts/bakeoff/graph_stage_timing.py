"""Раскладка времени построения графа по этапам: извлечение против записи в Neo4j.

Зачем прибор. Общий замер «документ строится 20 минут» ничего не говорит о причине: время может уходить
в модель, в две записи на фрагмент, в таймауты и повторы, или в кэш. Прибор повторяет ТОТ ЖЕ цикл, что и
конвейер (узел фрагмента → извлечение с записью), и печатает время по этапам на каждый фрагмент плюс
итог. Записи идемпотентны (MERGE), поэтому прогон на документе, чей граф уже построен, ничего не портит.

Два режима замера:
  --mode cache   — текст как есть: показывает стоимость ПОВТОРНОГО построения (попадание в кэш);
  --mode fresh   — к тексту добавляется уникальный хвост: показывает стоимость НАСТОЯЩЕГО извлечения.

Запуск внутри контейнера api:
  docker exec kag-api python /app/data/graph_stage_timing.py --doc <id> --chunks 3 --mode fresh
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.request


def qdrant_points(doc_id: str, limit: int) -> list:
    """Фрагменты документа как их видит конвейер: из Qdrant (content + chunk_id)."""
    from src.config import get_settings

    s = get_settings()
    base = f"http://{s.QDRANT_HOST}:{s.QDRANT_PORT}"
    headers = {"Content-Type": "application/json"}
    if getattr(s, "QDRANT_API_KEY", ""):
        headers["api-key"] = s.QDRANT_API_KEY
    out = []
    for coll in ("kag_documents", "kag_news"):
        body = json.dumps({"filter": {"must": [{"key": "document_id", "match": {"value": doc_id}}]},
                           "limit": limit, "with_payload": True}).encode()
        req = urllib.request.Request(f"{base}/collections/{coll}/points/scroll", body, headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                out += json.loads(r.read().decode())["result"]["points"]
        except Exception as e:  # noqa: BLE001
            print(f"  (коллекция {coll}: {type(e).__name__} {str(e)[:60]})")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc", required=True)
    ap.add_argument("--chunks", type=int, default=3)
    ap.add_argument("--mode", choices=("cache", "fresh"), default="fresh")
    args = ap.parse_args()

    from src.indexing.entity_extractor import entity_extractor
    from src.indexing.knowledge_graph import kg_service

    points = qdrant_points(args.doc, args.chunks)
    if not points:
        print("фрагментов документа не найдено в Qdrant")
        return 1
    payload = points[0].get("payload") or {}
    print(f"поля payload фрагмента: {sorted(payload.keys())[:14]}")

    t_doc = time.monotonic()
    kg_service.create_document_node(args.doc, payload.get("filename", "profile"), {})
    print(f"узел документа: {(time.monotonic() - t_doc):.2f} с")

    totals = {"neo4j": 0.0, "extract": 0.0, "entities": 0, "relations": 0}
    print(f"\n{'#':>3} {'neo4j,с':>8} {'извлеч.,с':>10} {'сущн.':>6} {'связей':>7}  предупреждения")
    for i, p in enumerate(points, 1):
        pl = p.get("payload") or {}
        text = pl.get("content") or ""
        chunk_id = pl.get("chunk_id") or p.get("id") or f"{args.doc}_chunk_{i}"
        seq = int(pl.get("chunk_seq") or pl.get("chunk_index") or i)
        if args.mode == "fresh":
            text = text + "  " + os.urandom(4).hex()

        t0 = time.monotonic()
        kg_service.create_chunk_node(str(chunk_id), args.doc, text, seq)
        neo4j_s = time.monotonic() - t0

        t1 = time.monotonic()
        # Важно: измеряем ТОТ ЖЕ вызов, что и конвейер (extract_and_store = извлечение + запись
        # сущностей и связей в граф). extract_from_chunk мерил бы только модель и скрывал бы
        # стоимость записей, которые у нас делаются по одной на сущность.
        res = asyncio.run(entity_extractor.extract_and_store(
            args.doc, str(chunk_id), text, seq, pl.get("filename", "")))
        res = res if isinstance(res, dict) else {}
        extract_s = time.monotonic() - t1
        ents = len(res.get("entities") or [])
        rels = len(res.get("relations") or [])
        totals["neo4j"] += neo4j_s
        totals["extract"] += extract_s
        totals["entities"] += ents
        totals["relations"] += rels
        print(f"{i:>3} {neo4j_s:>8.2f} {extract_s:>10.2f} {ents:>6} {rels:>7}  {(res.get('warnings') or [])[:1]}")

    n = len(points)
    print(f"\nитог по {n} фрагментам ({args.mode}):")
    print(f"  запись в Neo4j:  {totals['neo4j']:6.1f} с ({totals['neo4j'] / n:.1f} с на фрагмент)")
    print(f"  извлечение:      {totals['extract']:6.1f} с ({totals['extract'] / n:.1f} с на фрагмент)")
    print(f"  сущностей {totals['entities']}, связей {totals['relations']}")
    print(f"  пересчёт на документ из 10 фрагментов: "
          f"{(totals['neo4j'] + totals['extract']) / n * 10 / 60:.1f} минут последовательно")
    print(f"  пересчёт на корпус (10 863 фрагмента, 6 параллельно): "
          f"{(totals['neo4j'] + totals['extract']) / n * 10863 / 3600 / 6:.1f} часов")
    return 0


if __name__ == "__main__":
    sys.exit(main())
