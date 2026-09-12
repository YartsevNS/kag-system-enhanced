#!/usr/bin/env python3
"""Пересчёт sparse-векторов (BM25) без переиндексации: локально, без эмбеддингов и LLM.

Зачем: sparse-векторы строятся тем же токенизатором, что и запрос. После правки
токенизатора (стемминг, стоп-слова, ё→е — см. src/indexing/lexical.py) старые
векторы несовместимы с новыми запросами, но переиндексировать документы целиком
не нужно: sparse считается на хосте из payload.content, а dense-вектор остаётся
нетронутым. Запись — через Qdrant update_vectors по id точки.

Запуск (внутри контейнера api/worker — там есть зависимости пакета; код src в образе):
    docker cp scripts/reindex_sparse.py kag-api:/app/reindex_sparse.py
    docker exec -e QDRANT_URL=http://qdrant:6333 kag-api python /app/reindex_sparse.py           # dry-run
    docker exec -e QDRANT_URL=http://qdrant:6333 kag-api python /app/reindex_sparse.py --apply

Только stdlib. Секреты — из env (QDRANT_API_KEY), как в других скриптах.
Почему не на хосте: импорт src.indexing.lexical поднимает src/indexing/__init__.py,
а тот тянет parsers/chunking с зависимостями (loguru и т.д.), которых на хосте нет.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List

# Чтобы импортировать src.* при запуске из каталога scripts/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.indexing.lexical import sparse_vector  # noqa: E402


def _q(url: str, api_key: str, path: str, payload: dict = None,
       method: str = None, timeout: float = 180.0) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url.rstrip("/") + path, data=data,
                                 method=method or ("POST" if data else "GET"))
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("api-key", api_key)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode()
    return json.loads(body) if body else {}


def scroll_chunks(url: str, api_key: str, collection: str, document_id: str = None,
                  batch: int = 500) -> List[dict]:
    """Все точки: id + content (+ chunk_id для отчёта)."""
    out: List[dict] = []
    offset = None
    flt = None
    if document_id:
        flt = {"must": [{"key": "document_id", "match": {"value": document_id}}]}
    while True:
        payload = {"limit": batch, "with_payload": ["content", "chunk_id", "document_id"],
                   "with_vector": False}
        if offset is not None:
            payload["offset"] = offset
        if flt:
            payload["filter"] = flt
        res = _q(url, api_key, f"/collections/{collection}/points/scroll", payload)["result"]
        out.extend(res.get("points") or [])
        offset = res.get("next_page_offset")
        if offset is None:
            return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Пересчёт sparse-векторов в Qdrant")
    ap.add_argument("--qdrant-url", default=None,
                    help="по умолчанию http://localhost:6333 (или QDRANT_URL)")
    ap.add_argument("--qdrant-key", default=os.environ.get("QDRANT_API_KEY", ""))
    ap.add_argument("--collection", default=os.environ.get("QDRANT_COLLECTION", "kag_documents"))
    ap.add_argument("--document-id", default=None, help="только один документ")
    ap.add_argument("--batch", type=int, default=200)
    ap.add_argument("--apply", action="store_true", help="записать (иначе dry-run)")
    ap.add_argument("--report", default="/tmp/reindex_sparse.json")
    args = ap.parse_args()

    url = (args.qdrant_url or os.environ.get("QDRANT_URL")
           or f"http://{os.environ.get('QDRANT_HOST', 'localhost')}:"
              f"{os.environ.get('QDRANT_PORT', '6333')}")
    print(f"Qdrant: {url} | коллекция: {args.collection} | режим: "
          f"{'ЗАПИСЬ' if args.apply else 'dry-run'}")

    points = scroll_chunks(url, args.qdrant_key, args.collection, args.document_id)
    print(f"точек к пересчёту: {len(points)}")

    batch: List[dict] = []
    updated = skipped = 0
    terms_before = terms_after = 0
    samples = []
    started = time.time()

    for p in points:
        content = (p.get("payload") or {}).get("content") or ""
        if not content.strip():
            skipped += 1
            continue
        vec = sparse_vector(content)
        if not vec["indices"]:
            skipped += 1
            continue
        terms_after += len(vec["indices"])
        terms_before += len(set((content or "").lower().split()))
        if len(samples) < 5:
            samples.append((str((p.get("payload") or {}).get("chunk_id"))[-18:],
                            len(vec["indices"])))
        if args.apply:
            batch.append({"id": p["id"], "vector": {"sparse": vec}})
        updated += 1
        if args.apply and len(batch) >= args.batch:
            _q(url, args.qdrant_key, f"/collections/{args.collection}/points/vectors",
               {"points": batch}, method="PUT")
            batch = []

    if args.apply and batch:
        _q(url, args.qdrant_key, f"/collections/{args.collection}/points/vectors",
           {"points": batch}, method="PUT")

    print(f"  пересчитано: {updated}, пропущено (нет текста): {skipped}")
    print(f"  токенов: было (по пробелам) ~{terms_before}, стало (стемы без стоп-слов) {terms_after}")
    if samples:
        print("  примеры (chunk_id, число термов):")
        for cid, n in samples:
            print(f"    {cid}: {n}")

    if args.apply:
        col = _q(url, args.qdrant_key, f"/collections/{args.collection}")["result"]
        print(f"\n  в коллекции точек: {col.get('points_count')} "
              f"(sparse-вектор объявлен: "
              f"{'да' if col['config']['params'].get('sparse_vectors') else 'нет'}, "
              f"modifier: {((col['config']['params'].get('sparse_vectors') or {}).get('sparse') or {}).get('modifier')})")

    Path(args.report).write_text(json.dumps(
        {"url": url, "collection": args.collection, "document_id": args.document_id,
         "points": len(points), "recomputed": updated, "skipped": skipped,
         "applied": bool(args.apply), "terms_before": terms_before, "terms_after": terms_after},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nОтчёт: {args.report}; время: {time.time() - started:.1f} с")
    if not args.apply:
        print("Это dry-run. Для записи повторите с --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
