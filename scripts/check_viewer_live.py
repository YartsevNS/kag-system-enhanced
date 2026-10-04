"""Живая проверка просмотрщика: вход под администратором и открытие реальной страницы документа.

Запуск в контейнере api:
    docker exec kag-api python /app/data/check_viewer_live.py <document_id>

Смысл: проверить именно то, что видит владелец — рендер страницы, текстовый слой и ошибки в консоли.
"""
import json
import os
import sys
import urllib.request

from playwright.sync_api import sync_playwright

DOC = sys.argv[1] if len(sys.argv) > 1 else "e4da0d35-a70d-48a7-894a-368a93aa7054"


def _token() -> str:
    user = os.environ.get("ADMIN_USERNAME") or "admin"
    pwd = os.environ.get("ADMIN_PASSWORD") or ""
    body = json.dumps({"username": user, "password": pwd}).encode()
    req = urllib.request.Request("http://127.0.0.1:8000/api/v1/auth/login", body,
                                 {"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read()).get("access_token", "")


def main() -> int:
    token = _token()
    print("вход выполнен:", bool(token))
    if not token:
        return 1

    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        context = browser.new_context(viewport={"width": 1400, "height": 1000})
        context.add_cookies([{"name": "kag_token", "value": token, "domain": "127.0.0.1", "path": "/"}])
        page = context.new_page()
        page.on("pageerror", lambda e: errors.append("pageerror: " + str(e)[:200]))
        page.on("console", lambda m: errors.append("console: " + m.text[:200]) if m.type == "error" else None)
        page.goto(f"http://127.0.0.1:8000/viewer?id={DOC}", wait_until="load", timeout=60000)
        page.wait_for_timeout(7000)

        info = page.evaluate("""(() => {
            const canvas = document.querySelector('.pdf-page canvas') || document.querySelector('#viewer canvas');
            const layer = document.querySelector('.textLayer');
            const img = document.getElementById('page-image');
            return {
                canvas: canvas ? canvas.width + 'x' + canvas.height : 'нет',
                textSpans: layer ? layer.querySelectorAll('span').length : 0,
                картинка: img ? (img.naturalWidth + 'x' + img.naturalHeight) : 'нет',
                пусто: (document.getElementById('viewer') || {}).innerHTML ? 'нет' : 'да'
            };
        })()""")
        print("страница:", info)

        # Главное требование: текст на скане должен выделяться (как в PDF с текстовым слоем).
        selected = page.evaluate("""(() => {
            const layer = document.querySelector('.textLayer');
            if (!layer) return 'текстового слоя нет';
            const range = document.createRange();
            range.selectNodeContents(layer);
            const sel = window.getSelection();
            sel.removeAllRanges();
            sel.addRange(range);
            return (sel.toString() || '').replace(/\\s+/g, ' ').trim().slice(0, 140);
        })()""")
        print("выделяется текста:", len(selected), "символов:", selected[:110])

        print("ошибки JS:", errors[:5] or "нет")
        page.screenshot(path="/app/data/viewer_live.png")
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
