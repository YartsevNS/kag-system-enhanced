#!/usr/bin/env python3
"""Переиндексация подмножества документов из каталога uploads.

Зачем: файлы в ./data/uploads сохранились, а записи в БД документов почти пусты
(была очистка), поэтому штатный reindex по document_id не работает, а повторная
загрузка создаёт НОВЫЙ document_id — старые чанки в Qdrant/Neo4j остаются
и дают дубли в поиске. Скрипт делает это аккуратно: загружает файл заново,
дожидается обработки и удаляет данные старого document_id.

Использование (на сервере 18):
    # Все секреты — из .env (export их перед запуском), НЕ аргументами:
    # значения аргументов видны в ps всем локальным пользователям.
    set -a; . ./.env; set +a
    python3 scripts/reindex_from_uploads.py \
        --pattern 'cba4db51|7b7d951b|a3bbc588|5fa8e982|92d287b9'

    # посмотреть, что будет сделано, ничего не меняя:
    python3 scripts/reindex_from_uploads.py --pattern gost --dry-run

Скрипт только stdlib. Neo4j чистится через `docker exec <container> cypher-shell`
(по умолчанию kag-neo4j); Qdrant — через HTTP API.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

UUID_RE = re.compile(r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})_")

# access_token живёт 15 минут (см. TokenResponse в src/api/routes/auth.py), а
# прогон подмножества документов может идти часами: без авто-релогина upload
# и reindex после 15-й минуты падают с 401. Обновляем токен заранее (12 мин) и
# повторяем запрос один раз, если сервер всё же ответил 401.
TOKEN_TTL_SECONDS = 12 * 60
_AUTH = {"base": "", "user": "admin", "password": "", "token": "", "ts": 0.0}


def login(base: str, user: str, password: str) -> str:
    """POST /api/v1/auth/login → access_token ('' при неудаче)."""
    try:
        data = json.dumps({"username": user, "password": password}).encode()
        req = urllib.request.Request(f"{base}/api/v1/auth/login", data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode()).get("access_token", "") or ""
    except Exception as e:
        print(f"    логин не удался: {e}")
        return ""


def current_token() -> str:
    """Токен с авто-обновлением. Без пароля — статический (--token)."""
    a = _AUTH
    if not a["password"]:
        return a["token"]
    if not a["token"] or (time.time() - a["ts"]) > TOKEN_TTL_SECONDS:
        fresh = login(a["base"], a["user"], a["password"])
        if fresh:
            a["token"], a["ts"] = fresh, time.time()
    return a["token"]


def _force_relogin() -> None:
    _AUTH["ts"] = 0.0
    _AUTH["token"] = ""


def _http(url: str, payload: Optional[dict] = None, token: Optional[str] = None,
          method: Optional[str] = None, timeout: float = 180.0,
          api_key: Optional[str] = None) -> dict:
    for attempt in (1, 2):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
        req.add_header("Content-Type", "application/json")
        tok = current_token()
        if tok:
            req.add_header("Authorization", f"Bearer {tok}")
        if api_key:
            # Qdrant использует собственный заголовок api-key (не Bearer)
            req.add_header("api-key", api_key)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode()
            return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 1 and _AUTH["password"]:
                # Токен истёк (15 мин) — перелогиниваемся и повторяем один раз.
                _force_relogin()
                continue
            # Не падаем на отдельных ошибках (404 удалённого документа и т.п.) —
            # вызывающий код решает, что делать.
            return {"__http_error__": e.code, "__body__": e.read().decode()[:200]}
        except Exception as e:
            return {"__http_error__": str(e)}
    return {"__http_error__": 401}


def _http_multipart(url: str, path: Path, token: Optional[str], timeout: float = 600.0) -> dict:
    boundary = f"----kagboundary{uuid.uuid4().hex}"
    content_type = "application/pdf" if path.suffix.lower() == ".pdf" else "text/plain"
    parts = []
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n".encode()
    )
    parts.append(path.read_bytes())
    parts.append(f"\r\n--{boundary}\r\n".encode())
    parts.append(b'Content-Disposition: form-data; name="visibility"\r\n\r\npublic\r\n')
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)

    for attempt in (1, 2):
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        tok = current_token()
        if tok:
            req.add_header("Authorization", f"Bearer {tok}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 1 and _AUTH["password"]:
                _force_relogin()
                continue
            raise


def wait_completed(base: str, doc_id: str, token: str, max_wait: float = 3600.0,
                   since: Optional[float] = None) -> str:
    """Ждать завершения обработки документа.

    since: unix-время постановки задачи. Нужен, потому что документ может быть
    УЖЕ completed (случай дедупликации по хэшу: повторная загрузка того же файла
    возвращает существующий document_id, и /process переобрабатывает его с
    force=True). Без этой проверки опрос «status == completed» срабатывает
    мгновенно — до того, как задача вообще начнёт работу: так скрипт решает, что
    документ готов, и идёт дальше (проверено 2026-09-12: 12 задач уехали в
    очередь, а цепочка «завершила» их за 0 секунд). Ждём НОВОГО завершения:
    status == completed И updated_at > since.
    """
    start = time.time()
    last = ""
    polls = 0
    while time.time() - start < max_wait:
        st = _http(f"{base}/api/v1/upload/{doc_id}/status", token=token)
        polls += 1
        if "__http_error__" in st:
            return f"http_{st['__http_error__']}"
        status = st.get("status") or ""
        fresh = True
        if since is not None:
            upd = st.get("updated_at")
            fresh = False
            if upd:
                try:
                    ts = datetime.fromisoformat(str(upd).replace("Z", "+00:00"))
                    if ts.tzinfo is not None:
                        ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
                    # updated_at в API — naive UTC (datetime.utcnow()), поэтому
                    # порог тоже берём в naive UTC.
                    fresh = ts >= datetime.utcfromtimestamp(since)
                except (ValueError, TypeError):
                    fresh = True  # не смогли разобрать — не блокируем ожидание
        if status != last:
            print(f"      статус: {status} ({st.get('progress')})", flush=True)
            last = status
        elif polls % 10 == 0:
            # Heartbeat раз в ~60 с: длинные документы обрабатываются часами
            print(f"      ... {status} ({st.get('progress')}), прошло "
                  f"{int(time.time() - start)} с", flush=True)
        if status in ("completed", "failed"):
            if fresh:
                return status
            if polls % 10 == 0:
                print("      (это завершение ПРЕДЫДУЩЕЙ обработки — ждём новую задачу)",
                      flush=True)
        time.sleep(6)
    return "timeout"


def delete_qdrant(qdrant: str, api_key: str, document_id: str) -> bool:
    try:
        payload = {"filter": {"must": [{"key": "document_id", "match": {"value": document_id}}]}}
        resp = _http(f"{qdrant}/collections/kag_documents/points/delete",
                     payload, method="POST", api_key=api_key)
        if "__http_error__" in resp:
            print(f"      Qdrant delete HTTP ошибка: {resp['__http_error__']} {resp.get('__body__','')}")
            return False
        # Проверяем, что точки действительно удалены
        cnt = _http(f"{qdrant}/collections/kag_documents/points/count", payload={"exact": True, "filter": payload["filter"]},
                    method="POST", api_key=api_key)
        left = (cnt.get("result") or {}).get("count")
        if left:
            print(f"      Qdrant: осталось {left} точек после удаления — не удалено")
            return False
        return True
    except Exception as e:
        print(f"      Qdrant delete ошибка: {e}")
        return False


def delete_neo4j(container: str, password: str, document_id: str) -> bool:
    cypher = (
        f'MATCH (d:Document {{id: "{document_id}"}}) '
        f'OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c) DETACH DELETE c, d RETURN count(*)'
    )
    try:
        r = subprocess.run(
            ["docker", "exec", container, "cypher-shell", "-u", "neo4j", "-p", password, cypher],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0:
            print(f"      Neo4j ошибка: {r.stderr.strip()[:120]}")
        return r.returncode == 0
    except Exception as e:
        print(f"      Neo4j недоступен: {e}")
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Переиндексация подмножества из uploads")
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--token", default="", help="Bearer-токен (либо --auth-pass/ADMIN_PASSWORD)")
    ap.add_argument("--auth-user", default="admin", help="пользователь для авто-релогина")
    ap.add_argument("--auth-pass", default=os.environ.get("ADMIN_PASSWORD", ""),
                    help="пароль для авто-релогина (по умолчанию $ADMIN_PASSWORD). "
                         "Нужен для прогонов длиннее 15 минут (TTL access_token)")
    ap.add_argument("--uploads-dir", default="/home/yartsevn/kag-system/data/uploads")
    ap.add_argument("--pattern", default=".", help="regex по имени файла")
    ap.add_argument("--limit", type=int, default=0, help="0 = без ограничения")
    ap.add_argument("--max-wait", type=float, default=3600.0,
                    help="сколько секунд ждать обработки одного документа")
    ap.add_argument("--qdrant-url", default="http://localhost:6333")
    ap.add_argument("--qdrant-key", default=os.environ.get("QDRANT_API_KEY", ""),
                    help="ключ Qdrant (по умолчанию $QDRANT_API_KEY). НЕ передавать "
                         "аргументом без нужды — значение видно в ps всем локальным пользователям")
    ap.add_argument("--neo4j-container", default="kag-neo4j")
    ap.add_argument("--neo4j-password", default=os.environ.get("NEO4J_PASSWORD", ""),
                    help="пароль Neo4j (по умолчанию $NEO4J_PASSWORD)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", default="/tmp/reindex_subset.json")
    args = ap.parse_args()

    base = args.url.rstrip("/")
    _AUTH["base"] = base
    _AUTH["user"] = args.auth_user
    _AUTH["password"] = args.auth_pass or ""
    _AUTH["token"] = args.token or ""
    if _AUTH["password"]:
        _AUTH["token"] = login(base, _AUTH["user"], _AUTH["password"]) or _AUTH["token"]
        _AUTH["ts"] = time.time()
    elif not _AUTH["token"] and not args.dry_run:
        print("Нужен --token или --auth-pass/ADMIN_PASSWORD (авто-релогин)")
        return 2
    if not args.dry_run:
        print(f"Авторизация: user={_AUTH['user']}, "
              f"аккаунт с авто-релогином: {'да' if _AUTH['password'] else 'нет (статический токен)'}")

    rx = re.compile(args.pattern, re.IGNORECASE)
    files = sorted(p for p in Path(args.uploads_dir).iterdir()
                   if p.is_file() and rx.search(p.name))
    if args.limit:
        files = files[:args.limit]

    print(f"Файлов к обработке: {len(files)}")
    report = []
    for i, path in enumerate(files, 1):
        m = UUID_RE.match(path.name)
        old_id = m.group(1) if m else None
        print(f"\n[{i}/{len(files)}] {path.name}")
        print(f"    старый document_id: {old_id}")
        if args.dry_run:
            report.append({"file": path.name, "old_id": old_id, "action": "dry-run"})
            continue

        try:
            up = _http_multipart(f"{base}/api/v1/upload/", path, args.token)
        except urllib.error.HTTPError as e:
            print(f"    upload ошибка: HTTP {e.code} {e.read().decode()[:120]}")
            report.append({"file": path.name, "old_id": old_id, "status": f"upload_error_{e.code}"})
            continue
        new_id = up.get("document_id")
        print(f"    новый document_id: {new_id}")

        try:
            # ВАЖНО: /process (постановка задачи в Celery), а НЕ /reindex.
            # /reindex обрабатывает документ СИНХРОННО внутри HTTP-запроса: на
            # большом файле клиент уходит в таймаут, соединение рвётся, и
            # обработка обрывается на середине — документ остаётся в
            # status=processing без задачи, без замка и без ошибки
            # (проверено 2026-09-12 на PDF с 558 чанками: простой 30 минут).
            # /process ставит задачу через QueueGuard с force=True — дальше
            # работает worker, а скрипт только опрашивает /status.
            queued = _http(f"{base}/api/v1/upload/{new_id}/process", {}, args.token)
            if "__http_error__" in queued:
                print(f"    process ошибка: {queued}")
            else:
                print(f"    в очереди: {queued.get('status', '?')}")
            enqueued_at = time.time()
        except Exception as e:
            print(f"    process ошибка: {e}")
            enqueued_at = time.time()
        status = wait_completed(base, new_id, args.token, max_wait=args.max_wait,
                                since=enqueued_at)
        print(f"    итог обработки: {status}")

        cleaned = {"qdrant": False, "neo4j": False}
        if old_id and old_id != new_id:
            cleaned["qdrant"] = delete_qdrant(args.qdrant_url, args.qdrant_key, old_id)
            if args.neo4j_password:
                cleaned["neo4j"] = delete_neo4j(args.neo4j_container, args.neo4j_password, old_id)
            print(f"    старые данные удалены: {cleaned}")

        report.append({"file": path.name, "old_id": old_id, "new_id": new_id,
                       "status": status, "cleaned": cleaned})

    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nОтчёт: {args.report}")
    ok = sum(1 for r in report if r.get("status") == "completed")
    print(f"Успешно: {ok}/{len(report)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
