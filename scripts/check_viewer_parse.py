"""Проверка просмотрщика живым браузером: скрипт исполняется, а не печатается текстом.

Зачем: владелец увидел на странице исходник loadViewerTables — код оказался вне <script>. Здесь берём HTML
(файл или URL), грузим в Chromium и смотрим: определена ли функция (значит скрипт разобран и выполнен) и нет ли
её исходника в видимом тексте страницы.

Запуск в контейнере api:
    docker exec kag-api python /app/data/check_viewer_parse.py /app/data/viewer_check.html
"""
import sys

from playwright.sync_api import sync_playwright


def check(target: str) -> int:
    url = target if target.startswith("http") else "file://" + target
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_context(viewport={"width": 1400, "height": 900}).new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)[:120]))
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(1500)
        defined = page.evaluate("typeof loadViewerTables")
        visible = page.evaluate("() => document.body.innerText")
        leaked = "function loadViewerTables" in visible or "viewerEsc(s)" in visible
        box = page.evaluate("!!document.getElementById('viewer-tables')")
        scripts = page.evaluate("document.querySelectorAll('script').length")
        print(f"  loadViewerTables: {defined}")
        print(f"  блок таблиц в разметке: {'есть' if box else 'НЕТ'}")
        print(f"  исходник кода виден на странице: {'ДА (плохо)' if leaked else 'нет'}")
        print(f"  тегов script в DOM: {scripts}")
        if errors:
            print(f"  ошибки страницы: {'; '.join(errors[:3])}")
        browser.close()
    return 1 if (defined != "function" or leaked) else 0


if __name__ == "__main__":
    raise SystemExit(check(sys.argv[1] if len(sys.argv) > 1 else "/app/data/viewer_check.html"))
