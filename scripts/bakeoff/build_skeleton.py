"""Слой правил в графе: узлы стандартов и пунктов + ссылки, добытые БЕЗ модели.

Зачем: замеры показали, что связи, типизацию которых надо угадывать моделью, — слабое место (судьи
не согласны даже между собой, часть связей не подтверждается текстом). А связи, у которых истина
проверяема, — номера ГОСТ, ссылки на пункты, даты — берутся правилами точно и бесплатно. В текущем
графе этого класса связей нет вовсе, хотя спрашивают именно про них («какие документы ссылаются на
ГОСТ X», «что сказано в п. 5.2.1»).

Что создаёт прибор:
  * узлы Entity с типами `standard` (ГОСТ/СП/СНиП) и `clause` (пункт/раздел/таблица/приложение);
  * связи «документ ССЫЛАЕТСЯ НА стандарт» (REFERENCES) и «документ СОДЕРЖИТ ПУНКТ» (HAS_CLAUSE);
  * связи «фрагмент УПОМИНАЕТ стандарт/пункт» (MENTIONS) — чтобы от узла можно было дойти до текста.

Происхождение помечается прямо в данных: `layer = 'regex'`, `extractor_version`, `chunk_id`.
Без этого нельзя будет отличить правило от модели, а мы уже знаем, к чему приводит потеря происхождения.

Узлы и связи создаются через MERGE — повторный прогон ничего не дублирует.

Запуск на стенде:
    docker exec kag-api python /app/data/build_skeleton.py --dry-run --domain infosec --docs 5
    docker exec kag-api python /app/data/build_skeleton.py --apply --domain infosec,law
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from collections import Counter

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/data")

from deterministic_extractors import EXTRACTOR_VERSION, extract  # noqa: E402


async def service():
    from src.indexing.embeddings_service import embeddings_service

    svc = embeddings_service
    if callable(svc) and not hasattr(svc, "initialize"):
        svc = svc()
    if not svc.is_initialized():
        await svc.initialize()
    return svc


def pick_documents(domains: list[str], min_chunks: int, limit: int) -> list[dict]:
    from src.api.services.document_repository import get_doc_repo

    out = []
    for did, d in (get_doc_repo().get_all() or {}).items():
        if not isinstance(d, dict):
            continue
        rubs = [str(x).lower() for x in (d.get("rubrics") or [])]
        if domains and not (set(rubs) & set(domains)):
            continue
        n = int(d.get("chunks_count") or 0)
        if n < min_chunks:
            continue
        out.append({"id": did, "filename": d.get("filename") or "", "chunks": n, "rubrics": rubs})
    out.sort(key=lambda x: -x["chunks"])
    return out[:limit] if limit else out


async def chunks_of(svc, document_id: str, limit: int = 400) -> list[dict]:
    from qdrant_client.http import models as qm

    points, _ = svc._qdrant_client.scroll(
        collection_name=svc.collection_name, limit=limit,
        scroll_filter=qm.Filter(must=[qm.FieldCondition(
            key="document_id", match=qm.MatchValue(value=document_id))]),
        with_payload=True, with_vectors=False)
    out = []
    for p in points:
        payload = p.payload or {}
        text = payload.get("content") or payload.get("text") or ""
        if text.strip():
            out.append({"id": payload.get("chunk_id") or str(p.id), "text": text})
    return out


def driver():
    from neo4j import GraphDatabase

    return GraphDatabase.driver(
        os.environ.get("NEO4J_URI", "bolt://neo4j:7687"),
        auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "")))


def write_skeleton(session, document_id: str, filename: str, sets: list[dict]) -> Counter:
    """Записать найденное правилами. MERGE — повторный прогон безопасен."""
    stat = Counter()
    for item in sets:
        kind = item["kind"]          # standard | clause | date
        name = item["name"]
        # узел сущности: тип из онтологии (standard/clause/date)
        session.run(
            """
            MERGE (e:Entity {name: $name, type: $type})
            ON CREATE SET e.created_at = timestamp(), e.confidence = 1.0,
                          e.layer = 'regex', e.extractor_version = $ver
            SET e.source_docs = CASE WHEN $doc IN coalesce(e.source_docs, []) THEN e.source_docs
                                     ELSE coalesce(e.source_docs, []) + $doc END
            """, name=name, type=kind, ver=EXTRACTOR_VERSION, doc=document_id)
        stat[f"узлов_{kind}"] += 1

        # связь документа и фрагмента с узлом.
        # Документ создаём, если его нет: граф был очищен, и узлы есть только у пересобранных
        # документов — без этого связи молча не создавались (MATCH не находил якорь).
        rel = item["rel"]            # REFERENCES | HAS_CLAUSE | DATED
        session.run(
            f"""
            MERGE (d:Document {{id: $doc}})
            ON CREATE SET d.filename = $filename, d.created_at = timestamp(), d.layer = 'skeleton'
            WITH d
            MATCH (e:Entity {{name: $name, type: $type}})
            MERGE (d)-[r:`{rel}`]->(e)
            ON CREATE SET r.layer = 'regex', r.extractor_version = $ver,
                          r.chunk_id = $chunk, r.created_at = timestamp()
            """, doc=document_id, filename=filename, name=name, type=kind,
            ver=EXTRACTOR_VERSION, chunk=item["chunk"])
        stat[f"связей_{rel}"] += 1

        if item.get("chunk"):
            # Фрагмент тоже создаём, если его нет: иначе связи «фрагмент упоминает стандарт»
            # молча не появлялись (та же причина, что и с документом — граф был очищен).
            session.run(
                """
                MERGE (c:Chunk {id: $chunk})
                ON CREATE SET c.document_id = $doc, c.text = $preview, c.layer = 'skeleton'
                WITH c
                MERGE (d:Document {id: $doc})
                MERGE (d)-[:HAS_CHUNK]->(c)
                WITH c
                MATCH (e:Entity {name: $name, type: $type})
                MERGE (c)-[r:MENTIONS]->(e)
                ON CREATE SET r.layer = 'regex', r.extractor_version = $ver
                """, chunk=item["chunk"], doc=document_id, preview=(item.get("preview") or "")[:300],
                name=name, type=kind, ver=EXTRACTOR_VERSION)
    return stat


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--domain", default="infosec,law", help="какие темы брать (пусто — все)")
    ap.add_argument("--docs", type=int, default=0, help="сколько документов (0 — все подходящие)")
    ap.add_argument("--min-chunks", type=int, default=4)
    ap.add_argument("--with-dates", action="store_true", help="добавлять и даты (по умолчанию только стандарты и пункты)")
    args = ap.parse_args()
    apply = args.apply and not args.dry_run

    domains = [d.strip().lower() for d in args.domain.split(",") if d.strip()]
    docs = pick_documents(domains, args.min_chunks, args.docs)
    print("=" * 92)
    print(f"СЛОЙ ПРАВИЛ В ГРАФЕ — {'ЗАПИСЬ' if apply else 'ОТБОР'} (извлекатель {EXTRACTOR_VERSION})")
    print("=" * 92)
    print(f"темы: {', '.join(domains) or 'все'}; документов выбрано: {len(docs)}")

    svc = await service()
    drv = driver()
    total = Counter()
    per_doc = []
    t0 = time.monotonic()
    with drv.session() as session:
        for n, d in enumerate(docs, 1):
            chunks = await chunks_of(svc, d["id"])
            sets = []
            for ch in chunks:
                sk = extract(ch["text"])
                preview = ch["text"]
                for gost in sk.gost:
                    sets.append({"kind": "standard", "name": gost, "rel": "REFERENCES",
                                 "chunk": ch["id"], "preview": preview})
                for sp in sk.sp:
                    sets.append({"kind": "standard", "name": sp, "rel": "REFERENCES",
                                 "chunk": ch["id"], "preview": preview})
                for clause in sk.clauses:
                    sets.append({"kind": "clause", "name": clause, "rel": "HAS_CLAUSE",
                                 "chunk": ch["id"], "preview": preview})
                if args.with_dates:
                    for dt in sk.dates:
                        sets.append({"kind": "date", "name": dt, "rel": "DATED",
                                     "chunk": ch["id"], "preview": preview})
            if not sets:
                print(f"  {d['filename'][:46]:48s} фрагментов {len(chunks):4d} — правилами ничего не найдено")
                continue
            stat = write_skeleton(session, d["id"], d["filename"], sets) if apply else Counter(
                {f"найдено_{k}": sum(1 for s in sets if s['kind'] == k)
                 for k in {s['kind'] for s in sets}})
            total.update(stat)
            per_doc.append((d["filename"], len(chunks), len(sets)))
            print(f"  {d['filename'][:46]:48s} фрагментов {len(chunks):4d} находок {len(sets):4d} "
                  f"{dict(stat) if stat else ''}")

    drv.close()
    print()
    print(f"документов обработано: {len(per_doc)}; время {round(time.monotonic() - t0, 1)} с")
    print("итог:", dict(total))
    if not apply:
        print("\nэто отбор — в граф не записано. Для записи добавить --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
