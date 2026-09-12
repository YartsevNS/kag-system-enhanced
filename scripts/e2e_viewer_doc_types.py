"""E2E: в модалке «Изменить документ» можно добавить тип документа (viewer).

Зачем: список типов в модалке раньше был зашит в код viewer.html, добавить тип
было негде. Теперь он берётся из настроек (админка → «Типы документов»,
GET/POST /api/v1/admin/models/doc-types) и у администратора есть пункт
«＋ Добавить тип…». Этот скрипт проверяет весь путь в живом браузере.

Запуск (Playwright + Chromium есть только в worker-контейнере):

    cd /home/yartsevn/kag-system && set -a && . ./.env && set +a
    # пароль отдаём через stdin, чтобы значение не светилось в ps на хосте:
    printf "%s" "$ADMIN_PASSWORD" | docker exec -i kag-system_worker_1 \
        sh -c "cat > /tmp/.kag_pass; chmod 644 /tmp/.kag_pass"
    docker cp scripts/e2e_viewer_doc_types.py kag-system_worker_1:/tmp/
    docker exec kag-system_worker_1 python /tmp/e2e_viewer_doc_types.py <document_id>
    docker exec -u 0 kag-system_worker_1 rm -f /tmp/.kag_pass /tmp/e2e_viewer_doc_types.py

Что проверяется:
1) вход через /login (сначала по ссылке «🔑 Локальный вход» — по умолчанию
   страница уходит в SSO/Keycloak, и форма отдаёт «Invalid user credentials»);
2) /viewer?id=<document_id> → кнопка «Изменить» появилась (значит сессия жива);
3) в селекте типов есть пункт «＋ Добавить тип…» (только у админа);
4) выбор пункта открывает строку ввода;
5) ввод типа + «Добавить» → тип появляется в селекте, подпись как ввели, ключ
   в нижнем регистре, поле выбрано;
6) тип действительно сохранён в настройках (GET doc-types);
7) уборка: тестовый тип удаляется, документ при этом НЕ сохраняется (то есть
   его тип в БД остаётся прежним — проверять отдельно через /details).

Ожидаемый вывод в конце: "VERDICT": "OK" и пустой список «ошибки JS на странице».
"""
import asyncio
import json
import os
import sys

from playwright.async_api import async_playwright

BASE = "http://api:8000"
DOC_ID = sys.argv[1] if len(sys.argv) > 1 else ""
NEW_TYPE = "Проверка типа E2E"


async def main() -> int:
    if not DOC_ID:
        print("нужен document_id: python e2e_viewer_doc_types.py <document_id>")
        return 2
    result = {"steps": []}
    errors = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)[:300]))

        # 1. вход
        password = os.environ.get("ADMIN_PASSWORD")
        if not password:
            with open("/tmp/.kag_pass", encoding="utf-8") as fh:
                password = fh.read().strip()
        await page.goto(f"{BASE}/login", wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)
        local_link = await page.query_selector("#local-link")
        if local_link and await local_link.is_visible():
            await local_link.click()
            await page.wait_for_timeout(1200)
        await page.fill("#username", os.environ.get("KAG_USER", "admin"))
        await page.fill("#password", password)
        await page.click("button.btn")
        await page.wait_for_timeout(4000)
        result["steps"].append({"вход": page.url})

        # 2. просмотрщик
        await page.goto(f"{BASE}/viewer?id={DOC_ID}", wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)
        edit_visible = await page.evaluate(
            "(() => { const b = document.getElementById('btn-edit'); return !!b && b.style.display !== 'none'; })()"
        )
        result["steps"].append({"кнопка Изменить видна": edit_visible})
        if not edit_visible:
            result["VERDICT"] = "НЕ ДОШЛИ до кнопки — скорее всего вход не удался"
            await browser.close()
            print(json.dumps(result, ensure_ascii=False))
            return 1

        # 3. модалка: есть ли пункт добавления типа
        await page.click("#btn-edit")
        await page.wait_for_timeout(2500)
        before = await page.evaluate(
            "(() => { const s = document.getElementById('edit-type');"
            " return {options: s.options.length, has_add: Array.from(s.options).some(o => o.value === '__add__')}; })()"
        )
        result["steps"].append({"до добавления": before})

        # 4. выбрать «＋ Добавить тип…»
        await page.select_option("#edit-type", "__add__")
        await page.wait_for_timeout(300)
        row_shown = await page.evaluate(
            "(() => { const r = document.getElementById('edit-type-add');"
            " return !!r && getComputedStyle(r).display !== 'none'; })()"
        )
        result["steps"].append({"строка ввода открылась": row_shown})

        # 5. ввести и добавить
        await page.fill("#edit-type-new", NEW_TYPE)
        await page.click("#edit-type-add button")
        await page.wait_for_timeout(3500)
        after = await page.evaluate(
            "(() => { const s = document.getElementById('edit-type');"
            " return {selected: s.value, label: s.options[s.selectedIndex] ? s.options[s.selectedIndex].text : null,"
            " options: s.options.length, status: document.getElementById('edit-type-status').textContent}; })()"
        )
        result["steps"].append({"после добавления": after})

        # 6. тип реально в настройках
        saved = await page.evaluate(
            """async () => {
                 const r = await fetch('/api/v1/admin/models/doc-types', {credentials: 'include'});
                 const d = await r.json();
                 return (d.types || []).map(t => typeof t === 'string' ? t : (t.key + '=' + t.label));
               }"""
        )
        result["steps"].append({"типы в настройках": saved})

        # 7. уборка — удаляем тестовый тип, состояние стенда не меняем
        cleanup = await page.evaluate(
            """async () => {
                 const r = await fetch('/api/v1/admin/models/doc-types', {
                   method: 'POST', credentials: 'include',
                   headers: {'Content-Type': 'application/json'},
                   body: JSON.stringify({action: 'remove', name: 'проверка типа e2e'})
                 });
                 return r.status;
               }"""
        )
        result["steps"].append({"уборка (статус)": cleanup})
        result["ошибки JS на странице"] = errors
        result["VERDICT"] = "OK" if (before.get("has_add") and row_shown
                                     and after.get("selected") == NEW_TYPE.lower()) else "ПРОВАЛ"
        await browser.close()
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0 if result["VERDICT"] == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
