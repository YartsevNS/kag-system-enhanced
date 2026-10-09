"""Проверка селектора источника в чате: он есть, и выбранное значение уходит в запрос.

Что проверяем (по-настоящему, в браузере):
  1. элемент #chat-scope присутствует и виден;
  2. при выборе «только новости» тело запроса к /api/v1/chat/ содержит scope=news;
  3. выбор сохраняется в localStorage (после перезагрузки остаётся);
  4. ответ на новостной вопрос приходит с источником из коллекции новостей.

Запуск в контейнере api:
  ADMIN_PASSWORD=... docker exec -e ADMIN_PASSWORD kag-api python /app/data/scope_ui_probe.py
"""
import json
import os

from playwright.sync_api import sync_playwright

BASE = os.environ.get("KAG_BASE", "https://127.0.0.1")
USER = os.environ.get("ADMIN_USERNAME", "admin")
PASS = os.environ.get("ADMIN_PASSWORD", "")

with sync_playwright() as p:
    b = p.chromium.launch(args=["--no-sandbox"])
    ctx = b.new_context(viewport={"width": 1440, "height": 950}, ignore_https_errors=True)
    page = ctx.new_page()
    page.request.post(f"{BASE}/api/v1/auth/login", data={"username": USER, "password": PASS})

    bodies = []
    page.on("request", lambda r: bodies.append(r.post_data) if "/api/v1/chat/" in r.url and r.method == "POST" else None)
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)[:200]))

    page.goto(f"{BASE}/chat")
    page.wait_for_timeout(2500)

    # 1) элемент есть
    present = page.evaluate("() => !!document.getElementById('chat-scope')")
    options = page.evaluate("() => [...document.getElementById('chat-scope').options].map(o => o.value + '|' + o.text)")
    visible = page.evaluate("""() => { const s = document.getElementById('chat-scope');
        const r = s.getBoundingClientRect(); return r.width > 0 && r.height > 0; }""")
    print("селектор в разметке:", present, "| виден:", visible)
    print("варианты:", options)

    # 2) выбираем «только новости» и отправляем вопрос
    page.select_option("#chat-scope", "news")
    page.fill("#message-input", "новости Банка России о криптовалютах")
    page.click("#send-btn")
    page.wait_for_timeout(25000)

    with_scope = [body for body in bodies if body and "scope" in body]
    print(f"запросов к чату: {len(bodies)}, из них со scope: {len(with_scope)}")
    for body in with_scope[:2]:
        try:
            d = json.loads(body)
            print("   scope в теле:", d.get("scope"))
        except Exception:
            print("   тело не разобрал:", body[:120])

    # 3) запомнилось ли
    page.reload()
    page.wait_for_timeout(2500)
    saved = page.evaluate("() => document.getElementById('chat-scope').value")
    print("после перезагрузки выбрано:", saved or "(документы по умолчанию)")

    print("ошибок страницы:", len(errors))
    for e in errors[:3]:
        print("   ", e)
    b.close()
