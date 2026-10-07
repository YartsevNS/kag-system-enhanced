"""Проверка оформления KAG приборно: задать настройку -> посмотреть, что реально применилось.

Логинится администратором, сохраняет настройки через тот же API, что и админка, открывает
страницу и читает вычисленные стили (шрифт, размер, толщина, контраст, цвет). Затем возвращает
стандарт. Так видно, применяется ли настройка на самом деле, а не «должна применяться».

Запуск в контейнере базового образа (там Chromium):
  docker run --rm --network host -v /work:/work -e ADMIN_PASSWORD=... \
    --entrypoint /usr/local/bin/python kre44et/kag-base:<tag> /work/appearance_probe.py admin http://127.0.0.1:8000
"""
import os
import sys

from playwright.sync_api import sync_playwright

USER = sys.argv[1] if len(sys.argv) > 1 else "admin"
BASE = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8000"
PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

TEST = {
    "font_body": "tahoma",
    "font_display": "times",
    "font_size": 17,
    "font_weight": 500,
    "contrast": "high",
    "color_accent": "#b3005f",
    "color_bg": "#fbf7f2",
    "color_surface": "#ffffff",
    "color_text": "#101418",
}
RESET = {
    "font_body": "system", "font_display": "pt-serif", "font_size": 14, "font_weight": 400,
    "contrast": "normal", "color_accent": "", "color_bg": "", "color_surface": "", "color_text": "",
}


def read(page):
    return page.evaluate("""() => {
        const cs = getComputedStyle(document.body);
        const root = document.documentElement;
        return {
            font: cs.fontFamily.split(',')[0].replace(/["']/g, ''),
            size: cs.fontSize,
            weight: cs.fontWeight,
            contrast: root.getAttribute('data-contrast'),
            accent: getComputedStyle(root).getPropertyValue('--accent').trim(),
            bg: getComputedStyle(root).getPropertyValue('--bg').trim(),
        };
    }""")


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 950})
        r = ctx.request.post(f"{BASE}/api/v1/auth/login", data={"username": USER, "password": PASSWORD})
        print("вход:", r.status)

        api = f"{BASE}/api/v1/admin/models/branding-config"
        before = ctx.request.get(api).json()
        print("настройки до:", {k: before.get(k) for k in TEST})

        page = ctx.new_page()
        page.goto(f"{BASE}/documents")
        page.wait_for_timeout(1500)
        print("страница до правки:", read(page))

        res = ctx.request.post(api, data=TEST).json()
        print("сохранение тестовой настройки:", res.get("status"))

        page.goto(f"{BASE}/documents")
        page.wait_for_timeout(1500)
        got = read(page)
        print("страница после правки:", got)

        checks = [
            ("шрифт текста Tahoma применён", "Tahoma" in got["font"]),
            ("шрифт заголовков Times применён", page.evaluate(
                "() => getComputedStyle(document.querySelector('h1')).fontFamily.indexOf('Times') >= 0")),
            ("размер текста 17px", got["size"] == "17px"),
            ("толщина 500", got["weight"] == "500"),
            ("повышенный контраст включён", got["contrast"] == "high"),
            ("цвет акцента применён", got["accent"].lower().startswith("#b3005f")),
            ("фон применён", got["bg"].lower().startswith("#fbf7f2")),
        ]
        print("\nпроверки:")
        bad = 0
        for name, ok in checks:
            print(f"  {'ок  ' if ok else 'НЕТ '} {name}")
            bad += 0 if ok else 1

        ctx.request.post(api, data=RESET)
        page.goto(f"{BASE}/documents")
        page.wait_for_timeout(1500)
        back = read(page)
        print("\nпосле возврата стандарта:", back)
        ok_back = back["contrast"] == "normal" and back["size"] != "17px"
        print("  стандарт вернулся:", "ок" if ok_back else "НЕТ")

        browser.close()
        print("\nИТОГ:", "всё применилось" if bad == 0 and ok_back else f"провалов {bad + (0 if ok_back else 1)}")
        sys.exit(1 if (bad or not ok_back) else 0)


if __name__ == "__main__":
    main()
