"""Скриншот страницы просмотрщика с блоком таблиц (для проверки глазами).

Запуск в контейнере api (там есть Playwright и Chromium):
    KAG_USER=<логин> docker exec -e KAG_USER -e ADMIN_PASSWORD kag-api \
        python /app/data/shot_viewer.py <document_id> [выходной_файл]
"""
import json
import os
import re
import sys
import urllib.request

BASE = os.environ.get("KAG_INTERNAL_URL", "http://127.0.0.1:8000")


def login_cookie(user: str, password: str) -> str:
    body = json.dumps({"username": user, "password": password}).encode()
    req = urllib.request.Request(BASE + "/api/v1/auth/login", body,
                                 {"Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=30)
    raw = resp.headers.get("Set-Cookie", "")
    m = re.search(r"(kag_token=[^;]+)", raw)
    return m.group(1) if m else ""


def main() -> int:
    doc_id = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else "/app/data/viewer_tables.png"
    user = os.environ.get("KAG_USER", "")
    password = os.environ.get("ADMIN_PASSWORD", "")
    if not user or not password:
        print("  не заданы KAG_USER/ADMIN_PASSWORD")
        return 1

    cookie = login_cookie(user, password)
    print(f"  вход выполнен: {'да' if cookie else 'НЕТ'}")

    from playwright.sync_api import sync_playwright

    url = f"{BASE}/viewer?id={doc_id}"
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        if cookie:
            name, value = cookie.split("=", 1)
            context.add_cookies([{"name": name, "value": value, "url": BASE}])
        page = context.new_page()
        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(4000)
        title = page.title()
        print(f"  страница: {title!r} | url: {page.url}")

        # Раскрываем блок таблиц (он сворачиваемый) и снимаем его отдельно
        has_box = page.evaluate("!!document.getElementById('viewer-tables')")
        print(f"  блок «Таблицы документа» в разметке: {'есть' if has_box else 'НЕТ'}")
        if has_box:
            page.evaluate("() => { const b = document.getElementById('viewer-tables');"
                          " b.setAttribute('open',''); b.style.display=''; }")
            page.wait_for_timeout(2000)
            summary = page.evaluate("() => (document.getElementById('viewer-tables-summary')||{}).textContent")
            rows = page.evaluate("() => document.querySelectorAll('#viewer-tables-body tr').length")
            print(f"  заголовок блока: {summary!r} | строк в таблице на странице: {rows}")
            el = page.query_selector("#viewer-tables")
            if el:
                el.screenshot(path=out)
                print(f"  снимок блока: {out}")
        page.screenshot(path=out.replace(".png", "_full.png"), full_page=True)
        print(f"  снимок страницы: {out.replace('.png', '_full.png')}")
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
