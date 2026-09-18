"""E2E: переход из /kg к фрагменту и странице документа.

Проверяет то, ради чего правка делалась:
1) ссылка в панели ведёт в просмотрщик с номером страницы фрагмента (`&page=`)
   и терминами запроса (`&q=`), а не открывается через window.open;
2) просмотрщик открывается на этой странице и подсвечивает термины в текстовом слое;
3) в панели фрагмента видно, какой это фрагмент — совпадение или соседний;
4) в панели сущности фрагменты тоже дают переход на свою страницу.

Запуск (Playwright + Chromium есть только в worker-контейнере):

    cd /home/yartsevn/kag-system && set -a && . ./.env && set +a
    printf "%s" "$ADMIN_PASSWORD" | docker exec -i kag-system_worker_1 \\
        sh -c "cat > /tmp/.kag_pass; chmod 644 /tmp/.kag_pass"
    docker cp scripts/e2e_kg_fragment_nav.py kag-system_worker_1:/tmp/
    docker exec kag-system_worker_1 python /tmp/e2e_kg_fragment_nav.py
    docker exec -u 0 kag-system_worker_1 rm -f /tmp/.kag_pass /tmp/e2e_kg_fragment_nav.py
"""
import asyncio
import html
import json
import os
import re

from playwright.async_api import async_playwright

BASE = "http://api:8000"
QUERY = "цифровой рубль"
ENTITY = "Банк России"


async def login(page) -> None:
    with open("/tmp/.kag_pass", encoding="utf-8") as fh:
        password = os.environ.get("ADMIN_PASSWORD") or fh.read().strip()
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


async def main() -> int:
    res = {"запрос": QUERY, "сущность": ENTITY}
    errors = []
    async with async_playwright() as p:
        b = await p.chromium.launch()
        ctx = await b.new_context(viewport={"width": 1500, "height": 1000})
        page = await ctx.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)[:300]))
        await login(page)

        # ── 1. поиск по тексту: клик по найденному фрагменту ───────────────
        await page.goto(f"{BASE}/kg", wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        await page.click('.kg-mode[data-mode="text"]')
        await page.select_option("#kg-depth", "1")
        await page.fill("#kg-text", QUERY)
        await page.evaluate("searchChunksFromInput()")
        await page.wait_for_timeout(15000)

        # берём совпадение из PDF (у pdf есть текстовый слой — там проверяем подсветку)
        picked = await page.evaluate(
            """(() => {
                 const ns = cy.nodes().filter(n => n.data('matched') && n.data('kind') === 'chunk');
                 const pdf = ns.filter(n => String(n.data('filename') || '').toLowerCase().endsWith('.pdf'))[0] || ns[0];
                 if (!pdf) return null;
                 pdf.emit('tap');
                 return {name: pdf.data('name'), page: pdf.data('page'), doc: pdf.data('document_id'),
                         chunk: pdf.data('chunk_id'), matched: !!pdf.data('matched')};
               })()"""
        )
        res["выбранный фрагмент"] = picked
        await page.wait_for_timeout(4000)
        panel_html = str(await page.evaluate("document.getElementById('kg-info').innerHTML"))
        panel_text = re.sub(r"<[^>]+>", " ", panel_html)
        # href в HTML экранирован (&amp;) — при клике браузер декодирует сам,
        # а в тесте строку надо раскодировать, иначе &amp;page=27 не станет page=27.
        links = [html.unescape(x) for x in re.findall(r'href="([^"]*viewer\?[^"]*)"', panel_html)]
        res["ссылки в панели"] = links[:3]
        res["пометка совпадения в панели"] = "совпадение по запросу" in panel_text
        res["в панели есть подсветка (mark)"] = "<mark" in panel_html

        viewer_href = links[0] if links else ""
        res["ссылка с page"] = bool(viewer_href and "page=" in viewer_href)
        res["ссылка с q"] = bool(viewer_href and "q=" in viewer_href)

        # ── 2. просмотрщик: та же страница + подсветка терминов ────────────
        if viewer_href:
            vp = await ctx.new_page()
            vp.on("pageerror", lambda e: errors.append("viewer: " + str(e)[:200]))
            await vp.goto(BASE + viewer_href, wait_until="domcontentloaded")
            await vp.wait_for_timeout(9000)
            res["страница в просмотрщике"] = await vp.evaluate(
                "document.getElementById('page-num') ? document.getElementById('page-num').value : null")
            res["подсвечено спанов"] = await vp.evaluate(
                "document.querySelectorAll('.textLayer span.hl-term').length")
            res["подпись над страницей"] = str(await vp.evaluate(
                "(document.getElementById('hl-note') || {}).innerText || ''"))[:120]
            await vp.screenshot(path="/tmp/kg_fragment_viewer.png")
            await vp.close()

        # ── 3. панель сущности: у фрагментов тоже есть переход на страницу ─
        await page.click('.kg-mode[data-mode="entity"]')
        await page.fill("#kg-node", ENTITY)
        await page.evaluate("loadGraphFromInput()")
        await page.wait_for_timeout(12000)
        ent = await page.evaluate(
            """(() => {
                 const n = cy.nodes().filter(x => x.data('kind') === 'entity' && x.data('name') === %s)[0];
                 if (n) n.emit('tap');
                 return !!n;
               })()""" % json.dumps(ENTITY, ensure_ascii=False)
        )
        res["узел сущности найден"] = ent
        await page.wait_for_timeout(6000)
        ent_html = str(await page.evaluate("document.getElementById('kg-info').innerHTML"))
        ent_links = re.findall(r'href="([^"]*viewer\?[^"]*)"', ent_html)
        res["переходов на страницу в панели сущности"] = len(ent_links)
        res["пример ссылки"] = ent_links[0] if ent_links else ""
        await page.screenshot(path="/tmp/kg_fragment_panel.png")

        res["ошибки JS"] = errors
        res["VERDICT"] = "OK" if (
            res["ссылка с page"]
            and res["ссылка с q"]
            and res["пометка совпадения в панели"]
            and (res.get("подсвечено спанов") or 0) > 0
            and res["переходов на страницу в панели сущности"] > 0
            and not errors
        ) else "ПРОВАЛ"
        await b.close()

    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0 if res["VERDICT"] == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
