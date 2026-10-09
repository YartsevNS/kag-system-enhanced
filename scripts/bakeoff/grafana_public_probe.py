"""Разбор вызовов Grafana про публичный дашборд: какой набор работает.

Повод: после правки дашборда старая публичная ссылка отдавала прежнюю версию (панели запрашивали
данные как panels/undefined и получали 400), а наша попытка «снять публичность и опубликовать заново»
дала «Dashboard is already public». В самой Grafana разные методы ведут себя по-разному; aquí
проверяем по порядку и печатаем коды, чтобы в коде эндпоинта осталась рабочая последовательность.

Запуск в контейнере api (у него есть доступ к Grafana по внутренней сети и пароль в окружении):
  docker cp grafana_public_probe.py kag-api:/tmp/ && docker exec kag-api python /tmp/grafana_public_probe.py
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request

BASE = os.environ.get("GRAFANA_URL", "http://kag-grafana:3000").rstrip("/")
USER = os.environ.get("GRAFANA_USER", "admin")
PASSWORD = os.environ.get("GRAFANA_ADMIN_PASSWORD", "")
UID = os.environ.get("GRAFANA_UID", "kag-storage")
BODY = {"isEnabled": True, "annotationsEnabled": False,
        "timeSelectionEnabled": True, "share": "public"}
URL = f"{BASE}/api/dashboards/uid/{UID}/public-dashboards"


def call(method: str, url: str = URL, body: dict | None = None) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    auth = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Basic {auth}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode()[:200]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def main() -> None:
    if not PASSWORD:
        print("нет GRAFANA_ADMIN_PASSWORD в окружении")
        return
    print("Grafana:", BASE, "| дашборд:", UID)
    steps = [
        ("GET (состояние)", lambda: call("GET")),
        ("PUT (обновить)", lambda: call("PUT", body=BODY)),
        ("POST (создать)", lambda: call("POST", body=BODY)),
        ("DELETE (снять)", lambda: call("DELETE")),
        ("GET после DELETE", lambda: call("GET")),
        ("POST после DELETE", lambda: call("POST", body=BODY)),
    ]
    for name, fn in steps:
        code, text = fn()
        print(f"{name:20} код {code}  {text}")

    # отдельно проверим: остаётся ли публичный дашборд рабочим и отдаёт ли данные
    code, text = call("GET")
    print("\nитог: публичный дашборд сейчас:", code, text[:120])


if __name__ == "__main__":
    main()
