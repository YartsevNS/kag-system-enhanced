"""Проверка панели текста в просмотрщике живым браузером.

Запуск в контейнере api:
    docker exec kag-api python /app/data/check_viewer_text.py /app/data/viewer_check.html
"""
import sys

from playwright.sync_api import sync_playwright


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else "/app/data/viewer_check.html"
    url = target if target.startswith("http") else "file://" + target
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_context(viewport={"width": 1400, "height": 900}).new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)[:200]))
        page.goto(url, wait_until="domcontentloaded", timeout=45000)

        print("loadViewerTables:", page.evaluate("typeof loadViewerTables"))
        print("loadViewerText:", page.evaluate("typeof loadViewerText"))
        print("блок разметки viewer-text:", page.evaluate("!!document.getElementById('viewer-text')"))
        print("textarea/pre для текста:", page.evaluate("!!document.getElementById('viewer-text-body')"))
        print("кнопка копирования:", page.evaluate("!!document.getElementById('viewer-text-copy')"))
        body_text = page.evaluate("document.body.innerText || ''")
        print("исходник скрипта видно в тексте страницы:",
              "ДА (плохо)" if "async function loadViewerText" in body_text else "нет")
        print("ошибки JS:", errors or "нет")
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
