"""Проверка телефонного вида KAG: что реально работает, а что ломается на узком экране.

Снимает ключевые страницы при ширине телефона (по умолчанию 375x812), измеряет:
  1. горизонтальную прокрутку страницы (лишний overflow = «страница шире экрана»);
  2. виден ли бургер меню и открывается ли по нему панель разделов (меню доступно без колонки);
  3. слишком узкие места нажатия (< 40px) — по ним палец мажет;
  4. элементы шире экрана (что именно распирает страницу).

Запуск в контейнере базового образа:
  docker run --rm --network host -v /work:/work -e ADMIN_PASSWORD=... \
    --entrypoint /usr/local/bin/python kre44et/kag-base:<tag> /work/mobile_check.py admin http://127.0.0.1:8000
"""
import os
import sys

from playwright.sync_api import sync_playwright

USER = sys.argv[1] if len(sys.argv) > 1 else "admin"
BASE = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8000"
PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
OUT = os.environ.get("OUT_DIR", "/work/mobile")
W = int(os.environ.get("WIDTH", "375"))
H = int(os.environ.get("HEIGHT", "812"))
PAGES = os.environ.get("PAGES", "/documents,/chat,/admin,/kg,/login").split(",")

MEASURE = """() => {
    const de = document.documentElement;
    const vw = window.innerWidth;
    const wide = [];
    document.querySelectorAll('body *').forEach(el => {
        const r = el.getBoundingClientRect();
        if (r.width > vw + 2 && r.height > 8 && getComputedStyle(el).position !== 'fixed') {
            wide.push({ tag: el.tagName.toLowerCase(), cls: (el.className || '').toString().slice(0, 30),
                        w: Math.round(r.width) });
        }
    });
    const small = [];
    document.querySelectorAll('button, a, input[type=submit]').forEach(el => {
        const r = el.getBoundingClientRect();
        const cs = getComputedStyle(el);
        if (cs.display === 'none' || cs.visibility === 'hidden') return;
        if (r.width > 0 && r.height > 0 && (r.height < 40 || r.width < 40)) {
            small.push({ text: (el.textContent || '').trim().slice(0, 18),
                         size: Math.round(r.width) + 'x' + Math.round(r.height) });
        }
    });
    return {
        overflow: Math.round(de.scrollWidth) - vw,
        vw: vw,
        wide: wide.slice(0, 4),
        smallCount: small.length,
        small: small.slice(0, 4),
    };
}"""


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": W, "height": H}, device_scale_factor=2,
                                  is_mobile=True, has_touch=True)
        r = ctx.request.post(f"{BASE}/api/v1/auth/login", data={"username": USER, "password": PASSWORD})
        print(f"вход: {r.status} | экран {W}x{H} (телефон)")
        page = ctx.new_page()

        problems = []
        for path in PAGES:
            page.goto(f"{BASE}{path}")
            page.wait_for_timeout(1800)
            m = page.evaluate(MEASURE)
            name = path.strip("/").replace("/", "_") or "root"
            page.screenshot(path=f"{OUT}/{name}.png")
            status = []
            if m["overflow"] > 2:
                status.append(f"прокрутка вбок +{m['overflow']}px")
                detail = ", ".join(f"{w['tag']}.{w['cls']}={w['w']}px" for w in m["wide"])
                problems.append(f"{path}: страница шире экрана на {m['overflow']}px ({detail})")
            if m["smallCount"] > 0:
                problems.append(f"{path}: мест нажатия уже 40px — {m['smallCount']} "
                                f"(напр. {m['small'][0]['text']!r} {m['small'][0]['size']})")
            print(f"  {path:12} прокрутка вбок: {m['overflow']:>3}px | "
                  f"узких нажатий: {m['smallCount']:>2} | {'; '.join(status) if status else 'ок'}")

            # бургер и панель меню
            burger = page.locator(".kag-burger")
            if burger.count():
                visible = burger.first.is_visible()
                print(f"      бургер: {'виден' if visible else 'скрыт'}", end="")
                if visible:
                    burger.first.click()
                    page.wait_for_timeout(600)
                    opened = page.evaluate("() => { const d = document.querySelector('.kag-nav-drawer');"
                                           "return !!d && d.classList.contains('open'); }")
                    items = page.evaluate("() => document.querySelectorAll('.kag-nav-drawer .nav-item').length")
                    print(f", панель {'открылась' if opened else 'НЕ открылась'}, пунктов {items}")
                    page.screenshot(path=f"{OUT}/{name}_menu.png")
                    if not opened or items == 0:
                        problems.append(f"{path}: панель меню не открывается по бургеру")
                else:
                    print()
            else:
                print("      бургер: отсутствует")

        browser.close()
        print()
        if problems:
            print("НАЙДЕНО:")
            for pr in problems:
                print("  -", pr)
        else:
            print("Телефонный вид: прокрутки вбок нет, меню доступно, нажатия не мелкие.")
        print(f"\nснимки: {OUT}")


if __name__ == "__main__":
    main()
