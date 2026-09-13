"""Сплошная проверка админ-роутов: список берём из самого приложения.

OpenAPI на стенде отключён (/api/openapi.json → 404), поэтому маршруты читаем из
app.routes внутри контейнера, а затем дёргаем их по HTTP без токена и с токеном
не-администратора.

Запуск:
    docker cp scripts/check_admin_routes.py kag-api:/tmp/
    docker exec -e ADMIN_PASSWORD=... kag-api python /tmp/check_admin_routes.py
"""
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = "http://localhost:8000"
PLACEHOLDER = "x"


def call(method: str, path: str, token: str | None = None, body: dict | None = None,
         timeout: int = 180):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:  # таймауты и прочее
        return 0, str(e)


def admin_routes() -> list[str]:
    from src.api.main import app
    out = []
    for r in app.routes:
        path = getattr(r, "path", "")
        methods = getattr(r, "methods", None) or set()
        if not path.startswith("/api/v1/admin"):
            continue
        for m in methods:
            if m in ("GET", "POST", "PUT", "DELETE", "PATCH"):
                out.append(f"{m} {path}")
    # убираем параметры пути: заменяем {..} на заглушку
    return sorted({re.sub(r"\{[^}]+\}", PLACEHOLDER, x) for x in out})


def main() -> int:
    admin_pw = os.environ.get("ADMIN_PASSWORD", "")
    routes = admin_routes()
    print(f"=== Всего админ-роутов: {len(routes)} ===")

    open_now = []
    for r in routes:
        method, path = r.split(" ", 1)
        code, _ = call(method, path, timeout=30)
        if code not in (401, 403):
            open_now.append(f"{r} → {code}")
    print("Без токена доступны:", open_now if open_now else "нет — все 401/403")

    code, body = call("POST", "/api/v1/auth/login",
                      body={"username": "admin", "password": admin_pw})
    if code != 200:
        print("не удалось войти администратором:", code, body[:120])
        return 1
    admin = json.loads(body)["access_token"]

    uname = "tmp_routes_probe"
    pw = secrets.token_urlsafe(16)
    code, body = call("POST", "/api/v1/auth/register", admin,
                      {"username": uname, "password": pw, "is_admin": False})
    uid = json.loads(body).get("id") if code in (200, 201) else None
    if uid:
        ucode, ubody = call("POST", "/api/v1/auth/login",
                            body={"username": uname, "password": pw})
        utoken = json.loads(ubody).get("access_token")
        allowed = []
        for r in routes:
            method, path = r.split(" ", 1)
            code, _ = call(method, path, utoken, timeout=30)
            if code not in (401, 403):
                allowed.append(f"{r} → {code}")
        print("Не-админ проходит на:", allowed if allowed else "нет — все 401/403")
        call("DELETE", f"/api/v1/admin/users/{uid}", admin)
        print("временный пользователь удалён")
    else:
        print("не удалось создать временного пользователя:", code, body[:120])

    print()
    print("=== /ext-llm: ключ должен быть замаскирован ===")
    code, body = call("GET", "/api/v1/admin/models/ext-llm", admin)
    d = json.loads(body)
    print(f"HTTP {code} | api_key={d.get('api_key')!r} | api_key_set={d.get('api_key_set')}")

    print()
    print("=== /deploy: валидация ===")
    for payload, label in (
        ({"action": "nope"}, "неизвестный action"),
        ({"action": "write_file", "file_path": "evil.so", "file_content": "x"}, "расширение .so"),
        ({"action": "write_file", "file_path": "api/__init__.py",
          "file_content": "not-a-valid-base64!!!", "encoding": "base64"}, "битый base64"),
    ):
        code, body = call("POST", "/api/v1/admin/models/deploy", admin, payload)
        print(f"   {label}: HTTP {code} | {body[:100]}")

    print()
    print("=== Нагрузка: health во время 10 параллельных /aliases ===")

    def health():
        t = time.perf_counter()
        code, _ = call("GET", "/api/v1/health", timeout=10)
        return round((time.perf_counter() - t) * 1000), code

    def aliases():
        t = time.perf_counter()
        code, _ = call("GET", "/api/v1/admin/models/aliases", admin, timeout=60)
        return round((time.perf_counter() - t) * 1000), code

    with ThreadPoolExecutor(max_workers=11) as ex:
        heavy = [ex.submit(aliases) for _ in range(10)]
        time.sleep(0.2)
        h = ex.submit(health).result()
        res = [f.result() for f in heavy]
    print(f"health: {h[0]} мс (HTTP {h[1]}) | /aliases: макс {max(r[0] for r in res)} мс, "
          f"ошибок {sum(1 for r in res if r[1] >= 400)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
