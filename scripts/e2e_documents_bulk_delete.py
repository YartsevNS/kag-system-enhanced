"""Живая проверка массового удаления на странице «Документы» в НАСТОЯЩЕМ браузере.

Зачем: две жалобы подряд (20.09.2026) — «выбор слетает» и «кнопка удалить выбранные
не действует» (на сервер при этом не уходило ни одного DELETE). Проверка в jsdom
показала, что причина второй — подтверждение через window.confirm(), который браузер
может глушить. Здесь то же самое проверяется в Chromium на самой странице с стенда:
сеть подменяется заглушкой (чтобы не удалять настоящие документы), но клики, панель,
подтверждение и запросы — настоящие.

Запуск (Playwright + Chromium есть в контейнере worker):
    docker cp scripts/e2e_documents_bulk_delete.py kag-system_worker_1:/tmp/e2e_bulk.py
    docker exec kag-system_worker_1 python /tmp/e2e_bulk.py

Проверяет: выбор переживает автообновление; 1-е нажатие = подтверждение (запросов нет);
2-е нажатие = ровно N DELETE по отмеченным; системный диалог не вызывается; ошибок
страницы нет. Итог — строка VERDICT.
"""
import asyncio
import json

from playwright.async_api import async_playwright

PAGE_URL = "http://api:8000/static/documents.html"

INIT = """
window.__deleted = [];
window.__dialogs = 0;
window.__docs = [
  {document_id: 'doc-1', filename: 'a.pdf', recognized_title: 'A', status: 'completed', chunks_count: 53, created_at: '2026-09-19T10:00:00', tags: []},
  {document_id: 'doc-2', filename: 'b.pdf', recognized_title: 'B', status: 'completed', chunks_count: 2, created_at: '2026-09-19T10:00:00', tags: []},
  {document_id: 'doc-3', filename: 'c.pdf', recognized_title: 'C', status: 'completed', chunks_count: 1, created_at: '2026-09-19T10:00:00', tags: []},
];
window.fetch = async (url, opts) => {
  const u = String(url);
  const m = ((opts && opts.method) || 'GET').toUpperCase();
  if (u.includes('/upload/list')) {
    return {ok: true, status: 200, json: async () => ({documents: window.__docs, total: window.__docs.length})};
  }
  if (m === 'DELETE' && u.includes('/upload/')) {
    window.__deleted.push(u.split('/upload/')[1]);
    return {ok: true, status: 200, json: async () => ({status: 'ok'})};
  }
  return {ok: true, status: 200, json: async () => ({})};
};
window.confirm = () => { window.__dialogs++; return true; };
window.alert = () => { window.__dialogs++; };
"""


async def main():
    checks = []

    def check(name, ok, extra=""):
        checks.append(ok)
        print(("OK   " if ok else "ФЕЙЛ ") + name + (f" — {extra}" if extra else ""))

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(f"{e} | stack={str(getattr(e, 'stack', ''))[:300]}"))
        console_errors = []
        page.on("console", lambda m: console_errors.append(
            f"{m.text[:200]} @ {getattr(m, 'location', {})}") if m.type == "error" else None)
        failed_reqs = []
        page.on("requestfailed", lambda r: failed_reqs.append(f"{r.url} — {r.failure}"))
        unauthorized = []
        # Миниатюры грузятся тегом <img>, а не fetch: на статической странице без сессии
        # сервер отдаёт по ним 401 — это особенность проверки, а не дефект страницы.
        # Остальные 401/403 в ходе прогона считаем провалом.
        page.on("response", lambda r: unauthorized.append(f"{r.status} {r.url}")
                if r.status in (401, 403) and "/thumbnail" not in r.url else None)

        await page.add_init_script(INIT)
        await page.goto(PAGE_URL, wait_until="domcontentloaded")
        # Сначала переключаем на таблицу: по умолчанию открывается вид «Карточки»,
        # и в нём строки таблицы есть в DOM, но не видны — клик по ним невозможен
        # (первый прогон падал именно на этом: «resolved to 3 elements», но не visible).
        await page.evaluate("window.setView('table')")
        await page.wait_for_selector("#table-body .doc-check", state="visible", timeout=15000)

        # отметить три файла настоящими кликами по чекбоксам
        boxes = await page.query_selector_all("#table-body .doc-check")
        for b in boxes:
            await b.click()
        count = await page.inner_text("#bulk-count")
        check("после трёх кликов панель показала «Выбрано: 3»", "Выбрано: 3" in count, count)

        # автообновление: выбор должен остаться
        await page.evaluate("window.loadAll(true)")
        checked = await page.evaluate(
            "[...document.querySelectorAll('.doc-check')].filter(c => c.checked).length")
        check("автообновление не сбросило выбор", checked == 3, f"отмечено {checked}")

        # 1-е нажатие — подтверждение, 2-е — удаление
        await page.click("#btn-bulk-delete")
        deleted_after_first = await page.evaluate("window.__deleted.length")
        btn_text = await page.inner_text("#btn-bulk-delete")
        status = await page.inner_text("#bulk-status")
        check("первое нажатие не удаляет, а просит подтверждения",
              deleted_after_first == 0 and "Нажмите ещё раз" in btn_text, btn_text)
        check("подтверждение показано в панели", "нажмите кнопку ещё раз" in status, status)

        await page.click("#btn-bulk-delete")
        await page.wait_for_timeout(500)
        deleted = await page.evaluate("window.__deleted")
        status = await page.inner_text("#bulk-status")
        check("второе нажатие отправило DELETE ровно по отмеченным",
              sorted(deleted) == ["doc-1", "doc-2", "doc-3"], json.dumps(deleted))
        check("итог удаления виден в панели", "Удалено: 3" in status, status)

        dialogs = await page.evaluate("window.__dialogs")
        check("системный диалог не вызывался", dialogs == 0, f"вызовов: {dialogs}")

        # Inline-обработчики должны компилироваться: несобранный onerror/onclick
        # молча ничего не делает (найдено этим прогоном: у миниатюр карточек
        # в onerror не хватало закрывающей кавычки — «Invalid or unexpected token»).
        bad_handlers = await page.evaluate("""() => {
          const out = [];
          for (const el of document.querySelectorAll('*')) {
            for (const attr of ['onerror', 'onclick', 'onchange']) {
              const v = el.getAttribute && el.getAttribute(attr);
              if (!v) continue;
              try { new Function(v); } catch (e) { out.push(el.tagName + ' ' + attr + ': ' + e.message); }
            }
          }
          return out;
        }""")
        check("все inline-обработчики страницы компилируются", not bad_handlers,
              "; ".join(bad_handlers)[:200])
        # Ошибки страницы и ответы 401 не должны быть вызваны самой проверкой:
        # печатаем подробности (файл, строка, URL), иначе «есть ошибки» неисправимо.
        if errors:
            print("   подробности ошибок страницы:")
            for e in errors:
                print("     - " + e)
        if unauthorized:
            print("   ответы 401/403:")
            for u in unauthorized[:5]:
                print("     - " + u)
        if failed_reqs:
            print("   не удались запросы:")
            for f in failed_reqs[:5]:
                print("     - " + f)
        check("ошибок страницы нет", not errors, "; ".join(errors)[:200])
        check("доступов 401/403 в ходе проверки нет", not unauthorized, "; ".join(unauthorized)[:200])

        await browser.close()

    ok = all(checks)
    print("\nVERDICT " + ("OK" if ok else "FAIL") + f" — {sum(checks)}/{len(checks)}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
