"""Проверка страницы «Docker» через nginx: ошибки скрипта, сырые данные и встроенная Grafana.

Повод: в первом снимке панели «Диски» и «Контейнеры» остались пустыми, а в блоке динамики
показалось «Not Found». Первое — ошибка в скрипте страницы, второе — артефакт проверки:
страницу открывали по порту API, минуя nginx, поэтому адрес /grafana/ попал в наш API.

Здесь открываем страницу ТАК, КАК ЕЁ ОТКРЫВАЕТ ПОЛЬЗОВАТЕЛЬ (через nginx, https),
ловим ошибки консоли и смотрим, что реально отрисовалось.

Запуск в контейнере базового образа:
  docker run --rm --network host -v /tmp/kagui/mon:/work -v .../scripts/bakeoff:/scripts \
    -e ADMIN_PASSWORD=... --entrypoint /usr/local/bin/python kre44et/kag-base:<tag> /scripts/docker_page_probe.py
"""
import os
import sys

from playwright.sync_api import sync_playwright

USER = sys.argv[1] if len(sys.argv) > 1 else "admin"
BASE = sys.argv[2] if len(sys.argv) > 2 else "https://127.0.0.1"
PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
OUT = os.environ.get("OUT_DIR", "/work")


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--ignore-certificate-errors"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 1000},
                                  ignore_https_errors=True)
        print("вход:", ctx.request.post(f"{BASE}/api/v1/auth/login",
                                        data={"username": USER, "password": PASSWORD}).status)
        page = ctx.new_page()
        errors, failed, bad_responses = [], [], []
        page.on("pageerror", lambda e: errors.append(f"исключение: {str(e)[:200]}"))
        page.on("console", lambda m: errors.append(f"консоль: {m.text[:200]}") if m.type == "error" else None)
        page.on("requestfailed", lambda r: failed.append(f"{r.url[:120]} — {r.failure}"))
        # Главное для разбора: какие именно запросы вернули ошибку (без этого «403» ни о чём не говорит)
        api_calls = []
        bodies: dict[str, str] = {}

        def on_response(r):
            if r.status >= 400:
                bad_responses.append(f"{r.status} {r.url[:130]}")
                # Тело ошибки от Grafana объясняет причину (какие поля запроса не устроили)
                if "/grafana/" in r.url:
                    try:
                        bodies[r.url.split("/panels/")[-1][:20]] = r.text()[:300]
                    except Exception:
                        pass
            if "/api/v1" in r.url:
                api_calls.append(f"{r.status} {r.url.split('/api/v1')[-1][:60]}")

        page.on("response", on_response)

        # Запоминаем, что именно вернул API хранилища — по этому видно, дошёл ли до страницы grafana_url
        page.on("response", lambda r: page.evaluate("k => window.__kagRawKeys = k",
                                                    list(r.json().keys()))
                if "/storage/raw" in r.url and r.status == 200 else None)
        page.goto(f"{BASE}/docker")
        page.wait_for_timeout(10000)

        state = page.evaluate("""() => {
            const txt = (id) => (document.getElementById(id) || {}).innerText || '';
            const frame = document.getElementById('grafana-frame');
            return {
                disks: txt('disks').slice(0, 200),
                disksLen: txt('disks').length,
                containers: txt('containers').slice(0, 120),
                frameSrc: frame ? frame.getAttribute('src') : '(нет кадра)',
                raw: window.__kagRawKeys || null,
                stamp: txt('load-stamp'),
            };
        }""")
        page.screenshot(path=f"{OUT}/docker_nginx.png")

        print("\nпанель «Диски» — длина текста:", state["disksLen"])
        print(state["disks"][:180].replace("\n", " | "))
        print("\nпанель «Контейнеры»:", state["containers"].replace("\n", " | ")[:120])
        print("\nкадр Grafana:", state["frameSrc"][:120])
        print("отметка времени:", state["stamp"])

        print("\nзапросы к нашему API:")
        for a in dict.fromkeys(api_calls):
            print("  -", a)
        for key, body in bodies.items():
            print(f"  тело ошибки [{key}]: {body}")
        print("\nответы с ошибкой:", len(bad_responses))
        for b in dict.fromkeys(bad_responses):
            print("  -", b)
        print("\nошибки страницы:", len(errors))
        for e in dict.fromkeys(errors):
            print("  -", e)
        if failed:
            print("не прошли запросы:")
            for f in dict.fromkeys(failed):
                print("  -", f)
        browser.close()


if __name__ == "__main__":
    main()
