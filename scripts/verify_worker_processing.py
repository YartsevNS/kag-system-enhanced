"""Проверка: новый воркер (из ветки PREPROD) реально обрабатывает документ.

Зачем отдельно от «контейнер поднялся»: воркер не пересобирался с 20.09, и его часть кода
в TABLES/PREPROD ни разу не работала. Пуск контейнера и «celery ready» это не подтверждают —
нужно поставить реальный документ в очередь и увидеть, что задача выполнена.

Запуск внутри api-контейнера:
    docker exec -e DOC=<id> kag-api python /app/data/verify_worker_processing.py
Если DOC не задан — берётся последний завершённый документ из базы.
"""
import json
import os
import subprocess
import time
import urllib.request

API = "http://localhost:8000"


def req(method: str, path: str, token: str = "", payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(API + path, data=data, method=method,
                               headers={"Content-Type": "application/json",
                                        **({"Authorization": "Bearer " + token} if token else {})})
    with urllib.request.urlopen(r, timeout=300) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def psql(sql: str) -> str:
    """Запрос к базе через docker (скрипт запускается на хосте стенда)."""
    for cmd in (["docker", "exec", "kag-postgres", "psql", "-U", "kag", "-d", "kag", "-t", "-A", "-c", sql],
                ["psql", "-U", "kag", "-d", "kag", "-t", "-A", "-c", sql]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if out.returncode == 0:
                return (out.stdout or "").strip()
        except Exception:
            continue
    return ""


def admin_password() -> str:
    """Пароль админа: из файла (в контейнере) или из окружения контейнера (на хосте)."""
    for p in ("/tmp/.kag_pass", "/app/data/.kag_pass"):
        try:
            return open(p).read().strip()
        except Exception:
            continue
    for cmd in (["docker", "exec", "kag-api", "printenv", "ADMIN_PASSWORD"],):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except Exception:
            continue
    return os.environ.get("ADMIN_PASSWORD", "")


def main() -> int:
    pw = admin_password()
    token = req("POST", "/api/v1/auth/login",
                payload={"username": "admin", "password": pw}).get("access_token", "")
    print(f"вход: {'ок' if token else 'НЕ УДАЛСЯ'}")

    doc = os.environ.get("DOC", "").strip()
    if not doc:
        doc = psql("select id from documents where status='completed' order by updated_at desc limit 1")
    print(f"документ: {doc}")

    before = psql(f"select status || ' | updated ' || to_char(updated_at,'HH24:MI:SS') "
                  f"from documents where id='{doc}'")
    print(f"  до: {before}")

    try:
        r = req("POST", f"/api/v1/upload/{doc}/process", token, {})
        print(f"  постановка в очередь: {json.dumps(r, ensure_ascii=False)[:200]}")
    except Exception as e:
        print(f"  постановка не удалась: {type(e).__name__}: {str(e)[:150]}")
        return 1

    print("  ждём завершения…")
    final = ""
    for i in range(40):
        st = psql(f"select status from documents where id='{doc}'")
        final = st
        if i % 4 == 0:
            print(f"    [{i*5} с] {st}")
        if st in ("completed", "failed"):
            break
        time.sleep(5)

    after = psql(f"select status || ' | updated ' || to_char(updated_at,'HH24:MI:SS') "
                 f"from documents where id='{doc}'")
    chunks = psql(f"select count(*) from chunks where document_id='{doc}'")
    tables = psql(f"select count(*) from document_tables where document_id='{doc}'")
    print(f"  после: {after}")
    print(f"  чанков у документа: {chunks} | таблиц: {tables}")
    print(f"ИТОГ: {'обработка выполнена' if final == 'completed' else 'СТАТУС ' + final}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
