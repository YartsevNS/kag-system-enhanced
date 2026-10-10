"""Синхронизация payload `document_type`/`schema_version` с записью документа — в ТУ коллекцию.

Зачем прибор. При смене вида документа payload фрагментов обновляет админский эндпоинт правки
разметки, и коллекцию он выбирает через `service_for_document` по ТЕКУЩЕЙ записи. Если в том же
заходе меняется МАРШРУТ документа (например, новость перестаёт быть видом `news` и получает явный
признак `documents.collection='news'`), то в момент правки маршрут ещё старый, и `set_payload`
уходит в ДРУГУЮ коллекцию — там документ не найден, обновление молча не срабатывает (исключения
нет, точек 0). Так остались со старым видом 29 новостей ЦБ: их векторы в `kag_news`, а payload
писался в `kag_documents`.

Что делает: для каждого документа читает вид и версию словаря из ЗАПИСИ, определяет коллекцию по
маршруту (та же единственная точка решения — `service_for_document`) и записывает payload туда.
Идемпотентен: если значения совпадают, ничего не пишет.

Запуск в контейнере api:
  docker exec kag-api python /app/data/sync_document_type_payload.py            # сухой прогон
  docker exec kag-api python /app/data/sync_document_type_payload.py --apply
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter


async def main() -> int:
    from src.indexing.embeddings_service import service_for_document
    from src.api.services.document_repository import get_doc_repo
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="записать (по умолчанию сухой прогон)")
    args = ap.parse_args()

    docs = get_doc_repo().get_all()
    print(f"документов: {len(docs)}")

    mismatch, ok, no_points, fixed, failed = [], 0, 0, 0, 0
    for did, d in docs.items():
        want_type = str(d.get("document_type") or "")
        want_ver = str(d.get("schema_version") or "")
        svc = service_for_document(did)
        await svc.initialize()
        flt = Filter(must=[FieldCondition(key="document_id", match=MatchValue(value=did))])
        pts, _ = await asyncio.to_thread(
            svc._qdrant_client.scroll, collection_name=svc.collection_name,
            scroll_filter=flt, limit=1, with_payload=True)
        if not pts:
            no_points += 1
            continue
        payload = pts[0].payload or {}
        if str(payload.get("document_type") or "") == want_type:
            ok += 1
            continue
        mismatch.append((did, svc.collection_name, payload.get("document_type"), want_type, want_ver))

    print(f"payload совпадает с записью: {ok}")
    print(f"расхождений: {len(mismatch)}; документов без точек: {no_points}")
    for did, coll, old, new, ver in mismatch[:15]:
        print(f"   {did[:12]} [{coll}] payload={old!r} → запись={new!r}")
    if not args.apply:
        print("СУХОЙ ПРОГОН: ничего не записано (для применения — флаг --apply)")
        return 0

    for did, coll, old, new, ver in mismatch:
        try:
            svc = service_for_document(did)
            await svc.update_document_payload(did, {"document_type": new, "schema_version": ver})
            fixed += 1
        except Exception as e:  # noqa: BLE001 — один документ не должен ронять прогон
            failed += 1
            print(f"   ошибка {did[:12]}: {type(e).__name__}: {e}")

    print(f"обновлено: {fixed}, ошибок: {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
