"""Отрендерить HTML в PNG через Playwright (запускать внутри образа kag-base, где есть Chromium).

Зачем: проверить вёрстку схемы ГЛАЗАМИ, а не на слово. Локально на Windows нет cairo, поэтому рендер
идёт в контейнере: docker run --rm -v <каталог>:/work --entrypoint /opt/venv/bin/python \
  kre44et/kag-base:<tag> /work/render_html.py /work/page.html /work/page.png
"""
import sys

from playwright.sync_api import sync_playwright


def main() -> None:
    src, dst = sys.argv[1], sys.argv[2]
    width = int(sys.argv[3]) if len(sys.argv) > 3 else 1400
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": width, "height": 1000})
        page.goto("file://" + src)
        page.wait_for_timeout(2000)
        page.screenshot(path=dst, full_page=True)
        browser.close()
    print(f"готово: {dst}")


if __name__ == "__main__":
    main()
