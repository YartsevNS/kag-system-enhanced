"""Проверка страницы «Граф знаний»: строится ли граф и что мешает.

Зачем: владелец сообщил, что граф вокруг сущности вообще не строится. Бэкенд при этом отвечает
(проверено отдельно: /kg/graph/<сущность> отдаёт узлы и связи), значит смотреть надо браузер:
ошибки консоли, число элементов в cytoscape, текст сообщений страницы.

Запуск в контейнере базового образа:
  docker run --rm --network host -v /work:/work -e ADMIN_PASSWORD=... \
    --entrypoint /usr/local/bin/python kre44et/kag-base:<tag> /work/kg_page_probe.py admin https://127.0.0.1
"""
import os
import sys

from playwright.sync_api import sync_playwright

USER = sys.argv[1] if len(sys.argv) > 1 else "admin"
BASE = sys.argv[2] if len(sys.argv) > 2 else "https://127.0.0.1"
PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
ENTITY = os.environ.get("ENTITY", "Банк России")
THEME = os.environ.get("THEME", "light")
OUT = os.environ.get("OUT_DIR", "/work")


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 950}, ignore_https_errors=True)
        print("вход:", ctx.request.post(f"{BASE}/api/v1/auth/login",
                                        data={"username": USER, "password": PASSWORD}).status)
        page = ctx.new_page()
        errors: list[str] = []
        bad: list[str] = []
        page.on("pageerror", lambda e: errors.append(f"исключение: {str(e)[:200]}"))
        page.on("console", lambda m: errors.append(f"консоль: {m.text[:200]}") if m.type == "error" else None)
        page.on("response", lambda r: bad.append(f"{r.status} {r.url[:110]}") if r.status >= 400 else None)

        page.goto(f"{BASE}/kg")
        page.wait_for_timeout(2500)
        page.evaluate("(t) => document.documentElement.setAttribute('data-theme', t)", THEME)
        page.wait_for_timeout(500)

        # строим граф вокруг сущности так, как это делает пользователь
        page.fill("#kg-node", ENTITY)
        page.click("text=Показать")
        page.wait_for_timeout(6000)

        state = page.evaluate("""() => {
            const msg = document.getElementById('kg-msg');
            const hill = document.querySelector('#cy canvas');
            return {
                msg: msg ? msg.textContent.trim() : '(нет блока сообщения)',
                nodes: (typeof cy !== 'undefined' && cy) ? cy.nodes().length : -1,
                edges: (typeof cy !== 'undefined' && cy) ? cy.edges().length : -1,
                canvas: !!hill,
                canvas_size: hill ? (Math.round(hill.width) + 'x' + Math.round(hill.height)) : '-',
                theme: document.documentElement.getAttribute('data-theme'),
                node_label_color: (typeof cy !== 'undefined' && cy && cy.nodes().length)
                    ? cy.nodes()[0].style('color') : '-',
                edge_color: (typeof cy !== 'undefined' && cy && cy.edges().length)
                    ? cy.edges()[0].style('line-color') : '-',
            };
        }""")
        page.screenshot(path=f"{OUT}/kg_{THEME}.png")

        print(f"\nсущность: {ENTITY} | тема: {state['theme']}")
        print("сообщение страницы:", state["msg"])
        print(f"в графе: узлов {state['nodes']}, связей {state['edges']}")
        print("холст cytoscape:", state["canvas"], state["canvas_size"])
        print("цвет подписи узла:", state["node_label_color"], "| цвет связи:", state["edge_color"])
        print("\nошибки браузера:", len(errors))
        for e in dict.fromkeys(errors):
            print("  -", e)
        if bad:
            print("запросы с ошибкой:")
            for b in dict.fromkeys(bad):
                print("  -", b)
        browser.close()


if __name__ == "__main__":
    main()
