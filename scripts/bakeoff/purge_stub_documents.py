"""Отбор и удаление документов-заглушек: заголовок и ссылка на источник, без содержания.

Повод: в пилоте графа десять выбранных «нормативных» документов оказались заглушками —
одна строка вида «Указание Банка России от 15.06.2026 № 7369-У» и ссылка на cbr.ru. Такие
документы завышают счёт корпуса, попадают в разметку и в граф, но не несут ни одного факта.

Признак (детерминированный, без модели):
  * суммарная длина текста всех фрагментов меньше порога, ИЛИ
  * весь текст сводится к заголовку и адресу источника (после выкидывания ссылок остаётся мало).

Что делает: считает (по умолчанию только показывает), а при --apply удаляет ШТАТНЫМ путём
(document_service.delete_document — запись, векторы, узлы графа, файл) и сохраняет список
удалённых в JSON, чтобы решение было проверяемым и обратимым по источнику.

Запуск на стенде:
    docker exec kag-api python /app/data/purge_stub_documents.py
    docker exec kag-api python /app/data/purge_stub_documents.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time

sys.path.insert(0, "/app")

MIN_TEXT = 400          # меньше этого суммарного текста документ смысла не несёт
URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
SOURCE_LINE = re.compile(r"^\s*(источник|source|ссылка|url)\s*[:\-]", re.IGNORECASE)


async def service():
    from src.indexing.embeddings_service import embeddings_service

    svc = embeddings_service
    if callable(svc) and not hasattr(svc, "initialize"):
        svc = svc()
    if not svc.is_initialized():
        await svc.initialize()
    return svc


def meaningful(text: str) -> str:
    """Текст без ссылок и служебных строк «Источник: …»."""
    out = []
    for line in (text or "").splitlines():
        if SOURCE_LINE.match(line):
            continue
        out.append(URL_RE.sub("", line))
    cleaned = re.sub(r"\s+", " ", " ".join(out)).strip()
    return cleaned


async def collect(svc, limit_points: int = 40) -> list[dict]:
    """Пройтись по документам и оценить, сколько в каждом осмысленного текста.

    ВАЖНО: у части документов векторы лежат в ДРУГОЙ коллекции (новости монитора — kag_news,
    признак documents.collection = 'news'). Если смотреть только основную коллекцию, такие
    документы выглядят пустыми — и их легко удалить по ошибке. Проверено на живом прогоне:
    50 «пустых» документов оказались именно новостями в своей коллекции.
    """
    from qdrant_client.http import models as qm

    from src.api.services.document_repository import get_doc_repo
    from src.config import get_settings

    cfg = get_settings()
    news_collection = getattr(cfg, "QDRANT_NEWS_COLLECTION", "kag_news")

    docs = get_doc_repo().get_all() or {}
    rows: list[dict] = []
    for did, d in docs.items():
        if not isinstance(d, dict):
            continue
        fn = d.get("filename") or ""
        collection = news_collection if (d.get("collection") or "") == "news" else svc.collection_name
        points, _ = svc._qdrant_client.scroll(
            collection_name=collection, limit=limit_points,
            scroll_filter=qm.Filter(must=[qm.FieldCondition(
                key="document_id", match=qm.MatchValue(value=did))]),
            with_payload=True, with_vectors=False)
        raw = " ".join((p.payload or {}).get("content") or (p.payload or {}).get("text") or ""
                       for p in points)
        clean = meaningful(raw)
        rows.append({
            "id": did,
            "filename": fn,
            "collection": collection,
            "chunks": int(d.get("chunks_count") or 0),
            "points": len(points),
            "chars_all": len(raw),
            "chars_clean": len(clean),
            "пример": clean[:110],
        })
    return rows


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="удалить найденные заглушки")
    ap.add_argument("--min-text", type=int, default=MIN_TEXT)
    ap.add_argument("--report", default="/app/data/removed_stubs.json")
    args = ap.parse_args()

    svc = await service()
    rows = await collect(svc)
    stubs = [r for r in rows if r["chars_clean"] < args.min_text]

    print("=" * 92)
    print(f"ЗАГЛУШКИ В КОРПУСЕ — {'УДАЛЕНИЕ' if args.apply else 'ОТБОР'}")
    print("=" * 92)
    print(f"документов просмотрено: {len(rows)}; без содержания (осмысленного текста < "
          f"{args.min_text} знаков): {len(stubs)}")
    empty_chunks = [r for r in rows if r["points"] == 0]
    print(f"из них вообще без фрагментов в векторах: {len(empty_chunks)}")
    for r in stubs[:25]:
        print(f"  {r['filename'][:46]:48s} фрагментов {r['chunks']:4d} "
              f"осмысленного текста {r['chars_clean']:6d} | {r['пример'][:60]!r}")

    if not args.apply:
        print("\nэто отбор — ничего не удалено. Для удаления добавить --apply")
        return 0

    from src.api.services.document_service import document_service

    removed, failed = [], []
    t0 = time.monotonic()
    for r in stubs:
        try:
            ok = await document_service.delete_document(r["id"])
            (removed if ok else failed).append(r)
        except Exception as exc:  # noqa: BLE001
            r["ошибка"] = f"{type(exc).__name__}: {exc}"
            failed.append(r)

    with open(args.report, "w", encoding="utf-8") as fh:
        json.dump({"снято": time.strftime("%Y-%m-%d %H:%M"),
                   "порог": args.min_text, "удалено": removed, "не удалось": failed},
                  fh, ensure_ascii=False, indent=1)
    print(f"\nудалено: {len(removed)}, не удалось: {len(failed)}, время {round(time.monotonic() - t0, 1)} с")
    print(f"список удалённых: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
