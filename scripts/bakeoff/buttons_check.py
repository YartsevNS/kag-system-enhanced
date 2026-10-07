"""Проверка формы кнопок: все кнопки должны быть прямоугольными.

Требование владельца (05.10.2026): один радиус у всех кнопок, без «таблеток». Раньше переключатель
вида на «Документах» выглядел разнобоем («Обновить» овальная, соседние прямоугольные), а страница
входа и админка задавали 9999px.

Скрипт открывает страницы, собирает вычисленный border-radius у кнопок и управляющих элементов
и сообщает всё, что круглее порога. Круглые плашки-бейджи (<span class="tag">) и аватары не считаются
кнопками и в проверку не входят.

Запуск в контейнере базового образа:
  docker run --rm --network host -v /work:/work -e ADMIN_PASSWORD=... \
    --entrypoint /usr/local/bin/python kre44et/kag-base:<tag> /work/buttons_check.py admin http://127.0.0.1:8000
"""
import os
import sys

from playwright.sync_api import sync_playwright

USER = sys.argv[1] if len(sys.argv) > 1 else "admin"
BASE = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8000"
PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
MAX_RADIUS_PX = 10          # всё, что круглее, — уже не прямоугольник

PAGES = ["/documents", "/admin", "/chat", "/users", "/logs", "/kg", "/system", "/monitor"]

COLLECT = """(maxRadius) => {
    const sel = 'button, input[type=submit], input[type=button], a.btn, .btn, .btn-primary, ' +
                '.btn-outline, .header-btn, .new-chat-btn, .send-btn, .tab, .kag-theme-toggle';
    const out = [];
    document.querySelectorAll(sel).forEach(el => {
        const cs = getComputedStyle(el);
        if (cs.display === 'none' || cs.visibility === 'hidden') return;
        const r = el.getBoundingClientRect();
        if (r.width < 4 || r.height < 4) return;
        const px = parseFloat(cs.borderTopLeftRadius) || 0;
        const limit = Math.min(r.width, r.height) / 2 - 1;   // овал = радиус в половину высоты
        out.push({
            text: (el.textContent || el.value || '').trim().slice(0, 24),
            radius: px,
            size: Math.round(r.width) + 'x' + Math.round(r.height),
            pill: px > maxRadius || (limit > 0 && px >= limit),
        });
    });
    return out;
}"""


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 950})
        r = ctx.request.post(f"{BASE}/api/v1/auth/login", data={"username": USER, "password": PASSWORD})
        print("вход:", r.status)

        page = ctx.new_page()
        problems = []
        total = 0
        for path in PAGES:
            page.goto(f"{BASE}{path}")
            page.wait_for_timeout(1500)
            items = page.evaluate(COLLECT, MAX_RADIUS_PX)
            pills = [i for i in items if i["pill"]]
            total += len(items)
            print(f"  {path:12} кнопок {len(items):3} | овальных {len(pills)}")
            for i in pills:
                problems.append(f"{path}: «{i['text'] or 'без подписи'}» радиус {i['radius']}px, размер {i['size']}")

        # страница входа — отдельно: там нет сессии
        ctx2 = browser.new_context(viewport={"width": 1280, "height": 900})
        page2 = ctx2.new_page()
        page2.goto(f"{BASE}/login")
        page2.wait_for_timeout(1200)
        items = page2.evaluate(COLLECT, MAX_RADIUS_PX)
        pills = [i for i in items if i["pill"]]
        total += len(items)
        print(f"  {'/login':12} кнопок {len(items):3} | овальных {len(pills)}")
        for i in pills:
            problems.append(f"/login: «{i['text'] or 'без подписи'}» радиус {i['radius']}px, размер {i['size']}")

        browser.close()
        print(f"\nвсего проверено кнопок: {total}")
        if problems:
            print("НЕ прямоугольные:")
            for pr in problems:
                print("  -", pr)
            sys.exit(1)
        print("ИТОГ: все кнопки прямоугольные (радиус не больше "
              f"{MAX_RADIUS_PX}px), включая страницу входа")


if __name__ == "__main__":
    main()
