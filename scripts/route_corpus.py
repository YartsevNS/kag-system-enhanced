"""Прогон паспорта страниц по существующему корпусу: сколько страниц реально потребуют модели.

Модель НЕ вызывается — считаем только признаки. Нужно, чтобы до внедрения понять масштаб: сколько
фрагментов уйдёт в VLM и сколько это времени (по замеру: ~4 минуты на компактный фрагмент на CPU).

Запуск внутри api-контейнера на проде:
    docker exec kag-api python /app/data/route_corpus.py
"""
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/data")          # модуль может быть скопирован рядом со скриптом

try:
    from src.indexing.page_router import (  # noqa: E402
        ROUTE_OCR, ROUTE_PARSER, ROUTE_SKIP, ROUTE_VLM,
        PageSignals, decide_route, garbage_ratio, table_like_ratio,
    )
except ModuleNotFoundError:                 # замер на образе, собранном до появления модуля
    from page_router import (               # type: ignore  # noqa: E402
        ROUTE_OCR, ROUTE_PARSER, ROUTE_SKIP, ROUTE_VLM,
        PageSignals, decide_route, garbage_ratio, table_like_ratio,
    )

COLLECTION = os.environ.get("QDRANT_COLLECTION", "kag_documents")
VLM_SECONDS_PER_FRAGMENT = 240          # замер 26.09.2026: компактный фрагмент на CPU


def table_quality_by_document() -> dict:
    """Худшее качество разметки таблиц по документу (из табличного слоя).

    Это ключевой сигнал: если таблицы документа уже разобраны с приемлемым качеством, модель ему
    не нужна. Без него первый прогон отправлял в модель 22% фрагментов корпуса.
    """
    out = {}
    try:
        from sqlalchemy import text
        from src.database.session import get_session_local
        session_factory = get_session_local()
        if session_factory is None:
            raise RuntimeError("сессия БД не инициализирована")
        with session_factory() as s:
            rows = s.execute(text(
                "select document_id, min(quality) as worst, count(*) as n, max(page_num) as pages "
                "from document_tables group by document_id")).fetchall()
        for doc_id, worst, n, pages in rows:
            out[str(doc_id)] = {"worst_quality": float(worst) if worst is not None else None,
                                "tables": int(n), "pages": int(pages or 0)}
        print(f"  табличный слой прочитан: документов с таблицами {len(out)}", flush=True)
    except Exception as e:
        print(f"  табличный слой недоступен: {type(e).__name__}: {str(e)[:100]}", flush=True)
    return out


def scroll_chunks():
    """Все точки коллекции: по ним считаем признаки (страница или фрагмент)."""
    from qdrant_client import QdrantClient
    c = QdrantClient(url=os.environ.get("QDRANT_URL", "http://kag-qdrant:6333"),
                     api_key=os.environ.get("QDRANT_API_KEY") or None)
    offset = None
    while True:
        pts, offset = c.scroll(collection_name=COLLECTION, limit=512, offset=offset,
                               with_payload=True, with_vectors=False)
        for p in pts:
            yield p.payload or {}
        if offset is None:
            break


def main() -> int:
    quality = table_quality_by_document()
    print(f"  документов с таблицами: {len(quality)}", flush=True)

    by_doc = defaultdict(list)
    for pl in scroll_chunks():
        by_doc[str(pl.get("document_id") or "?")].append(pl)

    routes = Counter()
    vlm_per_doc = Counter()
    chars_per_doc = Counter()
    for doc_id, chunks in by_doc.items():
        q = quality.get(doc_id, {})
        for i, pl in enumerate(chunks):
            text = pl.get("content") or ""
            sig = PageSignals.from_text(
                page=i + 1,
                text=text,
                tables_found=int((pl.get("metadata") or {}).get("tables_count") or 0),
                worst_quality=q.get("worst_quality"),
                doc_tables_count=q.get("tables", 0),
                doc_tables_quality=q.get("worst_quality"),
                doc_is_scan=str(pl.get("file_type") or "").startswith("image/"),
            )
            d = decide_route(sig)
            routes[d["route"]] += 1
            chars_per_doc[doc_id] += len(text)
            if d["route"] == ROUTE_VLM:
                vlm_per_doc[doc_id] += 1

    total = sum(routes.values())
    print(f"\n=== ПАСПОРТ КОРПУСА (фрагментов: {total}, документов: {len(by_doc)}) ===")
    for route, n in routes.most_common():
        print(f"  {route:<12} {n:>5}  ({100 * n / max(total, 1):.1f}%)")
    print(f"\n  в модель уйдёт фрагментов: {routes.get(ROUTE_VLM, 0)} "
          f"→ по {VLM_SECONDS_PER_FRAGMENT} с ≈ {routes.get(ROUTE_VLM, 0) * VLM_SECONDS_PER_FRAGMENT / 3600:.1f} ч на CPU")
    print("\n=== ТОП документов по числу фрагментов для модели ===")
    for doc_id, n in vlm_per_doc.most_common(8):
        print(f"  {doc_id[:12]}  фрагментов: {n:>3}  символов всего: {chars_per_doc[doc_id]:>7}")
    print("\n  (модель не вызывалась: это только оценка маршрутов)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
