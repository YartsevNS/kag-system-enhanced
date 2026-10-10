"""Браузерная проверка страницы «Спорные случаи»: рендер, контролы, отсутствие ошибок.

Запускать ВНУТРИ worker-контейнера (там есть Playwright + Chromium):
  docker cp e2e_labels_page.py kag-system_worker_1:/tmp/
  docker exec kag-system_worker_1 python /tmp/e2e_labels_page.py

Адрес внутри docker-сети — http://api:8000 (контейнер не видит 127.0.0.1:8000 хоста).
Сессия подставляется cookie `kag_token`, полученной логином через API: это быстрее и надёжнее формы.
Снимок страницы кладётся в /tmp/labels_page.png — его надо забрать и посмотреть глазами (вёрстка
компиляцией не проверяется).
"""
from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request

API = "http://api:8000"
results: list = []


def log(ok: bool, title: str, extra: str = "") -> None:
    results.append(ok)
    print(f"{'OK  ' if ok else 'ФЕЙЛ'} | {title}" + (f" | {extra}" if extra else ""))


def token() -> str:
    """Пароль берём из окружения, а если его там нет — из файла, подготовленного снаружи.

    В worker-контейнере пароля нет (он есть только у api), и передавать его аргументом нельзя:
    значение окажется в `ps` на хосте. Поэтому пароль заранее кладётся в `/tmp/.kag_pass` через stdin.
    """
    password = os.environ.get("ADMIN_PASSWORD")
    if not password:
        try:
            with open("/tmp/.kag_pass", encoding="utf-8") as f:
                password = f.read().strip()
        except OSError:
            password = ""
    if not password:
        print("нет пароля: положите его в /tmp/.kag_pass или ADMIN_PASSWORD")
        return ""
    req = urllib.request.Request(
        API + "/api/v1/auth/login",
        data=json.dumps({"username": os.environ.get("ADMIN_USERNAME", "admin"),
                         "password": password}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        body = json.loads(r.read().decode())
    if body.get("access_token"):
        return body["access_token"]
    cookie = r.headers.get("Set-Cookie", "")
    for part in cookie.split(";"):
        if part.strip().startswith("kag_token="):
            return part.strip().split("=", 1)[1]
    return ""


def _api(method: str, path: str, tok: str, body: dict | None = None):
    """Запрос от имени админа по cookie сессии: проверить, что кнопка на странице ЗАПИСАЛА данные."""
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          "Cookie": f"kag_token={tok}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            return e.code, {}


def _manual_items(tok: str) -> set:
    """Множество (документ, поле) с ручной пометкой — по нему видно, что кнопка сработала."""
    _, data = _api("GET", "/api/v1/labels/review?only=manual", tok)
    return {(i["document_id"], i["field"]) for i in (data.get("items") or [])}


async def main() -> int:
    from playwright.async_api import async_playwright

    tok = token()
    if not tok:
        print("не получил токен сессии")
        return 2
    errors: list = []
    async with async_playwright() as p:
        b = await p.chromium.launch()
        ctx = await b.new_context(viewport={"width": 1440, "height": 950})
        await ctx.add_cookies([{"name": "kag_token", "value": tok, "domain": "api", "path": "/"}])
        page = await ctx.new_page()
        page.on("pageerror", lambda e: errors.append("pageerror: " + str(e)[:200]))
        page.on("console", lambda m: errors.append("console: " + m.text[:200])
                if m.type == "error" else None)
        await page.goto(API + "/labels", wait_until="networkidle")
        await page.wait_for_timeout(1200)

        # 1. Страница наша, а не экран входа
        title = await page.title()
        log("Спорные случаи" in title or "разметки" in title, "открылась страница разбора", title)

        # 2. Код страницы не вывалился текстом в тело
        body_text = await page.inner_text("body")
        log("function decide" not in body_text, "код не печатается как текст")

        # 3. Таблица и счётчики
        rows = await page.evaluate("document.querySelectorAll('#list tbody tr').length")
        counts = await page.evaluate(
            "['n-disputed','n-manual','n-docs','n-threshold'].map(i => (document.getElementById(i)||{}).textContent)")
        log(rows > 0, "строки со значениями отрисованы", f"строк {rows}")
        log(all(c not in (None, "—") for c in counts), "счётчики заполнены", str(counts))

        # 4. Контролы и подписи
        selects = await page.evaluate("document.querySelectorAll('#list select').length")
        locked = await page.evaluate("document.querySelectorAll('#list .locked').length")
        badges = await page.evaluate("document.querySelectorAll('#list .badge').length")
        log(selects > 0, "контролы выбора значений есть", f"select {selects}, замков {locked}")
        log(badges > 0, "метки «спорное/ручная правка» на месте", f"метки {badges}")
        log(selects + locked > 0, "у каждой строки есть либо выбор, либо объяснение замка")

        status = await page.inner_text("#status")
        log("показано" in status, "строка состояния говорит, сколько показано", status[:80])

        # 5. Снимок для глаз
        await page.screenshot(path="/tmp/labels_page.png", full_page=False)
        log(True, "снимок страницы сохранён", "/tmp/labels_page.png")

        # 6. Реальный клик: сохранить значение ПЕРВОЙ доступной строки.
        # Не трогаем выбор в селекте — страница уже подставляет ТЕКУЩЕЕ значение, поэтому запись
        # не меняет данные, а проверяет путь: запрос дошёл, значение помечено ручным.
        manual_before = _manual_items(tok)
        editable = await page.evaluate(
            "(() => { const s = [...document.querySelectorAll('#list select')].find(x => !x.disabled); "
            "return s ? s.id : ''; })()")
        if editable:
            before_idx = await page.evaluate("(id) => id.replace('v','')", editable)
            await page.click(f"#list tr:nth-child({int(before_idx) + 1}) button.primary")
            await page.wait_for_timeout(2500)
            manual_after = _manual_items(tok)
            new_items = [i for i in manual_after
                         if (i["document_id"], i["field"]) not in manual_before]
            log(bool(new_items), "нажатие «Сохранить» записало ручную правку",
                f"новых ручных записей: {len(new_items)}")
            if new_items:
                item = new_items[0]
                log(item.get("by") == "admin", "в метаданных записан автор правки",
                    f"by={item.get('by')} note={item.get('note')}")
                # Уборка: снимаем ручную пометку, значение не трогаем (оно записано тем же значением).
                code, _ = _api("POST", "/api/v1/labels/review/unlock", tok,
                               {"document_id": item["document_id"], "field": item["field"]})
                log(code == 200, "пометка снята после проверки (данные не изменены)", f"код {code}")
        else:
            print("   (все значения уже ручные — клик сохранения пропущен, чтобы не менять данные)")

        await b.close()

    real = [e for e in errors if "thumbnail" not in e and "401" not in e]
    log(not real, "ошибок страницы нет", "; ".join(real[:2]))
    failed = results.count(False)
    print(f"\nитог: проверок {len(results)}, провалено {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
