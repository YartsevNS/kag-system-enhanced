"""Проверка ветки просмотра скана живым браузером (без входа в систему).

Почему свой сервер: страница использует pdf.js по пути /static/pdf.min.js. При загрузке из файла этот путь не
резолвится, верхнеуровневая строка pdfjsLib.* выбрасывает исключение, и остаток скрипта не выполняется — проверка
становится нечестной. Поэтому кладём страницу во временный каталог, добавляем заглушку pdf.min.js и отдаём всё
по HTTP, как это делает настоящий сервер. Запросы API подменяются на картинку-документ.

Запуск в контейнере api:
    docker exec kag-api python /app/data/check_viewer_page.py /app/src/api/static/viewer.html
"""
import http.server
import os
import shutil
import socketserver
import sys
import tempfile
import threading

from playwright.sync_api import sync_playwright

PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000080000000808020000004b6d29dc0000000b49444154"
    "08d7cff80f00030001000c8a5c8d0000000049454e44ae426082"
)

PDFJS_STUB = """
var pdfjsLib = { GlobalWorkerOptions: {}, getDocument: function () {
  return { promise: Promise.reject(new Error('в этой проверке PDF не используется')) };
} };
"""


def serve(directory: str):
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=directory, **k)  # noqa: E731
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def main() -> int:
    src = sys.argv[1] if len(sys.argv) > 1 else "/app/src/api/static/viewer.html"
    doc_id = "9e116f5c-edee-4574-a854-5361be9be855"
    tmp = tempfile.mkdtemp(prefix="viewer_check_")
    os.makedirs(os.path.join(tmp, "static"), exist_ok=True)
    shutil.copyfile(src, os.path.join(tmp, "viewer.html"))
    with open(os.path.join(tmp, "static", "pdf.min.js"), "w", encoding="utf-8") as f:
        f.write(PDFJS_STUB)
    with open(os.path.join(tmp, "static", "branding.js"), "w", encoding="utf-8") as f:
        f.write("// заглушка\n")

    httpd, port = serve(tmp)
    url = f"http://127.0.0.1:{port}/viewer.html?id={doc_id}"
    ok = False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"])
            page = browser.new_context(viewport={"width": 1400, "height": 900}).new_page()
            errors = []
            console = []
            page.on("console", lambda m: console.append(m.text[:160]) if m.type in ("error", "warning") else None)
            page.on("pageerror", lambda e: errors.append(((getattr(e, "stack", "") or "").split(chr(10))[:4] or [str(e)[:140]])))
            page.route("**/upload/*/details*", lambda r: r.fulfill(
                status=200, content_type="application/json",
                body='{"id":"%s","filename":"skan-nakladnoj3.png","file_type":"image/png",'
                     '"status":"completed","chunks_count":4,"recognized_title":"Счёт-фактура"}' % doc_id))
            page.route("**/upload/*/preview*", lambda r: r.fulfill(
                status=200, content_type="image/png", body=PNG_1PX))
            page.route("**/chunks*", lambda r: r.fulfill(
                status=200, content_type="application/json", body='{"chunks":[]}'))

            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2500)

            mode = page.evaluate("() => (typeof viewMode === 'string' ? viewMode : '?')")
            has_img = page.evaluate("!!document.getElementById('page-image')")
            visible = page.evaluate(
                "() => { const i = document.getElementById('page-image'); if (!i) return false;"
                " const r = i.getBoundingClientRect();"
                " return r.width > 0 && getComputedStyle(i).display !== 'none'; }")
            w0 = page.evaluate("() => (document.getElementById('page-image')||{}).clientWidth || 0")
            page.evaluate("() => { zoomIn(); zoomIn(); }")
            page.wait_for_timeout(300)
            w1 = page.evaluate("() => (document.getElementById('page-image')||{}).clientWidth || 0")
            label = page.evaluate("() => (document.getElementById('zoom-label')||{}).textContent")
            pages = page.evaluate("() => (document.getElementById('page-total')||{}).textContent")
            leaked = page.evaluate("() => document.body.innerText.includes('function initImageViewer')")
            page_text = page.evaluate("() => (document.body.innerText || '').slice(0, 400)")
            print(f"  режим просмотра: {mode}")
            print(f"  страница документа показана: {'да' if has_img and visible else 'НЕТ'}")
            print(f"  ширина: {w0} → {w1} после двух шагов зума (метка {label})")
            print(f"  страниц: {pages}")
            print(f"  исходник кода виден: {'ДА (плохо)' if leaked else 'нет'}")
            if not has_img:
                print("  ЧТО ВИДИТ ПОЛЬЗОВАТЕЛЬ: " + page_text.replace(chr(10), ' | ')[:300])
            for c in console[:4]:
                print(f"  консоль: {c}")
            if errors:
                print("  ошибки страницы:")
                for e in errors[:2]:
                    for line in (e if isinstance(e, list) else [e]):
                        print("    " + str(line)[:150])
            ok = bool(mode == "image" and has_img and visible and w1 > w0 and not leaked)
            print(f"  ИТОГ: {'ветка скана работает' if ok else 'ПРОБЛЕМА'}")
            browser.close()
    finally:
        httpd.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
