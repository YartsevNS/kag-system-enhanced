"""Живая проверка страницы «Семантика»: рендер в Chromium + снимок для просмотра глазами.

Зачем: вёрстку и схему нельзя проверить ни компиляцией, ни чтением исходника — нужно открыть
страницу настоящим браузером с настоящей сессией, убедиться, что числа пришли, и ПОСМОТРЕТЬ
снимок (схема читаема? не наезжают ли подписи?).

Запуск в контейнере api (там есть Playwright и ADMIN_PASSWORD):
    docker cp scripts/e2e_semantics_page.py kag-api:/tmp/e2e_sem.py
    docker exec kag-api python /tmp/e2e_sem.py            # + снимок /tmp/semantics_page.png
"""
import asyncio
import os

from playwright.async_api import async_playwright

PAGE_URL = "http://api:8000/semantics"
API = "http://api:8000/api/v1"
SHOT = "/tmp/semantics_page.png"


async def main() -> int:
    checks = []

    def check(name, ok, extra=""):
        checks.append(ok)
        print(("OK   " if ok else "ФЕЙЛ ") + name + (f" — {extra}" if extra else ""))

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context(viewport={"width": 1400, "height": 1000})
        resp = await context.request.post(
            API + "/auth/login",
            data={"username": os.environ.get("ADMIN_USERNAME", "admin"),
                  "password": os.environ["ADMIN_PASSWORD"]},
        )
        check("вход в API", resp.status == 200, f"код {resp.status}")

        page = await context.new_page()
        errors, bad = [], []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("response", lambda r: bad.append(f"{r.status} {r.url}") if r.status >= 400 else None)

        r = await page.goto(PAGE_URL, wait_until="domcontentloaded")
        check("страница отдана", (r.status if r else 0) == 200, f"код {r.status if r else 0}")
        await page.wait_for_timeout(5000)

        cards = await page.evaluate("document.querySelectorAll('#vocab .card').length")
        rows = await page.evaluate("document.querySelectorAll('#vocab table tbody tr').length")
        tiles = await page.evaluate("document.querySelectorAll('#stores .tile').length")
        steps = await page.evaluate("document.querySelectorAll('.steps .step').length")
        svg = await page.evaluate("document.querySelectorAll('main svg').length")
        diagrams = await page.evaluate(
            "Array.from(document.querySelectorAll('main img'))"
            ".filter(i => (i.getAttribute('src')||'').includes('diagrams/'))"
            ".map(i => ({src: i.getAttribute('src'), w: i.naturalWidth, h: i.naturalHeight}))"
        )
        stamp = await page.evaluate("(document.getElementById('stamp')||{}).textContent || ''")

        check("три таблицы словарей", cards == 3, f"карточек {cards}")
        check("в таблицах есть строки (данные пришли с сервера)", rows >= 20, f"строк {rows}")
        check("плитки хранилищ отрисованы", tiles >= 6, f"плиток {tiles}")
        check("конвейер из 8 шагов", steps == 8, f"шагов {steps}")
        # Схемы теперь готовые SVG из репозитория (Graphviz), а не нарисованные в HTML
        check("обе схемы загрузились (axes + pipeline)", len(diagrams) == 2,
              "; ".join(f"{d['src']} {d['w']}x{d['h']}" for d in diagrams) or "ни одной")
        check("схемы отрисовались не нулевой высоты",
              all(d["h"] > 50 for d in diagrams), f"{[d['h'] for d in diagrams]}")
        check("метка снимка времени", "снимок" in stamp, stamp or "—")
        check("нет ошибок JS", not errors, "; ".join(errors[:2]) if errors else "чисто")
        check("нет ответов не-2xx", not bad, "; ".join(bad[:2]) if bad else "нет")

        await page.screenshot(path=SHOT, full_page=True)
        print("снимок:", SHOT)
        await browser.close()

    ok = all(checks)
    print(f"\nVERDICT {'OK' if ok else 'ФЕЙЛ'} {sum(checks)}/{len(checks)}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
