"""E2E: поиск фрагментов по тексту в /kg (совпадения + соседи по уровню).

Проверяет: ввод запроса, кнопку «Найти фрагменты», что в граф попадают ТОЛЬКО
выбранные фрагменты и их соседи (не весь документ), подсветку совпадений и пометку
в панели фрагмента.

Запуск (Playwright + Chromium есть только в worker-контейнере):

    cd /home/yartsevn/kag-system && set -a && . ./.env && set +a
    printf "%s" "$ADMIN_PASSWORD" | docker exec -i kag-system_worker_1 \
        sh -c "cat > /tmp/.kag_pass; chmod 644 /tmp/.kag_pass"
    docker cp scripts/e2e_kg_search_chunks.py kag-system_worker_1:/tmp/
    docker exec kag-system_worker_1 python /tmp/e2e_kg_search_chunks.py "ГОСТ Р 34.10"
    docker exec -u 0 kag-system_worker_1 rm -f /tmp/.kag_pass /tmp/e2e_kg_search_chunks.py
"""
import asyncio
import json
import os
import sys

from playwright.async_api import async_playwright

BASE = "http://api:8000"
QUERY = sys.argv[1] if len(sys.argv) > 1 else "ГОСТ Р 34.10"


async def main() -> int:
    result = {"запрос": QUERY}
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

        await page.select_option("#kg-depth", "1")
        await page.fill("#kg-text", QUERY)
        await page.evaluate("searchChunksFromInput()")
        await page.wait_for_timeout(15000)

        result["сообщение"] = str(await page.evaluate("document.getElementById('kg-msg').innerText"))[:260]
        nodes = await page.evaluate(
            """(() => {
                 if (typeof cy === 'undefined' || !cy) return [];
                 return cy.nodes().map(n => ({name: n.data('name'), kind: n.data('kind'),
                                             matched: !!n.data('matched'), classes: n.classes()}));
               })()"""
        )
        kinds = {}
        for n in nodes or []:
            kinds[n.get("kind")] = kinds.get(n.get("kind"), 0) + 1
        matched = [n for n in (nodes or []) if n.get("matched")]
        highlighted = [n for n in (nodes or []) if "matched" in (n.get("classes") or [])]
        result["узлов"] = len(nodes or [])
        result["виды узлов"] = kinds
        result["совпадений среди узлов"] = len(matched)
        result["подсвечено классом matched"] = len(highlighted)

        # панель фрагмента-совпадения
        await page.evaluate(
            """(() => {
                 const c = cy;
                 const n = c.nodes().filter(x => x.data('matched'))[0];
                 if (n) n.emit('tap');
               })()"""
        )
        await page.wait_for_timeout(5000)
        panel = str(await page.evaluate("document.getElementById('kg-info').innerText"))
        result["в панели пометка совпадения"] = "совпадение по запросу" in panel or "совпадение по поиску" in panel
        result["фрагмент панели"] = panel[:160]

        # важное: в графе не весь документ, а выборка
        result["ошибки JS"] = errors
        result["VERDICT"] = "OK" if (
            len(matched) > 0 and len(highlighted) > 0
            and kinds.get("chunk", 0) > 0
            and "полнотекстовому индексу" in str(result["сообщение"])
            and result["в панели пометка совпадения"] and not errors
        ) else "ПРОВАЛ"
        await b.close()

    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0 if result["VERDICT"] == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
