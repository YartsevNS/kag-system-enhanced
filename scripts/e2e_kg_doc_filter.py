"""E2E: фильтр графа по документу в /kg.

Проверяет: выбор документа (список имён), построение подграфа документа
(документ + фрагменты + сущности), панель сущности с фрагментами ТОЛЬКО из этого
файла и снятие фильтра.

Запуск (Playwright + Chromium есть только в worker-контейнере):

    cd /home/yartsevn/kag-system && set -a && . ./.env && set +a
    printf "%s" "$ADMIN_PASSWORD" | docker exec -i kag-system_worker_1 \
        sh -c "cat > /tmp/.kag_pass; chmod 644 /tmp/.kag_pass"
    docker cp scripts/e2e_kg_doc_filter.py kag-system_worker_1:/tmp/
    docker exec kag-system_worker_1 python /tmp/e2e_kg_doc_filter.py "Инфляция"
    docker exec -u 0 kag-system_worker_1 rm -f /tmp/.kag_pass /tmp/e2e_kg_doc_filter.py
"""
import asyncio
import json
import os
import sys

from playwright.async_api import async_playwright

BASE = "http://api:8000"
DOC_QUERY = sys.argv[1] if len(sys.argv) > 1 else "Инфляция"


async def main() -> int:
    result = {"фильтр по документу": DOC_QUERY}
    errors = []
    with open("/tmp/.kag_pass", encoding="utf-8") as fh:
        password = os.environ.get("ADMIN_PASSWORD") or fh.read().strip()

    async with async_playwright() as p:
        b = await p.chromium.launch()
        page = await b.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)[:300]))

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

        await page.goto(f"{BASE}/kg", wait_until="domcontentloaded")
        await page.wait_for_timeout(3500)

        # список документов подгружается по фокусу
        await page.click("#kg-doc")
        await page.wait_for_timeout(3000)
        options = await page.evaluate("document.querySelectorAll('#kg-docs option').length")
        result["документов в списке подсказок"] = options

        await page.fill("#kg-doc", DOC_QUERY)
        await page.wait_for_timeout(500)
        await page.evaluate("loadDocGraphFromInput()")
        await page.wait_for_timeout(9000)

        msg = await page.evaluate("document.getElementById('kg-msg').innerText")
        result["сообщение"] = str(msg)[:200]
        nodes = await page.evaluate(
            """(() => {
                 const c = cy;
                 if (!c) return [];
                 return c.nodes().map(n => ({name: n.data('name'), kind: n.data('kind'), label: n.data('label')}));
               })()"""
        )
        kinds = {}
        for n in nodes or []:
            kinds[n.get("kind")] = kinds.get(n.get("kind"), 0) + 1
        result["узлов"] = len(nodes or [])
        result["виды узлов"] = kinds
        result["имя документа в графе"] = [n["name"] for n in (nodes or []) if n.get("kind") == "document"]
        result["пример подписи фрагмента"] = [n["label"] for n in (nodes or []) if n.get("kind") == "chunk"][:1]

        # клик по сущности: панель должна показать фрагменты ТОЛЬКО этого документа
        await page.evaluate(
            """(() => {
                 const c = cy;
                 const ent = c.nodes().filter(n => n.data('kind') === 'entity')[0];
                 if (ent) ent.emit('tap');
               })()"""
        )
        await page.wait_for_timeout(6000)
        panel = str(await page.evaluate("document.getElementById('kg-info').innerText"))
        result["в панели есть ограничение по документу"] = "в документе «" in panel
        result["фрагмент панели"] = panel[:200]

        # снятие фильтра
        await page.evaluate("clearDocFilter()")
        await page.wait_for_timeout(800)
        result["после снятия фильтра"] = str(await page.evaluate("document.getElementById('kg-msg').innerText"))[:80]
        result["ошибки JS"] = errors
        result["VERDICT"] = "OK" if (
            kinds.get("document") == 1 and kinds.get("chunk", 0) > 0 and kinds.get("entity", 0) > 0
            and result["в панели есть ограничение по документу"] and not errors
        ) else "ПРОВАЛ"
        await b.close()

    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0 if result["VERDICT"] == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
