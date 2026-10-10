"""Замер задержек модели на конкретных фрагментах: где на самом деле висит обработка.

Повод: в пересборке видно «Извлечение сущностей таймаут (120с) — пропуск чанка» на пяти фрагментах
одного документа. Увеличение таймаута проблему не решило — значит дело не в пороге, а в самих вызовах.
Гадать нельзя: измеряем по отдельности
  * полный проход извлечения (два вызова: сущности, затем связи) — на фрагменте;
  * один «сырой» вызов модели с коротким запросом — это чистая задержка провайдера;
  * размеры ответа (сколько сущностей и связей вернулось).

Запуск на стенде:
    docker exec kag-api python /app/data/llm_latency_probe.py --filename ScansourceInc... --chunks 5
    docker exec kag-api python /app/data/llm_latency_probe.py --chunks 3 --raw
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

sys.path.insert(0, "/app")


async def service():
    from src.indexing.embeddings_service import embeddings_service

    svc = embeddings_service
    if callable(svc) and not hasattr(svc, "initialize"):
        svc = svc()
    if not svc.is_initialized():
        await svc.initialize()
    return svc


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--filename", default="", help="документ (точно по имени)")
    ap.add_argument("--match", default="", help="подстрока в имени документа (когда точное имя неизвестно)")
    ap.add_argument("--chunks", type=int, default=5)
    ap.add_argument("--chars", type=int, default=1500, help="сколько знаков фрагмента отдавать модели")
    args = ap.parse_args()

    from qdrant_client.http import models as qm

    from src.api.services.document_repository import get_doc_repo
    from src.indexing.entity_extractor import entity_extractor

    svc = await service()
    docs = get_doc_repo().get_all() or {}
    target = None
    for did, d in docs.items():
        if not isinstance(d, dict):
            continue
        if args.filename and (d.get("filename") or "") != args.filename:
            continue
        if args.match and args.match.lower() not in (d.get("filename") or "").lower():
            continue
        if int(d.get("chunks_count") or 0) > 0:
            target = (did, d.get("filename") or "")
            break
    if not target:
        print("документ с фрагментами не найден")
        return 2
    doc_id, name = target
    print(f"документ: {name} ({doc_id[:8]})")

    points, _ = svc._qdrant_client.scroll(
        collection_name=svc.collection_name, limit=args.chunks,
        scroll_filter=qm.Filter(must=[qm.FieldCondition(
            key="document_id", match=qm.MatchValue(value=doc_id))]),
        with_payload=True, with_vectors=False)
    print(f"фрагментов взято: {len(points)}\n")

    for n, p in enumerate(points, 1):
        payload = p.payload or {}
        text = (payload.get("content") or payload.get("text") or "")[: args.chars]
        if len(text) < 50:
            continue
        chunk_id = payload.get("chunk_id") or str(p.id)
        # Длину текста печатаем: если задержка растёт вместе с ней, дело в размере запроса.
        t0 = time.monotonic()
        try:
            res = await asyncio.wait_for(
                entity_extractor.extract_from_chunk(chunk_text=text, chunk_id=chunk_id,
                                                    document_id=doc_id, filename=name),
                timeout=180)
            dur = time.monotonic() - t0
            ents = len(res.get("entities") or [])
            rels = len(res.get("relations") or [])
            warns = res.get("warnings") or []
            print(f"  {n}. знаков {len(text):5d} | {dur:6.1f} с | сущностей {ents:3d} "
                  f"связей {rels:3d} | {warns[:1]}")
        except asyncio.TimeoutError:
            dur = time.monotonic() - t0
            print(f"  {n}. знаков {len(text):5d} | ТАЙМАУТ на {dur:.1f} с — вызов не вернулся")
        except Exception as exc:  # noqa: BLE001
            dur = time.monotonic() - t0
            print(f"  {n}. знаков {len(text):5d} | ошибка за {dur:.1f} с: {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
