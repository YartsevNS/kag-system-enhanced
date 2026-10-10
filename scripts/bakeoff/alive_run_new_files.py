"""Боевой прогон новых файлов: заливка → обработка → замер.

Зачем прибор, а не пара curl-ов: у заливки и обработки есть состояние (status/progress), и «код 200» на
заливку ничего не говорит о том, что документ обработан. Прибор ведёт документ до конечного статуса,
меряет время и печатает ЧТО получилось (фрагменты, векторы, таблицы), а не что запрос принят.

Запуск внутри контейнера api:
  docker exec kag-api python /app/data/alive_run_new_files.py --files a.pdf,b.pdf [--process]
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
    password = os.environ.get("ADMIN_PASSWORD") or ""
    req = urllib.request.Request(
        API + "/api/v1/auth/login",
        data=json.dumps({"username": os.environ.get("ADMIN_USERNAME", "admin"),
                         "password": password}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.headers.get("Set-Cookie", "").split(";")[0]


def upload(path: str, cookie: str) -> dict:
    """Multipart вручную: библиотека requests в контейнере есть не везде, а зависеть от неё не хочется."""
    boundary = "----kag" + os.urandom(8).hex()
    filename = os.path.basename(path)
    with open(path, "rb") as f:
        content = f.read()
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
        b"Content-Type: application/octet-stream\r\n\r\n", content, b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    req = urllib.request.Request(
        API + "/api/v1/upload/", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Cookie": cookie})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read().decode())


def post(path: str, cookie: str, timeout: int = 120) -> dict:
    req = urllib.request.Request(API + path, data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json", "Cookie": cookie})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {str(e)[:120]}"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", required=True, help="пути через запятую (внутри контейнера)")
    ap.add_argument("--process", action="store_true", help="сразу ставить в обработку")
    ap.add_argument("--wait", type=int, default=1800, help="сколько ждать обработки, секунд")
    args = ap.parse_args()

    from sqlalchemy import text

    from src.database.session import get_session_local

    cookie = login()
    paths = [p.strip() for p in args.files.split(",") if p.strip()]
    started = []
    for path in paths:
        if not os.path.exists(path):
            print(f"нет файла: {path}")
            continue
        t0 = time.time()
        try:
            res = upload(path, cookie)
        except Exception as e:  # noqa: BLE001
            print(f"заливка не удалась: {os.path.basename(path)} — {type(e).__name__}: {str(e)[:140]}")
            continue
        doc_id = res.get("document_id") or res.get("id") or ""
        print(f"залит {os.path.basename(path)[:60]:<62} id {str(doc_id)[:8]} "
              f"статус {res.get('status')} за {time.time()-t0:.1f} с")
        if doc_id:
            started.append((doc_id, os.path.basename(path), t0))
            if args.process:
                out = post(f"/api/v1/upload/{doc_id}/process", cookie)
                print(f"    запуск обработки: {out.get('status') or out.get('message') or out}")

    if not started:
        return 1

    deadline = time.time() + args.wait
    pending = {d: name for d, name, _ in started}
    while pending and time.time() < deadline:
        time.sleep(10)
        # НОВАЯ сессия на каждый опрос: если предыдущий запрос упал, старая сессия остаётся в
        # «прерванной транзакции» и все дальнейшие чтения падают молча — прибор тогда пишет
        # «не дождались завершения», хотя работа давно сделана.
        sess = get_session_local()()
        try:
            rows = sess.execute(text(
                "select id, status, progress, chunks_count, document_type, rubrics, "
                "coalesce(recognized_title,''), coalesce(error,'') from documents "
                f"where id in ({','.join([':id%d' % i for i in range(len(pending))])})"),
                {f"id{i}": d for i, d in enumerate(pending)}).fetchall()
        except Exception as e:  # noqa: BLE001
            print(f"  (опрос не удался: {type(e).__name__}: {str(e)[:80]})")
            sess.close()
            continue
        finally:
            pass
        for r in rows:
            if r[1] in ("completed", "failed", "error"):
                name = pending.pop(r[0], "")
                took = time.time() - dict((d, t) for d, _, t in started)[r[0]]
                print(f"\nИД {r[0][:8]} {name[:50]:<52}")
                print(f"  статус {r[1]} за {took:.0f} с; фрагментов {r[3]}; вид {r[4]}; тема {r[5]}")
                print(f"  название: {r[6][:80]}")
                if r[7]:
                    print(f"  ОШИБКА: {r[7][:200]}")
                # Таблицы и векторы — отдельными запросами: «обработано» не значит «всё на месте».
                try:
                    rec = sess.execute(text(
                        "select count(*), coalesce(sum(rows_count),0) from document_tables "
                        "where document_id = :d"), {"d": r[0]}).first()
                    print(f"  таблиц {rec[0]}, строк в них {rec[1]}")
                except Exception as e:  # noqa: BLE001
                    print(f"  таблицы: запрос не удался ({type(e).__name__})")
        sess.close()
    if pending:
        print(f"\nне дождались завершения: {list(pending.values())}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
