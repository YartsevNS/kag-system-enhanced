"""Контрольный замер: сколько идёт обработка ОДНОГО документа с графом, поэтапно.

Зачем. Если документ из одного фрагмента обрабатывается столько же, сколько из двенадцати, значит время
съедает НЕ извлечение, а фиксированная задержка в конвейере (таймаут, ожидание очереди, повтор). Такая
проверка отделяет «модель медленная» от «код ждёт».

Прибор ставит переобработку с графом (настройку включает сам, в конце возвращает как было), ждёт
завершения и печатает: время от постановки до конечного статуса, число фрагментов и записи журнала
обработки по этому документу (этапы с временем, если они есть).

Запуск внутри контейнера api:
  docker exec kag-api python /app/data/graph_e2e_timing.py --doc <id>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

API = "http://127.0.0.1:8000"


def login() -> str:
    req = urllib.request.Request(
        API + "/api/v1/auth/login",
        data=json.dumps({"username": os.environ.get("ADMIN_USERNAME", "admin"),
                         "password": os.environ.get("ADMIN_PASSWORD", "")}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.headers.get("Set-Cookie", "").split(";")[0]


def post(path: str, cookie: str, body: dict | None = None, timeout: int = 120):
    req = urllib.request.Request(API + path, data=json.dumps(body or {}).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Cookie": cookie})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except Exception as e:  # noqa: BLE001
        return 0, {"error": f"{type(e).__name__}: {str(e)[:140]}"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc", required=True)
    ap.add_argument("--wait", type=int, default=2400)
    ap.add_argument("--restore-skip", default="true",
                    help="вернуть настройку «граф не строить при обработке» после замера")
    args = ap.parse_args()

    from sqlalchemy import text

    from src.database.session import get_session_local

    cookie = login()
    print(f"настройка графа на время замера: строим (skip=false)")
    print("  ", post("/api/v1/admin/models/graph-build-config", cookie, {"skip": False})[1])

    t0 = time.time()
    status, body = post(f"/api/v1/upload/{args.doc}/process", cookie)
    print(f"постановка обработки: код {status} {json.dumps(body, ensure_ascii=False)[:120]}")

    done = None
    while time.time() - t0 < args.wait:
        time.sleep(15)
        sess = get_session_local()()
        try:
            row = sess.execute(text(
                "select status, chunks_count, progress from documents where id = :d"),
                {"d": args.doc}).first()
        except Exception as e:  # noqa: BLE001
            print(f"  (опрос не удался: {type(e).__name__})")
            sess.close()
            continue
        sess.close()
        if row is None:
            print("документ не найден")
            return 1
        if row[0] in ("completed", "failed", "error"):
            done = row
            break
        print(f"  +{time.time()-t0:5.0f} с: статус {row[0]}, прогресс {row[2]}")

    if done is None:
        print(f"не дождались за {args.wait} с")
    else:
        print(f"\nИТОГ: обработка документа с {done[1]} фрагментами заняла {time.time()-t0:.0f} с "
              f"(статус {done[0]})")

    # Этапы из журнала обработки (если пишутся) — они показывают, на что ушло время.
    sess = get_session_local()()
    try:
        rows = sess.execute(text(
            "select step, created_at from process_logs where document_id = :d "
            "order by created_at desc limit 20"), {"d": args.doc}).fetchall()
        if rows:
            print("\nэтапы обработки (последние):")
            prev = None
            for step, when in rows:
                delta = f" (+{(prev - when).total_seconds():.0f} с)" if prev else ""
                print(f"  {str(when)[11:19]} {step}{delta}")
                prev = when
    except Exception as e:  # noqa: BLE001
        print(f"\nжурнал этапов недоступен: {type(e).__name__}: {str(e)[:100]}")
    sess.close()

    if args.restore_skip.lower() == "true":
        print("\nвозвращаю настройку: граф при обработке не строить (skip=true)")
        print("  ", post("/api/v1/admin/models/graph-build-config", cookie,
                         {"skip": True, "message": "ждём локальную модель: граф строим по кнопке"})[1])
    return 0


if __name__ == "__main__":
    sys.exit(main())
