"""E2E: промпт правится из админки на развёрнутом стенде и влияет на чат.

Проверяет именно то, что ожидается от развёрнутого проекта: промпт живёт в
настройках стенда, редактируется в админке, правка сразу влияет на ответы, а
файл репозитория можно подставить обратно кнопкой.

Запуск (Playwright + Chromium есть только в worker-контейнере):

    cd /home/yartsevn/kag-system && set -a && . ./.env && set +a
    printf "%s" "$ADMIN_PASSWORD" | docker exec -i kag-system_worker_1 \
        sh -c "cat > /tmp/.kag_pass; chmod 644 /tmp/.kag_pass"
    docker cp scripts/e2e_admin_prompt_edit.py kag-system_worker_1:/tmp/
    docker exec kag-system_worker_1 python /tmp/e2e_admin_prompt_edit.py
    docker exec -u 0 kag-system_worker_1 rm -f /tmp/.kag_pass /tmp/e2e_admin_prompt_edit.py

Шаги: вход → /admin → строка «Чат» → кнопка «Настроить» → проверка пометки
источника → дописать маркер в промпт → «Сохранить» → проверка, что маркер в
настройках → вопрос в чат → маркер должен быть в ответе → кнопка «Взять из файла
репозитория» + «Сохранить» → проверка, что промпт вернулся к файловому.
"""
import asyncio
import json
import os

from playwright.async_api import async_playwright

BASE = "http://api:8000"
MARKER = "МАРКЕР-ПРОМПТА-ОК"
ADDON = "\n\nВ КОНЦЕ КАЖДОГО ОТВЕТА добавляй отдельной строкой: " + MARKER


async def main() -> int:
    result = {"steps": []}
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

        # админка → функции
        await page.goto(f"{BASE}/admin", wait_until="domcontentloaded")
        await page.wait_for_timeout(6000)
        rows = await page.evaluate(
            "Array.from(document.querySelectorAll('#functions-list tr')).map(r => r.innerText.replace(/\\s+/g,' ').slice(0,120))"
        )
        result["строки в списке функций"] = rows
        chat_row_src = next((r for r in rows if r.startswith(("💬", "Чат"))), "")
        result["источник промпта чата (из списка)"] = "настройки" if "настройки" in chat_row_src else "файл репозитория"

        # открыть настройку чата
        await page.locator('tr:has-text("Настройка чата"), tr:has-text("💬")').first.locator("button").first.click()
        await page.wait_for_timeout(3000)
        before = await page.evaluate(
            "(() => { const t = document.getElementById('fm-prompt');"
            " const s = document.getElementById('fm-prompt-src');"
            " return {len: t ? t.value.length : 0, src: s ? s.innerText : null,"
            " hasMarker: t ? t.value.includes('МАРКЕР-ПРОМПТА-ОК') : null}; })()"
        )
        result["промпт до правки"] = before
        if not before.get("len"):
            result["VERDICT"] = "ПРОВАЛ: промпт в админке пуст"
            await b.close()
            print(json.dumps(result, ensure_ascii=False, indent=1))
            return 1

        # дописать маркер и сохранить
        new_prompt = await page.evaluate(
            "(() => { const t = document.getElementById('fm-prompt'); t.value = t.value + %s; return t.value.length; })()"
            % json.dumps(ADDON, ensure_ascii=False)
        )
        await page.locator('#fm-modal-container button:has-text("Сохранить")').first.click()
        await page.wait_for_timeout(4000)

        saved = await page.evaluate(
            """async () => {
                 const r = await fetch('/api/v1/admin/models/functions/chat', {credentials: 'include'});
                 const d = await r.json();
                 return {len: (d.system_prompt || '').length,
                         from_settings: !!d.prompt_from_settings,
                         has_marker: (d.system_prompt || '').includes('МАРКЕР-ПРОМПТА-ОК'),
                         file: d.prompt_file || null};
               }"""
        )
        result["после сохранения в настройках"] = saved

        # ответ чата должен содержать маркер
        answer = await page.evaluate(
            """async () => {
                 const r = await fetch('/api/v1/chat/', {
                   method: 'POST', credentials: 'include',
                   headers: {'Content-Type': 'application/json'},
                   body: JSON.stringify({messages: [{role: 'user', content: 'Скажи одним предложением, что такое KAG.'}]})
                 });
                 const d = await r.json();
                 return d.response || d.answer || '';
               }"""
        )
        result["маркер в ответе чата"] = MARKER in str(answer)
        result["фрагмент ответа"] = str(answer)[-160:]

        # вернуть промпт из файла репозитория.
        # Модалка после сохранения закрылась — открываем её снова.
        await page.locator('tr:has-text("Настройка чата"), tr:has-text("💬")').first.locator("button").first.click()
        await page.wait_for_timeout(3000)
        await page.locator('#fm-modal-container button:has-text("Взять из файла")').first.click()
        await page.wait_for_timeout(2500)
        restored_len = await page.evaluate(
            "(() => { const t = document.getElementById('fm-prompt'); return t ? t.value.length : 0; })()"
        )
        await page.locator('#fm-modal-container button:has-text("Сохранить")').first.click()
        await page.wait_for_timeout(4000)
        final = await page.evaluate(
            """async () => {
                 const r = await fetch('/api/v1/admin/models/functions/chat', {credentials: 'include'});
                 const d = await r.json();
                 return {len: (d.system_prompt || '').length,
                         has_marker: (d.system_prompt || '').includes('МАРКЕР-ПРОМПТА-ОК')};
               }"""
        )
        result["после возврата из файла"] = {"в редакторе": restored_len, **final}
        result["ошибки JS"] = errors
        result["VERDICT"] = "OK" if (
            result["маркер в ответе чата"]
            and saved.get("has_marker") and saved.get("from_settings")
            and not final.get("has_marker")
            and restored_len == before.get("len")
            and not errors
        ) else "ПРОВАЛ"
        await b.close()

    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0 if result["VERDICT"] == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
