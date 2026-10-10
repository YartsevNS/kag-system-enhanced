"""Тест фильтра темы на юридических документах: не теряет ли фильтр нужный документ.

Повод: в наборе вопросов с эталонами (29 штук) юридических вопросов нет вовсе — только кибербез и
экономика. А владелец просил проверить и юристов. Поэтому тест строится из самих документов:
для юридического документа берём характерную фразу из его текста как запрос — эталоном считается
сам документ, из которого фраза взята. Затем сравниваем, на каком месте документ оказывается
без фильтра и с фильтром по теме (мягкий режим: документы без темы не выбрасываются).

Метрика: доля случаев, где документ остался в top-10, и средний ранг до/после фильтра.

Запуск в контейнере api:
    docker exec kag-api python /app/data/legal_domain_test.py --domain law --docs 10
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys

sys.path.insert(0, "/app")


async def service():
    from src.indexing.embeddings_service import embeddings_service

    svc = embeddings_service
    if callable(svc) and not hasattr(svc, "initialize"):
        svc = svc()
    if not svc.is_initialized():
        await svc.initialize()
    return svc


def meta(chunk) -> dict:
    payload = chunk if isinstance(chunk, dict) else {}
    return {**(payload.get("metadata") or {}), **payload}


def rubrics_of(chunk) -> list[str]:
    raw = meta(chunk).get("rubrics")
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, str) and raw.strip():
        try:
            import json as _json

            val = _json.loads(raw)
            return [str(x) for x in val] if isinstance(val, list) else [raw]
        except Exception:
            return [raw]
    return []


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", default="law", help="какую тему проверяем")
    ap.add_argument("--docs", type=int, default=10, help="сколько документов взять")
    ap.add_argument("--min-chunks", type=int, default=4)
    ap.add_argument("--top", type=int, default=50, help="сколько фрагментов брать у поиска")
    args = ap.parse_args()

    from qdrant_client.http import models as qm

    from src.api.services.document_repository import get_doc_repo

    svc = await service()
    docs = [(did, d) for did, d in (get_doc_repo().get_all() or {}).items()
            if isinstance(d, dict) and args.domain in (d.get("rubrics") or [])
            and int(d.get("chunks_count") or 0) >= args.min_chunks]
    docs = docs[: args.docs]
    print(f"документов с темой «{args.domain}»: {len(docs)}")

    kept_plain = kept_filtered = 0
    ranks_plain: list[int] = []
    ranks_filtered: list[int] = []

    for did, d in docs:
        fn = d.get("filename") or ""
        points, _ = svc._qdrant_client.scroll(
            collection_name=svc.collection_name, limit=1,
            scroll_filter=qm.Filter(must=[qm.FieldCondition(
                key="document_id", match=qm.MatchValue(value=did))]),
            with_payload=True, with_vectors=False)
        if not points:
            continue
        text = ((points[0].payload or {}).get("content") or "")[:600]
        # Запрос — характерная фраза из документа (первое длинное предложение).
        sentences = [s.strip() for s in re.split(r"[.!?]\s+", text) if len(s.strip()) > 60]
        query = sentences[0][:200] if sentences else text[:200]
        if len(query) < 40:
            continue

        vector = await svc._embedding_client.generate(query)
        # Коллекция может быть с ИМЕНОВАННЫМИ векторами (dense/sparse) — тогда в запросе надо
        # указать, какой вектор использовать, иначе Qdrant отвечает «Vector params not specified».
        using = None
        try:
            info = svc._qdrant_client.get_collection(svc.collection_name)
            vectors = info.config.params.vectors
            if isinstance(vectors, dict) and vectors:
                using = next(iter(vectors.keys()))
        except Exception:
            using = None
        hits = svc._qdrant_client.query_points(
            collection_name=svc.collection_name, query=vector, using=using,
            limit=args.top, with_payload=True).points

        def rank_of(filter_by_topic: bool) -> int | None:
            seen = []
            for h in hits:
                payload = meta(h.payload or {})
                rid = str(payload.get("document_id") or "")
                rubs = [r.lower() for r in rubrics_of(payload)]
                if filter_by_topic and rubs and args.domain not in rubs:
                    continue          # мягкий режим: без темы допускаем, с чужой темой — нет
                if rid not in seen:
                    seen.append(rid)
            return seen.index(did) + 1 if did in seen else None

        r_plain = rank_of(False)
        r_filtered = rank_of(True)
        ranks_plain.append(r_plain or args.top + 1)
        ranks_filtered.append(r_filtered or args.top + 1)
        kept_plain += 1 if r_plain and r_plain <= 10 else 0
        kept_filtered += 1 if r_filtered and r_filtered <= 10 else 0
        print(f"  {fn[:52]:54s} без фильтра {r_plain}, с фильтром темы {r_filtered}")

    n = max(len(ranks_plain), 1)
    print()
    print(f"документов проверено: {len(ranks_plain)}")
    print(f"  остались в top-10 БЕЗ фильтра:      {kept_plain} ({100 * kept_plain / n:.0f}%)")
    print(f"  остались в top-10 С фильтром темы:   {kept_filtered} ({100 * kept_filtered / n:.0f}%)")
    print(f"  средний ранг без фильтра: {sum(ranks_plain) / n:.1f}; "
          f"с фильтром: {sum(ranks_filtered) / n:.1f}")
    verdict = ("фильтр темы документы НЕ теряет" if kept_filtered >= kept_plain
               else "ВНИМАНИЕ: фильтр темы выбрасывает нужные документы")
    print(f"вывод: {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
