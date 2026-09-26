"""Отчёт «что именно уйдёт в модель»: список фрагментов с причиной и ссылкой на страницу.

Зачем: перед вызовом модели человек должен увидеть своими глазами, что она получит. Для каждого
фрагмента показываем: документ, страницу, причину решения маршрутизатора, начало текста и ссылку
на страницу в просмотрщике (там видно исходную картинку — по ней и понятно, таблица это или нет).

Запуск внутри api-контейнера:
    docker exec kag-api python /app/data/vlm_candidates_report.py /app/data/vlm_candidates.html
Готовый файл копируется на ноутбук и открывается в браузере; ссылки ведут на прод-интерфейс.
"""
import html
import os
import sys
from collections import defaultdict

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/data")

try:
    from src.indexing.page_router import PageSignals, ROUTE_VLM, decide_route
except ModuleNotFoundError:
    from page_router import PageSignals, ROUTE_VLM, decide_route  # type: ignore

BASE_URL = os.environ.get("KAG_UI_URL", "http://192.168.50.18")
COLLECTION = os.environ.get("QDRANT_COLLECTION", "kag_documents")


def table_quality_by_document() -> dict:
    out = {}
    try:
        from sqlalchemy import text
        from src.database.session import get_session_local
        factory = get_session_local()
        if factory is None:
            return out
        with factory() as s:
            rows = s.execute(text("select document_id, min(quality), count(*) "
                                  "from document_tables group by document_id")).fetchall()
        for doc_id, worst, n in rows:
            out[str(doc_id)] = {"worst_quality": float(worst) if worst is not None else None,
                                "tables": int(n)}
    except Exception as e:
        print(f"  табличный слой недоступен: {type(e).__name__}: {str(e)[:80]}", flush=True)
    return out


def main() -> int:
    out_path = sys.argv[1] if len(sys.argv) > 1 else "/app/data/vlm_candidates.html"
    quality = table_quality_by_document()

    from qdrant_client import QdrantClient
    client = QdrantClient(url=os.environ.get("QDRANT_URL", "http://kag-qdrant:6333"),
                          api_key=os.environ.get("QDRANT_API_KEY") or None)
    all_lens = defaultdict(list)          # для медианы по документу
    # ПРЕДВАРИТЕЛЬНЫЙ проход: длины фрагментов по документам (медиана — признак документа)
    pre, off0 = None, None
    while True:
        pts0, off0 = client.scroll(collection_name=COLLECTION, limit=512, offset=off0,
                                   with_payload=True, with_vectors=False)
        for p0 in pts0:
            pl0 = p0.payload or {}
            all_lens[str(pl0.get("document_id") or "?")].append(len(pl0.get("content") or ""))
        if off0 is None:
            break
    low_text = {d: (sorted(v)[len(v) // 2] < 200) for d, v in all_lens.items() if v}

    offset, total, candidates = None, 0, []
    page_counter = defaultdict(int)
    while True:
        pts, offset = client.scroll(collection_name=COLLECTION, limit=512, offset=offset,
                                    with_payload=True, with_vectors=False)
        for p in pts:
            pl = p.payload or {}
            doc_id = str(pl.get("document_id") or "?")
            page_counter[doc_id] += 1
            total += 1
            text = pl.get("content") or ""
            q = quality.get(doc_id, {})
            sig = PageSignals.from_text(
                page=page_counter[doc_id], text=text,
                tables_found=int((pl.get("metadata") or {}).get("tables_count") or 0),
                worst_quality=q.get("worst_quality"),
                doc_tables_count=q.get("tables", 0), doc_tables_quality=q.get("worst_quality"),
                doc_is_scan=str(pl.get("file_type") or "").startswith("image/"),
                doc_low_text=low_text.get(doc_id),
            )
            d = decide_route(sig)
            if d["route"] == ROUTE_VLM:
                pages = pl.get("pages") or []
                page = pages[0] if pages else sig.page
                candidates.append({
                    "doc_id": doc_id,
                    "filename": pl.get("filename") or doc_id[:12],
                    "file_type": pl.get("file_type") or "",
                    "page": page,
                    "reason": d["reason"],
                    "chars": len(text),
                    "text": text[:700],
                })
        if offset is None:
            break

    print(f"  фрагментов всего: {total} | в модель: {len(candidates)}", flush=True)

    rows = []
    for c in sorted(candidates, key=lambda x: (-x["chars"], x["filename"])):
        link = f"{BASE_URL}/viewer?id={c['doc_id']}&page={c['page']}"
        rows.append(f"""
        <div class="card">
          <div class="head">
            <b>{html.escape(c['filename'])}</b>
            <span class="tag">id {c['doc_id'][:8]}</span>
            <span class="tag">{html.escape(c['file_type'])}</span>
            <span class="tag">страница {c['page']}</span>
            <span class="tag">{c['chars']} символов</span>
            <a class="btn" href="{link}" target="_blank">открыть страницу</a>
          </div>
          <div class="why">Причина: {html.escape(c['reason'])}</div>
          <pre>{html.escape(c['text'])}</pre>
        </div>""")

    doc = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><title>Кандидаты в модель: {len(candidates)}</title>
<style>
 body{{background:#171717;color:#e6e6e6;font:14px/1.5 -apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:0;padding:24px}}
 h1{{font-size:20px;margin:0 0 6px 0}} .sub{{color:#8a8a8a;font-size:13px;margin-bottom:18px}}
 .card{{background:#1f1f1f;border:1px solid #2c2c2c;border-radius:8px;padding:14px;margin-bottom:12px}}
 .head{{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:8px}}
 .tag{{background:#262626;border:1px solid #333;border-radius:999px;padding:2px 10px;font-size:12px;color:#b5b5b5}}
 .btn{{margin-left:auto;background:#3ecf8e;color:#0d0d0d;text-decoration:none;border-radius:999px;padding:4px 14px;font-size:12px;font-weight:600}}
 .why{{color:#8a8a8a;font-size:12px;margin-bottom:8px}}
 pre{{white-space:pre-wrap;background:#141414;border:1px solid #262626;border-radius:6px;padding:10px;margin:0;font-size:12px;color:#cfcfcf;max-height:320px;overflow:auto}}
</style></head><body>
<h1>Кандидаты на восстановление таблиц моделью: {len(candidates)}</h1>
<div class="sub">Отбор автоматический по паспорту страницы: скан или картинка с признаками таблицы.
Кнопка «открыть страницу» ведёт в просмотрщик — там видна исходная страница, по ней и проверяем, таблица ли это.</div>
{''.join(rows)}
</body></html>"""

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(doc)
    print(f"  отчёт: {out_path}", flush=True)
    by_doc = defaultdict(int)
    label = {}
    for c in candidates:
        by_doc[c["doc_id"]] += 1
        label[c["doc_id"]] = c["filename"]
    print("  по документам (id — чтобы не путать одноимённые):")
    for doc_id, n in sorted(by_doc.items(), key=lambda x: -x[1])[:12]:
        print(f"    {n:>3}  {doc_id[:8]}  {str(label[doc_id])[:44]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
