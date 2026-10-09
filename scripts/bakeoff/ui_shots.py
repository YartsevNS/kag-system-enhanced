"""Скриншоты живых страниц KAG для проверки вида (светлая и тёмная тема).

Логинится тем же способом, что браузер (cookie kag_token), снимает список страниц в двух темах.
Запуск в контейнере образа kag-base (там Chromium):
  docker run --rm --network host -v /tmp/kagui:/work -e ADMIN_PASSWORD=... \
    --entrypoint /usr/local/bin/python kre44et/kag-base:<tag> /work/ui_shots.py admin http://127.0.0.1:8000
"""
import os
import sys

from playwright.sync_api import sync_playwright

USER = sys.argv[1] if len(sys.argv) > 1 else "admin"
BASE = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8000"
PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
OUT = os.environ.get("OUT_DIR", "/work")
WIDTH = int(os.environ.get("WIDTH", "1440"))
PAGES = os.environ.get("PAGES", "/chat,/documents,/admin,/kg,/docs,/architecture").split(",")
# Необязательно: селектор, к которому прокрутить перед снимком (например, панель «Оформление»)
SCROLL = os.environ.get("SCROLL_SELECTOR", "")
# Необязательно: селектор элемента — тогда снимок делается только по нему (удобно для панелей)
ELEMENT = os.environ.get("ELEMENT_SELECTOR", "")
# Сколько ждать после загрузки перед снимком (внешние панели, например Grafana, грузятся дольше)
WAIT_MS = int(os.environ.get("WAIT_MS", "1800"))


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        # ignore_https_errors: система отдаётся по https с самоподписанным сертификатом,
        # без этого браузер обрывает переход и снимки не делаются
        ctx = browser.new_context(viewport={"width": WIDTH, "height": 950}, ignore_https_errors=True)
        if PASSWORD:
            r = ctx.request.post(f"{BASE}/api/v1/auth/login",
                                 data={"username": USER, "password": PASSWORD})
            print("вход:", r.status)
        page = ctx.new_page()
        for path in PAGES:
            name = path.strip("/").replace("/", "_") or "root"
            for theme in ("light", "dark"):
                page.goto(f"{BASE}{path}")
                page.evaluate("(t) => { document.documentElement.setAttribute('data-theme', t); "
                              "try{localStorage.setItem('kag-theme', t)}catch(e){} }", theme)
                page.wait_for_timeout(WAIT_MS)
                if SCROLL:
                    # Прокрутка через JS: страница после загрузки данных сама возвращается наверх,
                    # поэтому прокручиваем прямо перед снимком и ждём отрисовку.
                    try:
                        page.evaluate("""(sel) => {
                            const el = document.querySelector(sel);
                            if (el) el.scrollIntoView({block: 'center'});
                        }""", SCROLL)
                        page.wait_for_timeout(700)
                    except Exception as e:
                        print(f"    прокрутка к {SCROLL} не удалась: {e}")
                if ELEMENT:
                    try:
                        page.locator(ELEMENT).first.screenshot(
                            path=f"{OUT}/{name}_{theme}_element.png", timeout=5000)
                        print(f"  {path} [{theme}] -> {name}_{theme}_element.png (элемент)")
                        continue
                    except Exception as e:
                        print(f"    снимок элемента {ELEMENT} не удался: {e}")
                page.screenshot(path=f"{OUT}/{name}_{theme}.png", full_page=False)
                print(f"  {path} [{theme}] -> {name}_{theme}.png")
        browser.close()


if __name__ == "__main__":
    main()
