"""E2E: визуализация графа /kg — фрагменты сущности, текст чанка, переходы к файлам.

Проверяет то, что просил пользователь: по клику на узел видеть не только соседей,
но и фрагменты (текст из Qdrant с номером страницы), и открывать документ; а имена
узлов должны быть человекочитаемыми (имя файла и номер фрагмента, а не «b93f09…»).

Запуск (Playwright + Chromium есть только в worker-контейнере):

    cd /home/yartsevn/kag-system && set -a && . ./.env && set +a
    printf "%s" "$ADMIN_PASSWORD" | docker exec -i kag-system_worker_1 \
        sh -c "cat > /tmp/.kag_pass; chmod 644 /tmp/.kag_pass"
    docker cp scripts/e2e_kg_viz.py kag-system_worker_1:/tmp/
    docker exec kag-system_worker_1 python /tmp/e2e_kg_viz.py "Банк России"
    docker exec -u 0 kag-system_worker_1 rm -f /tmp/.kag_pass /tmp/e2e_kg_viz.py
"""
import asyncio
import json
import os
import re
import sys

from playwright.async_api import async_playwright

BASE = "http://api:8000"
ENTITY = sys.argv[1] if len(sys.argv) > 1 else "Банк России"
UUID_TAIL = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


async def main() -> int:
    result = {"entity": ENTITY}
    errors = []
    with open("/tmp/.kag_pass", encoding="utf-8") as fh:
        password = os.environ.get("ADMIN_PASSWORD") or fh.read().strip()

    async with async_playwright() as p:
        b = await p.chromium.launch()
        page = await b.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)[:300]))

        # вход
        await page.goto(f"{BASE}/login", wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)
        link = await page.query_selector("#local-link")
        if link and await link.is_visible():
            await link.click()
            await page.wait_for_timeout(1200)
        await page.fill("#username", os.environ.get("KAG_USER", "admin"))
        await page.fill("#password", password)
        await page.click("button.btn")
        await page.wait_for_timeout(4000)

        # граф
        await page.goto(f"{BASE}/kg", wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)
        await page.fill("#kg-node", ENTITY)
        await page.evaluate("loadGraphFromInput()")
        await page.wait_for_timeout(7000)

        nodes = await page.evaluate(
            """(() => {
                 const c = (typeof cy !== 'undefined' && cy) ? cy : null;
                 if (!c) return null;
                 return c.nodes().map(n => ({name: n.data('name'), label: n.data('label'),
                                            kind: n.data('kind'), type: n.data('type')}));
               })()"""
        )
        result["узлов в графе"] = len(nodes or [])
        result["виды узлов"] = sorted({(n or {}).get("kind") for n in (nodes or [])})
        raw = [n["name"] for n in (nodes or []) if n.get("name") and UUID_TAIL.match(str(n["name"]))]
        result["имён с сырым id"] = raw[:5]
        result["примеры имён"] = [n["label"] for n in (nodes or [])[:6]]

        # клик по сущности → панель с фрагментами
        await page.evaluate(
            """(() => {
                 const c = cy;
                 const node = c.nodes().filter(n => n.data('kind') === 'entity')[0] || c.nodes()[0];
                 node.emit('tap');
               })()"""
        )
        await page.wait_for_timeout(5000)
        panel = await page.evaluate("document.getElementById('kg-info').innerText")
        result["панель сущности"] = str(panel)[:220]
        result["есть список фрагментов"] = "Фрагменты, где встречается сущность" in str(panel)
        result["фрагментов показано"] = str(panel).count("фрагмент ")
        result["есть кнопка открытия файла"] = "открыть на странице" in str(panel)

        # клик по чанку (если он есть в графе на depth=2) → текст фрагмента
        chunk_present = await page.evaluate(
            "(() => { const c = cy; return c ? c.nodes().filter(n => n.data('kind') === 'chunk').length : 0; })()"
        )
        result["узлов-фрагментов в графе"] = chunk_present
        if chunk_present:
            await page.evaluate(
                """(() => {
                     const c = cy;
                     const node = c.nodes().filter(n => n.data('kind') === 'chunk')[0];
                     node.emit('tap');
                   })()"""
            )
            await page.wait_for_timeout(4000)
            panel2 = await page.evaluate("document.getElementById('kg-info').innerText")
            result["панель фрагмента"] = str(panel2)[:200]
            result["текст фрагмента показан"] = len(str(panel2)) > 60

        # ссылка на просмотрщик формируется корректно?
        link_ok = await page.evaluate(
            """(() => {
                 const html = document.getElementById('kg-info').innerHTML;
                 const m = html.match(/openDoc\\('([0-9a-f-]{36})'/);
                 return m ? m[1] : null;
               })()"""
        )
        result["id документа для перехода"] = link_ok
        result["ошибки JS"] = errors
        result["VERDICT"] = "OK" if (
            result["есть список фрагментов"] and result["есть кнопка открытия файла"]
            and not raw and not errors
        ) else "ПРОВАЛ"
        await b.close()

    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0 if result["VERDICT"] == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
