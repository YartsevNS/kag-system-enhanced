"""E2E: страница «Опыты и модели» — только для админа, данные из настроек.

Проверяет:
1) пункт «Опыты и модели» появляется в навигации админа (branding.js читает /auth/me);
2) сама страница открывается и рисует таблицу с нашими цифрами (из config_store);
3) без входа страница закрыта (302 на /documents).

Запуск (Playwright есть в worker-контейнере):
    cd /home/yartsevn/kag-system && set -a && . ./.env && set +a
    printf "%s" "$ADMIN_PASSWORD" | docker exec -i kag-system_worker_1 \
        sh -c "cat > /tmp/.kag_pass; chmod 644 /tmp/.kag_pass"
    docker cp scripts/e2e_experiments_page.py kag-system_worker_1:/tmp/
    docker exec kag-system_worker_1 python /tmp/e2e_experiments_page.py
    docker exec -u 0 kag-system_worker_1 rm -f /tmp/.kag_pass /tmp/e2e_experiments_page.py
"""
import asyncio
import json
import os
import urllib.request

from playwright.async_api import async_playwright

BASE = "http://api:8000"


async def main() -> int:
    res, errors = {}, []
    with open("/tmp/.kag_pass", encoding="utf-8") as fh:
        password = os.environ.get("ADMIN_PASSWORD") or fh.read().strip()

    # 1. Без входа страница закрыта. Редирект ведёт на /login, а urllib его проходит,
    # поэтому смотрим и код, и итоговый адрес: защита сработала, если нас увели на вход
    # (или сразу отдали 401/403).
    try:
        req = urllib.request.Request(BASE + "/experiments")
        opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler)
        with opener.open(req, timeout=30) as r:
            res["без входа код"] = r.status
            res["без входа адрес"] = r.url
    except urllib.error.HTTPError as e:
        res["без входа код"] = e.code
    except Exception as e:
        res["без входа ошибка"] = f"{type(e).__name__}: {str(e)[:80]}"

    res["доступ закрыт"] = (
        res.get("без входа код") in (302, 401, 403)
        or "/login" in str(res.get("без входа адрес", ""))
    )

    async with async_playwright() as p:
        b = await p.chromium.launch()
        page = await b.new_page(viewport={"width": 1500, "height": 1000})
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

        # 2. Ссылка в навигации появляется у админа
        await page.goto(f"{BASE}/admin", wait_until="domcontentloaded")
        await page.wait_for_timeout(2500)
        res["ссылка в меню"] = await page.evaluate(
            "!!document.querySelector('nav a[href=\"/experiments\"]')")

        # 3. Страница открывается и рисует данные
        await page.goto(f"{BASE}/experiments", wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        res["адрес после перехода"] = page.url
        rows = await page.evaluate(
            "Array.from(document.querySelectorAll('#emb-table tr')).map(tr => "
            "Array.from(tr.querySelectorAll('td')).map(td => td.innerText.trim()))")
        res["строк в таблице"] = len(rows)
        res["первая строка"] = rows[0] if rows else None
        body = await page.evaluate("document.body.innerText")
        res["есть цифра эталона 0.550"] = "0.550" in body
        res["есть статус замера"] = "замер идёт" in body
        res["есть раздел GPU"] = "GPU" in body
        await page.screenshot(path="/tmp/experiments_page.png", full_page=True)

        res["ошибки JS"] = errors
        res["VERDICT"] = "OK" if (
            res.get("доступ закрыт") and res["ссылка в меню"]
            and res["строк в таблице"] >= 5 and res["есть цифра эталона 0.550"]
            and not errors
        ) else "ПРОВАЛ"
        await b.close()

    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0 if res["VERDICT"] == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
