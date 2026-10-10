"""Приёмка страницы спорных случаев: правила доступа, ручная правка, происхождение.

Зачем прибор, а не «посмотрел код». Правило «ручное значение правит только администратор» живёт в
проверке внутри роута; на стенде его надо ДОКАЗАТЬ живыми запросами — иначе оно тихо теряется при
следующей правке. Прибор поднимает временного не-администратора, прогоняет отказы, проверяет успешный
путь администратора и убирает за собой (временный пользователь удалён, документ возвращён в
машинное состояние).

Запуск (внутри контейнера api, пароль из окружения):
  docker exec kag-api python /app/data/e2e_labels_review.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

API = os.environ.get("KAG_API", "http://127.0.0.1:8000")
results: list = []


def log(ok: bool, title: str, extra: str = "") -> None:
    results.append(ok)
    print(f"{'OK  ' if ok else 'ФЕЙЛ'} | {title}" + (f" | {extra}" if extra else ""))


def login(username: str, password: str) -> str:
    req = urllib.request.Request(
        API + "/api/v1/auth/login",
        data=json.dumps({"username": username, "password": password}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.headers.get("Set-Cookie", "").split(";")[0]


def call(method: str, path: str, cookie: str = "", body: dict | None = None,
         raw: bool = False):
    """Вернуть (код, тело). Редирект НЕ следуем: иначе «закрыто» выглядит как «открыто».

    Для проверки доступности страницы нужен честный код ответа, поэтому используем
    HTTPRedirectHandler-запрет (urllib иначе молча идёт на /login и отдаёт 200).
    """
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):  # noqa: D102
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          **({"Cookie": cookie} if cookie else {})})
    try:
        with opener.open(req, timeout=60) as r:
            text = r.read().decode("utf-8", "replace")
            return r.status, (text if raw else _json(text))
    except urllib.error.HTTPError as e:
        return e.code, _json(e.read().decode("utf-8", "replace"))


def _json(text: str):
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        return {"_raw": text[:200]}


def main() -> int:
    admin_password = os.environ.get("ADMIN_PASSWORD")
    admin_user = os.environ.get("ADMIN_USERNAME", "admin")
    if not admin_password:
        print("нет ADMIN_PASSWORD в окружении")
        return 2
    admin_ck = login(admin_user, admin_password)

    # ── 1. Страница доступна вошедшему и закрыта без входа ───────────────────
    code_anon, _ = call("GET", "/labels", raw=True)
    log(code_anon in (301, 302, 303, 307), "страница /labels закрыта без входа", f"код {code_anon}")
    code_auth, _ = call("GET", "/labels", cookie=admin_ck, raw=True)
    log(code_auth == 200, "страница /labels отдаётся вошедшему", f"код {code_auth}")
    code_missing, _ = call("GET", "/api/v1/labels/review", cookie=admin_ck)
    log(code_missing == 200, "GET /api/v1/labels/review отвечает", f"код {code_missing}")

    # ── 2. Временный не-администратор: правило «ручное правит только админ» ──
    name = "label-probe-" + os.urandom(3).hex()
    password = "probe-" + os.urandom(6).hex()
    code, created = call("POST", "/api/v1/auth/register", cookie=admin_ck,
                         body={"username": name, "password": password, "is_admin": False})
    if code != 201:
        log(False, "временный пользователь не создан", f"код {code} {created}")
        return 1
    uid = created.get("id")
    user_ck = login(name, password)

    code, mine = call("GET", "/api/v1/labels/review", cookie=user_ck)
    log(code == 200 and mine.get("can_admin") is False,
        "сотрудник видит список, но не как администратор", f"код {code}")

    code, _ = call("POST", "/api/v1/labels/review/unlock", cookie=user_ck,
                   body={"document_id": "нет-такого", "field": "rubrics"})
    log(code == 403, "снятие ручной пометки закрыто сотруднику", f"код {code}")

    admin_items = (call("GET", "/api/v1/labels/review", cookie=admin_ck)[1] or {}).get("items") or []

    def _values(item) -> list:
        raw = item.get("value")
        vals = raw if isinstance(raw, list) else ([raw] if raw else [])
        return [str(v) for v in vals if v]

    # Берём документ, у которого значение УЖЕ есть: тогда правка тем же значением не меняет данные,
    # а проверяет именно правило и провенанс (приёмка не должна оставлять за собой другие значения).
    with_value = [i for i in admin_items if _values(i)]
    target = next((i for i in with_value if i.get("kind") == "disputed"), None) or \
        (with_value[0] if with_value else None)

    if target:
        code, body = call("POST", "/api/v1/labels/review/decide", cookie=user_ck,
                          body={"document_id": target["document_id"], "field": target["field"],
                                "value": _values(target)})
        log(code == 403, "чужой документ сотруднику не доступен", f"код {code}")

    # ── 3. Администратор: правка, пометка «ручное», снятие пометки ───────────
    if not target:
        print("   (значений для проверки нет — сначала прогнать импорт провенанса)")
    else:
        value = _values(target)
        code, body = call("POST", "/api/v1/labels/review/decide", cookie=admin_ck,
                          body={"document_id": target["document_id"], "field": target["field"],
                                "value": value, "note": "приёмка прав: проверка ручной правки"})
        log(code == 200, "администратор правит значение", f"код {code} {body.get('status')}")

        code, after = call("GET", "/api/v1/labels/review?only=manual", cookie=admin_ck)
        manual = [i for i in (after.get("items") or [])
                  if i["document_id"] == target["document_id"] and i["field"] == target["field"]]
        log(bool(manual) and manual[0].get("by"),
            "ручная правка помечена в метаданных (кто)", f"by={manual[0].get('by') if manual else '—'}")

        if manual:
            code, _ = call("POST", "/api/v1/labels/review/decide", cookie=user_ck,
                           body={"document_id": target["document_id"], "field": target["field"],
                                 "value": value})
            log(code == 403, "сотруднику ручное значение не переписать", f"код {code}")

        code, body = call("POST", "/api/v1/labels/review/unlock", cookie=admin_ck,
                          body={"document_id": target["document_id"], "field": target["field"]})
        log(code == 200 and body.get("manual") is False,
            "администратор снимает ручную пометку (поле возвращается модели)", f"код {code}")

    # ── 4. Уборка за собой ───────────────────────────────────────────────────
    if uid:
        code, _ = call("DELETE", f"/api/v1/admin/users/{uid}", cookie=admin_ck)
        log(code in (200, 204), "временный пользователь удалён", f"код {code}")
        code, users = call("GET", "/api/v1/admin/users", cookie=admin_ck)
        names = {u.get("username") for u in (users.get("users") or [])}
        log(name not in names, "в базе не осталось мусорного пользователя", f"код {code}")

    failed = results.count(False)
    print(f"\nитог: проверок {len(results)}, провалено {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
