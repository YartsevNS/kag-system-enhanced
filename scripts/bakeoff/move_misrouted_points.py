"""Перенос точек документа в ТУ коллекцию, где его маршрут.

ЗАЧЕМ. Маршрут документа (поле `collection`) решает, в какой коллекции Qdrant лежат его векторы.
У части документов маршрут и данные разошлись: документ с маршрутом `news` имеет точки в основной
коллекции (расхождение старше правки словарей). Следствие: штатное удаление такого документа чистит
свою коллекцию и ОСТАВЛЯЕТ точки в чужой — сироты, которые потом никто не уберёт.

ЧТО ДЕЛАЕТ. Для КАЖДОГО документа сравнивает маршрут с тем, где реально лежат точки; если точки не
там — переносит их (тот же id точки, тот же payload и вектор: id детерминированный, см.
`src/indexing/ids.py`) и удаляет из чужой коллекции. Идемпотентен: совпадающие документы не трогает.

ЗАПУСК в контейнере api:
  docker exec kag-api python /app/data/move_misrouted_points.py            # сухой прогон
  docker exec kag-api python /app/data/move_misrouted_points.py --apply
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter

from qdrant_client.models import FieldCondition, Filter, MatchValue, PointStruct


async def main() -> int:
    from src.api.services.document_repository import get_doc_repo
    from src.indexing.embeddings_service import (
        embeddings_service, news_embeddings_service, service_for_document,
    )

    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="перенести (по умолчанию сухой прогон)")
    ap.add_argument("--limit", type=int, default=0, help="ограничить число документов (для пробы)")
    args = ap.parse_args()

    main_svc, news_svc = embeddings_service, news_embeddings_service()
    await main_svc.initialize()
    await news_svc.initialize()
    by_name = {main_svc.collection_name: main_svc, news_svc.collection_name: news_svc}

    async def count(svc, did: str) -> int:
        flt = Filter(must=[FieldCondition(key="document_id", match=MatchValue(value=did))])
        res = await asyncio.to_thread(
            svc._qdrant_client.count, collection_name=svc.collection_name,
            count_filter=flt, exact=True)
        return int(getattr(res, "count", 0) or 0)

    docs = get_doc_repo().get_all()
    print(f"документов в реестре: {len(docs)}")
    before = {name: int((await svc.get_collection_stats()).get("points_count") or 0)
              for name, svc in by_name.items()}
    print(f"точек до: {before}")

    misrouted = []      # (doc_id, откуда, куда, сколько)
    for i, (did, d) in enumerate(docs.items()):
        if args.limit and i >= args.limit:
            break
        target = service_for_document(did).collection_name
        own = await count(by_name[target], did)
        others = {name: await count(svc, did) for name, svc in by_name.items() if name != target}
        stray = sum(others.values())
        if stray:
            misrouted.append((did, others, target, stray, own))

    print(f"документов с точками не в своей коллекции: {len(misrouted)}")
    total_stray = sum(m[3] for m in misrouted)
    print(f"точек к переносу: {total_stray}")
    for did, others, target, stray, own in misrouted[:10]:
        src = ", ".join(f"{n}: {c}" for n, c in others.items() if c)
        print(f"   {did[:12]} {src} → {target} (своих там {own})")
    if not misrouted:
        print("переносить нечего — маршрут и данные совпадают")
        return 0
    if not args.apply:
        print("СУХОЙ ПРОГОН: ничего не записано (для применения — флаг --apply)")
        return 0

    moved = failed = 0
    for did, others, target, stray, own in misrouted:
        dst = by_name[target]
        try:
            for src_name, cnt in others.items():
                if not cnt:
                    continue
                src = by_name[src_name]
                flt = Filter(must=[FieldCondition(key="document_id", match=MatchValue(value=did))])
                points_batch = []
                offset = None
                while True:
                    pts, offset = await asyncio.to_thread(
                        src._qdrant_client.scroll, collection_name=src.collection_name,
                        scroll_filter=flt, limit=256, with_payload=True, with_vectors=True,
                        offset=offset)
                    for p in pts:
                        points_batch.append(PointStruct(id=p.id, vector=p.vector, payload=p.payload or {}))
                    if offset is None:
                        break
                if points_batch:
                    await asyncio.to_thread(dst._qdrant_client.upsert,
                                            collection_name=dst.collection_name, points=points_batch)
                await asyncio.to_thread(src._qdrant_client.delete,
                                        collection_name=src.collection_name,
                                        points_selector=flt, wait=True)
            moved += 1
        except Exception as e:  # noqa: BLE001 — один документ не должен ронять перенос
            failed += 1
            print(f"   ошибка {did[:12]}: {type(e).__name__}: {e}")

    after = {name: int((await svc.get_collection_stats()).get("points_count") or 0)
             for name, svc in by_name.items()}
    print(f"\nперенесено документов: {moved}, ошибок: {failed}")
    print(f"точек после: {after}")
    for name in before:
        delta = after[name] - before[name]
        print(f"   {name}: {before[name]} → {after[name]} ({delta:+d})")

    # Проверка результата: у каждого перенесённого документа точки ТОЛЬКО в своей коллекции
    left = 0
    for did, others, target, stray, own in misrouted:
        others2 = {n: await count(svc, did) for n, svc in by_name.items() if n != target}
        left += sum(others2.values())
        if not await count(by_name[target], did):
            print(f"   ВНИМАНИЕ: у {did[:12]} в целевой коллекции точек не появилось")
    print(f"осталось точек не в своей коллекции: {left}")
    return 0 if (failed == 0 and left == 0) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
