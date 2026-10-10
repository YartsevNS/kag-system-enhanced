"""Перезаливка документов с испорченным текстом: снести мусор и залить заново из исходника.

Повод: 29 документов корпуса были залиты с текстом, прочитанным как Latin-1 вместо UTF-8 — мусорные
векторы, нечитаемый текст в графе, пустая разметка. Исходные файлы целые (лежат в data/inbox),
восстановить текст из базы нельзя (потери навсегда) — значит только перезаливка.

Что делает по каждому документу:
  1) находит ИСХОДНЫЙ файл (перебором по data/inbox) и проверяет, что он читается как внятный русский;
  2) сносит мусор штатным путём (`document_service.delete_document` — запись + векторы + узлы графа + файл);
  3) заливает заново из исходника (штатный разбор: наш конвейер уже читает файл правильно);
  4) ставит в очередь обработки, чтобы пересчитались векторы и граф.

По умолчанию — примерка (ничего не меняет). Для работы добавить --apply.
Проверить на одном документе: --only 732.txt --apply

Запуск на стенде:
    docker cp scripts/bakeoff/reindex_mangled_docs.py kag-api:/app/data/
    docker exec kag-api python /app/data/reindex_mangled_docs.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import sys

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/data")

from text_encoding import cyrillic_share, decode_file, looks_like_mojibake  # noqa: E402

MOJI = ("Ð°", "Ð¾", "Ñ€", "Ñ‚")


def find_source(filename: str, roots: list[str]) -> pathlib.Path | None:
    """Исходник ищем перебором: файлы лежат и в корне папки импорта, и в подпапках batch_*."""
    for root in roots:
        base = pathlib.Path(root)
        if not base.exists():
            continue
        direct = base / filename
        if direct.exists():
            return direct
        for candidate in base.rglob(filename):
            return candidate
    return None


def collect_affected() -> list[dict]:
    """Документы, у которых в графе лежит испорченный текст."""
    from neo4j import GraphDatabase

    drv = GraphDatabase.driver(
        os.environ.get("NEO4J_URI", "bolt://neo4j:7687"),
        auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "")),
    )
    rows = []
    with drv.session() as s:
        for rec in s.run(
            """
            MATCH (d:Document)-[:HAS_CHUNK]->(c:Chunk)
            WITH d, count(c) AS total,
                 count(CASE WHEN c.text CONTAINS $m1 OR c.text CONTAINS $m2
                            OR c.text CONTAINS $m3 OR c.text CONTAINS $m4 THEN 1 END) AS bad
            WHERE bad > 0
            RETURN d.id AS id, d.filename AS filename, bad, total
            ORDER BY bad DESC
            """,
            m1=MOJI[0], m2=MOJI[1], m3=MOJI[2], m4=MOJI[3],
        ):
            rows.append({"id": rec["id"], "filename": rec["filename"] or "",
                         "bad": rec["bad"], "total": rec["total"]})
    drv.close()
    return rows


def clear_graph_nodes(document_id: str) -> int:
    """Снести узлы документа и его фрагментов из графа — своим соединением, без опоры на сервис.

    Почему отдельно: `kg_service.clear_document` при неинициализированном соединении просто ничего
    не делает (теперь хотя бы пишет предупреждение), а в служебном приборе прогрев не делается.
    """
    from neo4j import GraphDatabase

    drv = GraphDatabase.driver(
        os.environ.get("NEO4J_URI", "bolt://neo4j:7687"),
        auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "")),
    )
    removed = 0
    with drv.session() as s:
        rec = s.run(
            """
            MATCH (d:Document {id: $id})
            OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c:Chunk)
            WITH d, collect(c.id) AS chunk_ids
            DETACH DELETE d
            RETURN size(chunk_ids) AS n, chunk_ids
            """,
            id=document_id,
        ).single()
        if rec:
            removed = int(rec["n"] or 0)
            chunk_ids = [i for i in (rec["chunk_ids"] or []) if i]
            if chunk_ids:
                # Фрагменты удаляем отдельно: DETACH DELETE документа снимает только связи,
                # сами узлы фрагментов остались бы висеть в графе.
                s.run("MATCH (c:Chunk) WHERE c.id IN $ids DETACH DELETE c", ids=chunk_ids)
        # Убираем документ из перечня источников у сущностей и подчищаем осиротевшие.
        s.run("""
            MATCH (e:Entity)
            WHERE $id IN coalesce(e.source_docs, [])
            SET e.source_docs = [x IN e.source_docs WHERE x <> $id]
        """, id=document_id)
        s.run("MATCH (e:Entity) WHERE NOT (()-[:MENTIONS]->(e)) DETACH DELETE e")
    drv.close()
    return removed


async def reindex_one(doc: dict, roots: list[str], apply: bool) -> dict:
    from src.api.services.document_service import document_service

    filename = doc["filename"]
    source = find_source(filename, roots)
    result = {"filename": filename, "old_id": doc["id"], "bad": doc["bad"], "status": ""}

    if source is None:
        result["status"] = "пропуск: исходник не найден"
        return result

    decoded = decode_file(source)
    result["source"] = str(source)
    result["source_encoding"] = decoded.encoding
    result["source_cyr"] = round(cyrillic_share(decoded.text[:5000]), 3)
    if decoded.suspicious or looks_like_mojibake(decoded.text) or result["source_cyr"] < 0.3:
        result["status"] = "пропуск: исходник сам не читается (не перезаливаем, чтобы не тиражировать порчу)"
        return result

    if not apply:
        result["status"] = "к перезаливке (примерка)"
        return result

    # Если записи в базе уже нет (документ перезалит ранее) — чистим только остатки в графе.
    from src.api.services.document_repository import get_doc_repo

    if doc["id"] not in (get_doc_repo().get_all() or {}):
        removed = clear_graph_nodes(doc["id"])
        result["status"] = f"записи в базе нет: убраны остатки графа (узлов документа {removed})"
        return result

    # 1. Сносим мусор (запись + векторы + граф + файл)
    try:
        await document_service.delete_document(doc["id"])
        # Граф чистим ещё раз своим соединением: штатный путь молча пропускает очистку, если
        # служебное соединение не поднято (наш случай — приборы без прогрева).
        removed = clear_graph_nodes(doc["id"])
        result["graph_cleared"] = removed
    except Exception as exc:  # noqa: BLE001
        result["status"] = f"ошибка удаления: {exc}"
        return result

    # 2. Заливаем заново из исходника
    try:
        import uuid

        content = source.read_bytes()
        record = await document_service.upload_document(
            filename=filename, file_content=content,
            file_type=source.suffix.lower(), uploaded_by=None,
            upload_id=str(uuid.uuid4()),
            source_metadata={"reindexed_from": str(source), "reason": "испорченная кодировка"},
        )
        result["new_id"] = record.document_id
    except Exception as exc:  # noqa: BLE001
        result["status"] = f"ошибка заливки: {exc}"
        return result

    # 3. В очередь обработки — иначе не будет ни векторов, ни графа
    try:
        from src.indexing.queue_guard import enqueue_document

        queued = enqueue_document(record.document_id, force=True)
        result["queued"] = bool(queued)
    except Exception as exc:  # noqa: BLE001
        result["queued"] = False
        result["status"] = f"залит, но в очередь не поставлен: {exc}"
        return result

    result["status"] = "перезалит и поставлен в обработку"
    return result


async def cleanup_orphan_vectors(apply: bool, batch: int = 1000) -> dict:
    """Убрать из Qdrant векторы, у которых нет документа в базе.

    Зачем: удаление документа может провалиться (например, если служба векторов не прогрета),
    и тогда векторы остаются висеть мусором. Такие точки нельзя найти поиском по документам —
    они невидимы, но занимают место и могут попадать в выдачу.
    """
    from qdrant_client import QdrantClient

    from src.api.services.document_repository import get_doc_repo
    from src.config import get_settings

    cfg = get_settings()
    client = QdrantClient(url=os.environ.get("QDRANT_URL") or getattr(cfg, "QDRANT_URL", "http://qdrant:6333"),
                          api_key=os.environ.get("QDRANT_API_KEY") or None)
    known = set((get_doc_repo().get_all() or {}).keys())
    report = {}
    for collection in (getattr(cfg, "QDRANT_COLLECTION", "kag_documents"),
                       getattr(cfg, "QDRANT_NEWS_COLLECTION", "kag_news")):
        seen: dict[str, int] = {}
        offset = None
        while True:
            points, offset = client.scroll(collection_name=collection, limit=batch, offset=offset,
                                           with_payload=["document_id"], with_vectors=False)
            for p in points:
                did = (p.payload or {}).get("document_id")
                if did:
                    seen[did] = seen.get(did, 0) + 1
            if offset is None:
                break
        orphans = {d: n for d, n in seen.items() if d not in known}
        report[collection] = {"точек_в_коллекции": sum(seen.values()), "документов": len(seen),
                              "осиротевших_документов": len(orphans), "осиротевших_точек": sum(orphans.values())}
        if apply and orphans:
            from qdrant_client.http import models as qm

            for did in orphans:
                client.delete(collection_name=collection, points_selector=qm.Filter(
                    must=[qm.FieldCondition(key="document_id", match=qm.MatchValue(value=did))]))
            report[collection]["удалено"] = True
    client.close()
    return report


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="выполнить (по умолчанию только примерка)")
    ap.add_argument("--dry-run", action="store_true", help="то же, что без --apply (для явности)")
    ap.add_argument("--only", default="", help="работать с одним файлом по имени")
    ap.add_argument("--limit", type=int, default=0, help="ограничить число документов")
    ap.add_argument("--roots", default="/app/data/inbox", help="где искать исходники (через запятую)")
    ap.add_argument("--names-file", default="",
                    help="файл со списком имён документов (когда в графе записей уже нет)")
    ap.add_argument("--cleanup-orphans", action="store_true",
                    help="только убрать векторы без документа в базе (мусор)")
    args = ap.parse_args()
    apply = args.apply and not args.dry_run

    if args.cleanup_orphans:
        print("=" * 96)
        print(f"УБОРКА ОСИРОТЕВШИХ ВЕКТОРОВ — {'РАБОТА' if apply else 'ПРИМЕРКА'}")
        print("=" * 96)
        report = await cleanup_orphan_vectors(apply)
        for collection, stat in report.items():
            print(f"  {collection}: точек {stat['точек_в_коллекции']}, документов {stat['документов']}, "
                  f"осиротевших документов {stat['осиротевших_документов']} "
                  f"({stat['осиротевших_точек']} точек)" + ("  → удалены" if stat.get("удалено") else ""))
        if not apply:
            print("\nэто примерка — ничего не изменено. Для работы добавить --apply")
        return 0

    roots = [r.strip() for r in args.roots.split(",") if r.strip()]
    docs = collect_affected()
    if args.names_file:
        # Список имён: нужен, когда узлы графа уже снесены и по графу документы не найти.
        names = {n.strip() for n in pathlib.Path(args.names_file).read_text(encoding="utf-8").split() if n.strip()}
        from src.api.services.document_repository import get_doc_repo

        all_docs = get_doc_repo().get_all() or {}
        docs = [{"id": did, "filename": (d.get("filename") or ""), "bad": 0, "total": 0}
                for did, d in all_docs.items() if (d.get("filename") or "") in names]
        print(f"по списку имён найдено записей в базе: {len(docs)} из {len(names)}")
    if args.only:
        docs = [d for d in docs if d["filename"] == args.only]
    if args.limit:
        docs = docs[: args.limit]

    print("=" * 96)
    print(f"ПЕРЕЗАЛИВКА ДОКУМЕНТОВ С ИСПОРЧЕННЫМ ТЕКСТОМ — {'РАБОТА' if apply else 'ПРИМЕРКА'}")
    print("=" * 96)
    print(f"документов к разбору: {len(docs)}; исходники ищем в: {roots}")
    print()

    total_bad = 0
    results = []
    for doc in docs:
        res = await reindex_one(doc, roots, apply)
        results.append(res)
        total_bad += doc["bad"]
        line = (f"  {res['filename'][:22]:22s} битых {res['bad']:4d}/{doc['total']:<4d} "
                f"{res['status']}")
        if res.get("source"):
            line += f" | исходник {res['source_encoding']}, кириллицы {res['source_cyr']}"
        if res.get("new_id"):
            line += f" | новый {res['new_id'][:8]}"
        print(line)

    done = [r for r in results if r.get("new_id")]
    print()
    print(f"испорченных фрагментов затронуто: {total_bad}")
    print(f"перезалито документов: {len(done)} из {len(results)}")
    if not apply:
        print("\nэто примерка — ничего не изменено. Для работы добавить --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
