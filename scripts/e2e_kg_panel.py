"""E2E панели /kg: режимы, поле, кнопка «Показать», чип фильтра + скриншоты.

Проверяет, что после переделки панели ничего не отвалилось: клик по режиму
переключает нужное поле, «Показать» запускает действие режима, вторичные кнопки
на месте, фильтр по документу показывается чипом и снимается крестиком.

Запуск (Playwright + Chromium есть только в worker-контейнере):

    cd /home/yartsevn/kag-system && set -a && . ./.env && set +a
    printf "%s" "$ADMIN_PASSWORD" | docker exec -i kag-system_worker_1 \
        sh -c "cat > /tmp/.kag_pass; chmod 644 /tmp/.kag_pass"
    docker cp scripts/e2e_kg_panel.py kag-system_worker_1:/tmp/
    docker exec kag-system_worker_1 python /tmp/e2e_kg_panel.py
    docker cp kag-system_worker_1:/tmp/kg_panel_entity.png .
"""
import asyncio
import json
import os

from playwright.async_api import async_playwright

BASE = "http://api:8000"
OUT = "/tmp"

CHECK = """(() => {
  const vis = (sel) => { const e = document.querySelector(sel); if (!e) return null;
    const r = e.getBoundingClientRect(); const cs = getComputedStyle(e);
    return {display: cs.display, w: Math.round(r.width), x: Math.round(r.x)}; };
  return {mode: document.getElementById('kg-tools').className,
          node: vis('#kg-node'), doc: vis('#kg-doc'), text: vis('#kg-text'),
          level: vis('.kg-level'), hint: document.getElementById('kg-hint').textContent.slice(0, 60),
          nodes: (typeof cy !== 'undefined' && cy) ? cy.nodes().length : 0,
          msg: document.getElementById('kg-msg').innerText.slice(0, 120)};
})()"""


async def main() -> int:
    result = {"шаги": []}
    errors = []
    with open("/tmp/.kag_pass", encoding="utf-8") as fh:
        password = os.environ.get("ADMIN_PASSWORD") or fh.read().strip()

    async with async_playwright() as p:
        b = await p.chromium.launch()
        page = await b.new_page(viewport={"width": 1440, "height": 900})
        page.on("pageerror", lambda e: errors.append(str(e)[:300]))

        await page.goto(f"{BASE}/login", wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)
        link = await page.query_selector("#local-link")
        if link and await link.is_visible():
            await link.click()
            await page.wait_for_timeout(1200)
        await page.fill("#username", "admin")
        await page.fill("#password", password)
        await page.click("button.btn")
        await page.wait_for_timeout(4000)

        await page.goto(f"{BASE}/kg", wait_until="domcontentloaded")
        await page.wait_for_timeout(3500)
        result["шаги"].append({"стартовый режим": await page.evaluate(CHECK)})
        await page.screenshot(path=f"{OUT}/kg_panel_entity.png")

        # режим «Документ»: клик по пилюле, поле документа, уровень скрыт
        await page.click('.kg-mode[data-mode="document"]')
        await page.wait_for_timeout(2500)
        st = await page.evaluate(CHECK)
        result["шаги"].append({"после клика «Документ»": st})
        result["поле документа показано"] = bool(st["doc"] and st["doc"]["display"] != "none")
        result["поле сущности скрыто"] = bool(st["node"] and st["node"]["display"] == "none")
        result["уровень скрыт"] = bool(st["level"] and st["level"]["display"] == "none")

        # построить граф документа через кнопку «Показать»
        await page.fill("#kg-doc", "Инфляция")
        await page.click('.kg-row .btn')
        await page.wait_for_timeout(9000)
        st = await page.evaluate(CHECK)
        result["шаги"].append({"граф документа": st})
        chip = await page.evaluate("document.getElementById('kg-chip').innerText")
        result["чип фильтра"] = str(chip)
        await page.screenshot(path=f"{OUT}/kg_panel_document.png")

        # режим «Фрагменты по тексту»: сначала с активным фильтром по документу
        await page.click('.kg-mode[data-mode="text"]')
        await page.wait_for_timeout(600)
        await page.select_option("#kg-depth", "1")
        await page.fill("#kg-text", "ГОСТ Р 34.10")
        await page.click('.kg-row .btn')
        await page.wait_for_timeout(16000)
        st_filtered = await page.evaluate(CHECK)
        result["шаги"].append({"поиск с фильтром по документу": st_filtered})
        result["поиск ограничен документом"] = "в выбранном документе" in st_filtered.get("msg", "")

        # снимаем фильтр крестиком в чипе и ищем по всей базе
        await page.evaluate("clearDocFilter()")
        await page.wait_for_timeout(600)
        await page.click('.kg-row .btn')
        await page.wait_for_timeout(18000)
        st = await page.evaluate(CHECK)
        result["шаги"].append({"поиск по всей базе": st})
        result["совпадений подсвечено"] = await page.evaluate(
            "(() => (typeof cy !== 'undefined' && cy) ? cy.nodes('.matched').length : 0)()"
        )
        await page.screenshot(path=f"{OUT}/kg_panel_text.png")

        # чип после снятия фильтра (крестиком выше)
        result["чип после снятия"] = str(await page.evaluate("document.getElementById('kg-chip').innerText"))

        result["ошибки JS"] = errors
        ok = (result["поле документа показано"] and result["поле сущности скрыто"]
              and result["уровень скрыт"] and st["nodes"] > 100
              and result["совпадений подсвечено"] > 0 and not errors
              and result["поиск ограничен документом"]
              and "Инфляция" in str(result["чип фильтра"]))
        result["VERDICT"] = "OK" if ok else "ПРОВАЛ"
        await b.close()

    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0 if result["VERDICT"] == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
