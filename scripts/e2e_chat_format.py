"""E2E: ответы в чате показываются без markdown-мусора (** и ##).

Зачем: модель отвечает в markdown, а чат выводил ответ сырым текстом — в
ответах были видны «**жирный**» и «## Заголовок». Промпт просят писать без
разметки, но это срабатывает не всегда, поэтому разметку разбирает интерфейс
(formatMessageText в chat.html). Этот скрипт проверяет результат в браузере.

Запуск (Playwright + Chromium есть только в worker-контейнере):

    cd /home/yartsevn/kag-system && set -a && . ./.env && set +a
    printf "%s" "$ADMIN_PASSWORD" | docker exec -i kag-system_worker_1 \
        sh -c "cat > /tmp/.kag_pass; chmod 644 /tmp/.kag_pass"
    docker cp scripts/e2e_chat_format.py kag-system_worker_1:/tmp/
    docker exec kag-system_worker_1 python /tmp/e2e_chat_format.py
    docker exec -u 0 kag-system_worker_1 rm -f /tmp/.kag_pass /tmp/e2e_chat_format.py

Что проверяется: вход (через ссылку «🔑 Локальный вход»), отправка вопроса в
чат, ожидание ответа, затем разбор innerHTML последнего .message-text:
нет литералов ** и ##, есть ли структура (<strong>/<div class="msg-h">/<ul>),
нет ошибок JS. Вопрос выбран такой, на котором модель обычно ставит заголовок.
"""
import asyncio
import json
import os
import re
import sys

from playwright.async_api import async_playwright

BASE = "http://api:8000"
QUESTION = os.environ.get(
    "E2E_QUESTION",
    "Какие требования к защите персональных данных по ГОСТ Р 57580.1-2017?",
)


async def main() -> int:
    result = {}
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

        await page.goto(f"{BASE}/chat", wait_until="domcontentloaded")
        await page.wait_for_timeout(2500)
        await page.fill("#message-input", QUESTION)
        await page.click("#send-btn")

        # ждём ответ (модель отвечает 10-40 с)
        html = ""
        for _ in range(30):
            await page.wait_for_timeout(3000)
            blocks = await page.evaluate(
                "Array.from(document.querySelectorAll('.message-text')).map(e => e.innerHTML)"
            )
            if blocks and len(blocks) >= 2 and len(blocks[-1]) > 200:
                html = blocks[-1]
                break

        text = re.sub(r"<[^>]+>", "", html)
        answer = await page.evaluate(
            "(() => { const els = document.querySelectorAll('.message-text');"
            " return els.length ? els[els.length - 1].innerText : ''; })()"
        )
        result["вопрос"] = QUESTION
        result["длина ответа"] = len(str(answer))
        result["литералы ** в тексте"] = str(answer).count("**")
        result["заголовки ## в тексте"] = len(re.findall(r"^#{1,6}\s", str(answer), re.M))
        result["есть <strong>"] = "<strong>" in html
        result["есть блок заголовка"] = 'class="msg-h"' in html
        result["есть списки"] = ("<ul>" in html or "<ol>" in html)
        result["ошибки JS"] = errors
        result["фрагмент ответа"] = str(answer)[:200]
        result["VERDICT"] = "OK" if (
            result["длина ответа"] > 100
            and result["литералы ** в тексте"] == 0
            and result["заголовки ## в тексте"] == 0
            and not errors
        ) else "ПРОВАЛ"
        await b.close()

    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0 if result["VERDICT"] == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
