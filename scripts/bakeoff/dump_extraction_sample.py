"""Выгрузка образца извлечения: что наша модель вытащила из фрагментов (для оценки точности).

Точность графа до сих пор не измерялась ни разу: непонятно, сколько сущностей и связей извлечено
верно. Чтобы оценить, нужен образец «текст → что мы извлекли», а судить его будет отдельная модель
(JEV, закрытые вопросы с вероятностями). Этот прибор готовит образец на стенде.

Запуск на стенде:
    docker exec kag-api python /app/data/dump_extraction_sample.py --docs 5 --per-doc 3
    # результат: /app/data/extraction_sample.json (скачать на ноутбук и судить прибором jev_graph_accuracy.py)
"""

from __future__ import annotations

import argparse
import asyncio
import json
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
    ap.add_argument("--docs", type=int, default=5, help="сколько документов взять")
    ap.add_argument("--per-doc", type=int, default=3, help="сколько фрагментов на документ")
    ap.add_argument("--min-chunks", type=int, default=8, help="не брать заглушки")
    ap.add_argument("--out", default="/app/data/extraction_sample.json")
    args = ap.parse_args()

    from qdrant_client.http import models as qm

    from src.api.services.document_repository import get_doc_repo
    from src.indexing.entity_extractor import entity_extractor

    svc = await service()
    docs = [(did, d) for did, d in (get_doc_repo().get_all() or {}).items()
            if isinstance(d, dict) and int(d.get("chunks_count") or 0) >= args.min_chunks]
    docs.sort(key=lambda kv: int(kv[1].get("chunks_count") or 0))
    docs = docs[: args.docs]

    sample = []
    for did, d in docs:
        fn = d.get("filename") or ""
        points, _ = svc._qdrant_client.scroll(
            collection_name=svc.collection_name, limit=args.per_doc,
            scroll_filter=qm.Filter(must=[qm.FieldCondition(
                key="document_id", match=qm.MatchValue(value=did))]),
            with_payload=True, with_vectors=False)
        for p in points:
            payload = p.payload or {}
            text = (payload.get("content") or payload.get("text") or "").strip()
            if len(text) < 300:
                continue
            chunk_id = payload.get("chunk_id") or str(p.id)
            t0 = time.monotonic()
            try:
                res = await entity_extractor.extract_from_chunk(
                    chunk_text=text[:3000], chunk_id=chunk_id, document_id=did, filename=fn)
            except Exception as exc:  # noqa: BLE001
                print(f"  извлечение не удалось для {fn}: {exc}")
                continue
            sample.append({
                "документ": fn,
                "документ_id": did,
                "фрагмент_id": chunk_id,
                "текст": text[:3000],
                "сущности": [{"имя": e.get("name"), "тип": e.get("type"),
                              "уверенность": e.get("confidence")} for e in (res.get("entities") or [])],
                "связи": [{"от": r.get("source"), "тип": r.get("type"), "к": r.get("target")}
                          for r in (res.get("relations") or [])],
                "секунд": round(time.monotonic() - t0, 1),
            })
            print(f"  {fn[:40]:42s} фрагмент {str(chunk_id)[-8:]} "
                  f"сущностей {len(sample[-1]['сущности']):3d} связей {len(sample[-1]['связи']):3d} "
                  f"за {sample[-1]['секунд']} с")

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"снято": time.strftime("%Y-%m-%d %H:%M"),
                   "образцов": len(sample), "данные": sample},
                  fh, ensure_ascii=False, indent=1)
    print(f"\nобразцов: {len(sample)}; файл: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
