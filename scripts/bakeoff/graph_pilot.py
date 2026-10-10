"""Пилот правильного графа: сборка с онтологией связей на небольшой выборке документов.

Задача (решение владельца 10.10.2026): старый граф оказался больным (27% связей проходили схему,
56% свалены в одну мусорку), поэтому «вытягивать его» не нужно. Граф очищен, слепок сохранён.
Теперь строим заново по ПРАВИЛЬНОЙ методике на 10 документах и смотрим, что получится:
типы связей осмысленные, пары проверены, мусорки нет.

Что делает прибор:
  1. выбирает документы (по видам: право, стандарты, акты; ограничение по числу фрагментов —
     чтобы пилот был дешёвым);
  2. достаёт их фрагменты из Qdrant (текст + идентификаторы);
  3. запускает ШТАТНУЮ сборку графа (`document_service._build_knowledge_graph_async`) —
     ту же, что в конвейере, но управляемо и только для выбранных документов;
  4. печатает итог по каждому документу.

Проверка типов связей идёт внутри извлечения (онтология в задании + проверка троек перед записью),
поэтому отдельно её включать не нужно.

Запуск на стенде:
    docker exec kag-api python /app/data/graph_pilot.py --dry-run
    docker exec kag-api python /app/data/graph_pilot.py --apply --limit 10
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, "/app")

DEFAULT_TYPES = ("law", "national_standard", "regulation", "subordinate_act",
                 "standardization_recommendation", "official_letter")


async def service():
    from src.indexing.embeddings_service import embeddings_service

    svc = embeddings_service
    if callable(svc) and not hasattr(svc, "initialize"):
        svc = svc()
    if not svc.is_initialized():
        await svc.initialize()
    return svc


async def chunks_from_qdrant(svc, document_id: str, limit: int = 600) -> list[dict]:
    from qdrant_client.http import models as qm

    points, _ = svc._qdrant_client.scroll(
        collection_name=svc.collection_name, limit=limit,
        scroll_filter=qm.Filter(must=[qm.FieldCondition(
            key="document_id", match=qm.MatchValue(value=document_id))]),
        with_payload=True, with_vectors=False)
    chunks = []
    for i, p in enumerate(points):
        payload = p.payload or {}
        text = payload.get("content") or payload.get("text") or ""
        if not text.strip():
            continue
        chunks.append({
            "content": text, "text": text,
            "chunk_id": payload.get("chunk_id") or str(p.id),
            "chunk_index": payload.get("chunk_index", i),
            "document_id": document_id,
        })
    return chunks


def pick_documents(limit: int, types: tuple[str, ...], max_chunks: int, names: list[str],
                   min_chunks: int = 0, domains: tuple[str, ...] = ()) -> list[dict]:
    from src.api.services.document_repository import get_doc_repo

    out = []
    for did, d in (get_doc_repo().get_all() or {}).items():
        if not isinstance(d, dict):
            continue
        fn = d.get("filename") or ""
        n = int(d.get("chunks_count") or 0)
        dt = (d.get("document_type") or d.get("type") or "").lower()
        rubs = {str(x).lower() for x in (d.get("rubrics") or [])}
        if names:
            if fn not in names:
                continue
        elif domains:
            # Отбор по темам: так берём нормативное подмножество (кибербез, юристы), где связи
            # вообще имеют проверяемый смысл, и не тратим модель на новости и аналитику.
            if not (rubs & set(domains)):
                continue
            if n < min_chunks or n > max_chunks:
                continue
        else:
            if dt not in types:
                continue
            if n < min_chunks or n > max_chunks:
                continue
        out.append({"id": did, "filename": fn, "type": dt, "chunks": n, "rubrics": sorted(rubs)})
    out.sort(key=lambda x: x["chunks"])
    return out[:limit] if limit else out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--max-chunks", type=int, default=150)
    ap.add_argument("--min-chunks", type=int, default=5,
                    help="нижняя граница: отсекает документы-заглушки (только заголовок и ссылка)")
    ap.add_argument("--types", default=",".join(DEFAULT_TYPES))
    ap.add_argument("--names", default="", help="явный список имён через запятую")
    ap.add_argument("--domain", default="", help="отбор по темам (например infosec,law)")
    args = ap.parse_args()
    apply = args.apply and not args.dry_run

    types = tuple(t.strip() for t in args.types.split(",") if t.strip())
    names = [n.strip() for n in args.names.split(",") if n.strip()]
    domains = tuple(d.strip().lower() for d in args.domain.split(",") if d.strip())
    docs = pick_documents(args.limit, types, args.max_chunks, names, args.min_chunks, domains)

    print("=" * 92)
    print(f"ПИЛОТ ГРАФА НА ОНТОЛОГИИ — {'СБОРКА' if apply else 'ПРИМЕРКА'}")
    print("=" * 92)
    print(f"виды: {', '.join(types) if not names else 'явный список'}; "
          f"фрагментов на документ не более {args.max_chunks}")
    total_chunks = sum(d["chunks"] for d in docs)
    print(f"документов выбрано: {len(docs)}, фрагментов всего: {total_chunks} "
          f"(это ~{total_chunks * 2} вызовов модели)")
    for d in docs:
        print(f"  {d['filename'][:58]:60s} {d['type']:28s} фрагментов {d['chunks']}")
    if not apply:
        print("\nэто примерка — граф не строится. Для сборки добавить --apply")
        return 0

    from src.api.services.document_service import document_service

    svc = await service()
    t_all = time.monotonic()
    for d in docs:
        chunks = await chunks_from_qdrant(svc, d["id"])
        if not chunks:
            print(f"  пропуск {d['filename']}: фрагментов с текстом нет")
            continue
        t0 = time.monotonic()
        try:
            await document_service._build_knowledge_graph_async(d["id"], d["filename"], chunks)
            print(f"  собрано {d['filename'][:50]:52s} фрагментов {len(chunks):4d} "
                  f"за {round(time.monotonic() - t0, 1)} с")
        except Exception as exc:  # noqa: BLE001 — показываем причину, не глотаем
            print(f"  ОШИБКА {d['filename'][:50]}: {type(exc).__name__}: {exc}")
    print(f"\nвсего: {round(time.monotonic() - t_all, 1)} с")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
