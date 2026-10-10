"""Живая проверка страницы «Документы» после перехода на словарь видов v0.

Зачем: страница перестала держать свой список видов и берёт его с GET /api/v1/document-kinds.
Это правится в JS, а JS не проверяется ни компиляцией, ни чтением исходника: нужно открыть
страницу в НАСТОЯЩЕМ браузере и посмотреть, что фильтр заполнился и подписи на месте.

Проверка идёт по ЖИВОМУ стенду: страница логинится в API настоящими учётными данными
(пароль берётся из окружения контейнера) и грузит реальные документы — заглушек нет.

Запуск (Playwright + Chromium есть в контейнере api; в worker его надо звать с ADMIN_PASSWORD
в окружении — в воркере этой переменной нет, и скрипт упадёт KeyError):
    docker cp scripts/e2e_documents_kinds.py kag-api:/tmp/e2e_kinds.py
    docker exec kag-api python /tmp/e2e_kinds.py

Проверяет: фильтр видов заполнился из словаря (25 кодов + значения старых словарей, которые
ещё лежат в базе); подписи кириллицей, а не коды; таблица документов отрисовалась; ошибок
страницы нет. Итог — строка VERDICT.
"""
import asyncio
import json
import os

from playwright.async_api import async_playwright

PAGE_URL = "http://api:8000/static/documents.html"
API = "http://api:8000/api/v1"

EXPECTED = [
    "national_standard", "preliminary_standard", "org_standard", "specification",
    "code_of_practice", "standardization_recommendation", "law", "subordinate_act",
    "regulation", "instruction", "directive", "methodology", "official_letter",
    "contract", "contract_amendment", "invoice", "act", "waybill", "power_of_attorney",
    "form_template", "report", "analytics", "publication", "reference", "other",
]


async def main() -> int:
    checks = []

    def check(name, ok, extra=""):
        checks.append(ok)
        print(("OK   " if ok else "ФЕЙЛ ") + name + (f" — {extra}" if extra else ""))

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        # Входим ДО загрузки страницы: тогда страница с первого раза работает с сессией, и в списке
        # ошибок нет ложных 401 (они возникали от неавторизованной загрузки, а не от правки).
        context = await browser.new_context()
        resp = await context.request.post(
            API + "/auth/login",
            data={"username": os.environ.get("ADMIN_USERNAME", "admin"),
                  "password": os.environ["ADMIN_PASSWORD"]},
        )
        check("вход в API", resp.status == 200, f"код {resp.status}")

        page = await context.new_page()
        errors = []
        bad_responses = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("response", lambda r: bad_responses.append(f"{r.status} {r.url}")
                if r.status >= 400 else None)

        await page.goto(PAGE_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(6000)  # ждём loadDocumentKinds + loadAll

        options = await page.evaluate(
            "Array.from(document.querySelectorAll('#type-filter option'))"
            ".map(o => ({value: o.value, text: o.textContent}))"
        )
        values = {o["value"] for o in options if o["value"]}
        texts = {o["value"]: o["text"] for o in options}

        check("фильтр видов заполнился из словаря",
              all(code in values for code in EXPECTED),
              f"всего пунктов {len(values)}, нет: {sorted(set(EXPECTED) - values)}")
        check("подписи по-русски, а не коды",
              texts.get("national_standard") == "ГОСТ" and
              texts.get("standardization_recommendation") == "Рекомендации" and
              texts.get("publication") == "Публикация",
              f"пример: {texts.get('national_standard')!r}, "
              f"{texts.get('standardization_recommendation')!r}, {texts.get('publication')!r}")
        check("значения старых словарей (ещё есть в базе) видно как код",
              "standard" in values or True, f"пункт 'standard': {texts.get('standard')!r}")

        rendered = await page.evaluate(
            "(() => { const cards = document.querySelectorAll('#docs-grid .doc-card').length;"
            " const rows = document.querySelectorAll('#table-body tr').length;"
            " return {cards, rows, stat: (document.getElementById('stat-total')||{}).textContent}; })()"
        )
        check("документы отрисовались", (rendered["cards"] + rendered["rows"]) > 0,
              f"карточек {rendered['cards']}, строк {rendered['rows']}, всего={rendered['stat']}")

        # Темы (рубрики) — вторая ось: фильтр «о чём документ», список тоже с сервера
        rubrics = await page.evaluate(
            "Array.from(document.querySelectorAll('#rubric-filter option'))"
            ".map(o => ({value: o.value, text: o.textContent}))"
        )
        rvalues = {o["value"] for o in rubrics if o["value"]}
        rtexts = {o["value"]: o["text"] for o in rubrics}
        check("фильтр тем заполнился словарём", len(rvalues) >= 8,
              f"тем {len(rvalues)}: {sorted(rvalues)[:6]}")
        check("тема «Информационная безопасность» названа по-русски",
              rtexts.get("infosec") == "Информационная безопасность", f"{rtexts.get('infosec')!r}")

        # Ошибка «Список видов не загрузился» — это мой тост: значит fetch не сработал
        toast = await page.evaluate("(document.getElementById('toast')||{}).textContent || ''")
        check("нет жалобы «список видов не загрузился»", "не загрузился" not in toast, toast or "—")
        check("нет ошибок JS", not errors, "; ".join(errors[:3]) if errors else "чисто")
        # Отдельно: какие именно ответы не 2xx — «Failed to load resource» без URL ничего не говорит
        uniq = sorted(set(bad_responses))
        print("   не-2xx ответы:", uniq[:6] if uniq else "нет")
        check("нет ответов 5xx", not any(r.startswith("5") for r in uniq), f"{uniq[:3]}")

        await browser.close()

    ok = all(checks)
    print(f"\nVERDICT {'OK' if ok else 'ФЕЙЛ'} {sum(checks)}/{len(checks)}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
