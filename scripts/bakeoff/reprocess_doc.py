"""Запуск повторной обработки документа на стенде через штатный API (для замера шага parse).

Запускать ВНУТРИ контейнера kag-api: docker exec -i kag-api python /tmp/reprocess_doc.py <document_id>
Пароль админа берётся из окружения контейнера (ADMIN_PASSWORD), в вывод не печатается.
"""
import json
import os
import sys
import urllib.error
import urllib.request

CANDIDATES = ["http://127.0.0.1:8000/api/v1", "http://127.0.0.1:8080/api/v1", "http://127.0.0.1/api/v1"]


def pick_base() -> str:
    for base in CANDIDATES:
        try:
            with urllib.request.urlopen(f"{base}/health", timeout=5):
                return base
        except Exception:  # noqa: BLE001
            continue
    raise SystemExit("API не отвечает ни на одном из портов")


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("укажите document_id")
    doc = sys.argv[1]
    base = pick_base()
    user = os.environ.get("ADMIN_USERNAME") or "admin"
    pw = os.environ.get("ADMIN_PASSWORD") or ""
    if not pw:
        raise SystemExit("в окружении нет ADMIN_PASSWORD")
    body = json.dumps({"username": user, "password": pw}).encode()
    req = urllib.request.Request(f"{base}/auth/login", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        tok = json.loads(urllib.request.urlopen(req, timeout=30).read())["access_token"]
    except urllib.error.HTTPError as e:
        raise SystemExit(f"вход не удался: {e.code}") from e
    req2 = urllib.request.Request(f"{base}/upload/{doc}/reprocess-ocr", data=b"{}",
                                  headers={"Authorization": f"Bearer {tok}",
                                           "Content-Type": "application/json"}, method="POST")
    try:
        print("ответ:", json.loads(urllib.request.urlopen(req2, timeout=60).read()))
    except urllib.error.HTTPError as e:
        raise SystemExit(f"перезапуск не удался: {e.code} {e.read()[:200]!r}") from e


if __name__ == "__main__":
    main()
