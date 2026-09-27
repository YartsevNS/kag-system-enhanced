"""Отрисовка блока «Таблицы документа» тем же кодом разметки, что в просмотрщике.

Зачем: браузер на рабочей машине недоступен, а страница просмотрщика требует входа. Здесь берём данные
табличного слоя из базы и рендерим ровно ту разметку, которую строит /viewer (тот же JS, тот же стиль),
чтобы посмотреть глазами и показать владельцу. Паролей не требуется — только чтение базы.

Запуск в контейнере api:
    docker exec -i kag-api python /app/data/render_preview.py <document_id> [выходной_файл.png]
"""
import html as _html
import json
import sys

MARKUP_HEAD = """<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<style>
 body{background:#171717;color:#e6e6e6;font:14px/1.45 -apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:0;padding:20px}
 .card{border:1px solid #333;border-radius:6px;padding:8px;margin-bottom:10px}
 .meta{font-size:11px;color:#898989;margin-bottom:6px}
 table{border-collapse:collapse;font-size:12px;min-width:100%}
 th{border:1px solid #333;padding:4px 6px;text-align:left;background:#1f1f1f}
 td{border:1px solid #333;padding:4px 6px;vertical-align:top}
 td.num{text-align:right}
 h3{margin:0 0 10px 0;font-size:15px}
</style></head><body>
<h3>Блок «Таблицы документа» — как он выглядит в просмотрщике</h3>
"""


def esc(v) -> str:
    return _html.escape(str(v if v is not None else ""))


def main() -> int:
    doc_id = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else "/app/data/viewer_preview.png"

    from src.database.document_table_models import DocumentTable
    from src.database.session import get_session_local

    maker = get_session_local()
    session = maker()
    tables = (session.query(DocumentTable).filter_by(document_id=doc_id)
              .order_by(DocumentTable.page_num, DocumentTable.table_index).all())

    parts = [MARKUP_HEAD]
    if not tables:
        parts.append('<div style="font-size:13px;color:#898989">Таблиц нет — блок в просмотрщике не показывается.</div>')
    for i, t in enumerate(tables):
        rows = json.loads(t.rows_json or "[]")
        heads = json.loads(t.headers_json or "[]")
        parts.append('<div class="card">')
        parts.append(
            f'<div class="meta">Таблица {i + 1} · страница {t.page_num or 1} · строк {t.row_count or len(rows)}'
            f' · качество {t.quality if t.quality is not None else "—"} · распознано: {esc(t.model or "—")}</div>')
        parts.append("<table>")
        if heads:
            parts.append("<thead><tr>" + "".join(f"<th>{esc(h)}</th>" for h in heads) + "</tr></thead>")
        body = []
        for r in rows[:25]:
            cells = []
            for c in r:
                text = str(c).strip()
                numeric = bool(text) and all(ch.isdigit() or ch in " .,%:+-" for ch in text.replace("\u00a0", " "))
                cells.append(f'<td{" class=num" if numeric else ""}>{esc(c)}</td>')
            body.append("<tr>" + "".join(cells) + "</tr>")
        parts.append("<tbody>" + "".join(body) + "</tbody></table></div>")
    parts.append("</body></html>")
    session.close()

    page_html = "".join(parts)
    with open("/tmp/viewer_preview_src.html", "w", encoding="utf-8") as f:
        f.write(page_html)
    print(f"  таблиц: {len(tables)} | html {len(page_html)} символов")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_context(viewport={"width": 1700, "height": 1100}).new_page()
        page.goto("file:///tmp/viewer_preview_src.html", wait_until="load")
        page.wait_for_timeout(800)
        page.screenshot(path=out, full_page=True)
        print(f"  снимок: {out}")
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
