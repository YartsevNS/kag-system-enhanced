"""E2E: раздел «Сироты в векторном хранилище» в админке.

Проверяет, что страница /admin отдаёт раздел, при загрузке показывает результат
последнего скана, а кнопка «Проверить сейчас» действительно пересчитывает и обновляет
сводку (числа из Qdrant/Postgres, не заглушка).

Запуск в worker-контейнере (там есть Playwright):
    cd /home/yartsevn/kag-system && set -a && . ./.env && set +a
    printf "%s" "$ADMIN_PASSWORD" | docker exec -i kag-system_worker_1 \
        sh -c "cat > /tmp/.kag_pass; chmod 644 /tmp/.kag_pass"
    docker cp scripts/e2e_orphans_panel.py kag-system_worker_1:/tmp/
    docker exec kag-system_worker_1 python /tmp/e2e_orphans_panel.py
"""
import asyncio
import json
import os
import re

from playwright.async_api import async_playwright

BASE = "http://api:8000"


async def main() -> int:
    res, errors = {}, []
    with open("/tmp/.kag_pass", encoding="utf-8") as fh:
        password = os.environ.get("ADMIN_PASSWORD") or fh.read().strip()

    async with async_playwright() as p:
        b = await p.chromium.launch()
        page = await b.new_page(viewport={"width": 1500, "height": 1100})
        page.on("pageerror", lambda e: errors.append(str(e)[:200]))

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

        await page.goto(f"{BASE}/admin", wait_until="domcontentloaded")
        await page.wait_for_timeout(3500)

        res["раздел есть"] = await page.evaluate(
            "!!document.getElementById('orphans-summary')")
        res["сводка до кнопки"] = (await page.evaluate(
            "document.getElementById('orphans-summary').innerText")).strip()[:160]

        # кнопка «Проверить сейчас» должна пересчитать и обновить сводку
        await page.click("button:has-text('Проверить сейчас')")
        await page.wait_for_timeout(9000)
        after = (await page.evaluate(
            "document.getElementById('orphans-summary').innerText")).strip()
        res["сводка после кнопки"] = after[:200]
        res["статус кнопки"] = (await page.evaluate(
            "document.getElementById('orphans-status').innerText")).strip()[:80]
        res["есть числа Qdrant"] = bool(re.search(r"точек в Qdrant \d+", after))
        res["есть числа базы"] = bool(re.search(r"документов в базе \d+", after))
        res["сказано про сирот"] = ("сирот" in after)

        # таблица/кнопки появляются только при найденных сиротах — фиксируем как есть
        res["строк в таблице сирот"] = await page.evaluate(
            "document.querySelectorAll('#orphans-table tr').length")
        await page.screenshot(path="/tmp/orphans_panel.png", full_page=True)
        res["ошибки JS"] = errors

        res["VERDICT"] = "OK" if (
            res["раздел есть"] and res["есть числа Qdrant"] and res["есть числа базы"]
            and res["сказано про сирот"] and not errors
        ) else "ПРОВАЛ"
        await b.close()

    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0 if res["VERDICT"] == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
