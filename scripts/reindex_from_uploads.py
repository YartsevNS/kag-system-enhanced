#!/usr/bin/env python3
"""Переиндексация подмножества документов из каталога uploads.

Зачем: файлы в ./data/uploads сохранились, а записи в БД документов почти пусты
(была очистка), поэтому штатный reindex по document_id не работает, а повторная
загрузка создаёт НОВЫЙ document_id — старые чанки в Qdrant/Neo4j остаются
и дают дубли в поиске. Скрипт делает это аккуратно: загружает файл заново,
дожидается обработки и удаляет данные старого document_id.

Использование (на сервере 18):
    python3 scripts/reindex_from_uploads.py \
        --token "$TOKEN" --pattern 'gost|sto_br|barter|Инфляция|Цифровая|текущей' \
        --limit 10

    # посмотреть, что будет сделано, ничего не меняя:
    python3 scripts/reindex_from_uploads.py --token "$TOKEN" --pattern gost --dry-run

Скрипт только stdlib. Neo4j чистится через `docker exec <container> cypher-shell`
(по умолчанию kag-neo4j); Qdrant — через HTTP API.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Optional

UUID_RE = re.compile(r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})_")


def _http(url: str, payload: Optional[dict] = None, token: Optional[str] = None,
          method: Optional[str] = None, timeout: float = 180.0) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode()
    return json.loads(body) if body else {}


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

    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def wait_completed(base: str, doc_id: str, token: str, max_wait: float = 3600.0) -> str:
    start = time.time()
    last = ""
    while time.time() - start < max_wait:
        st = _http(f"{base}/api/v1/upload/{doc_id}/status", token=token)
        status = st.get("status") or ""
        if status != last:
            print(f"      статус: {status} ({st.get('progress')})", flush=True)
            last = status
        if status in ("completed", "failed"):
            return status
        time.sleep(6)
    return "timeout"


def delete_qdrant(qdrant: str, api_key: str, document_id: str) -> bool:
    try:
        payload = {"filter": {"must": [{"key": "document_id", "match": {"value": document_id}}]}}
        _http(f"{qdrant}/collections/kag_documents/points/delete",
              payload, token=None, method="POST")
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
    ap.add_argument("--token", default="", help="Bearer-токен (обязателен вне --dry-run)")
    ap.add_argument("--uploads-dir", default="/home/yartsevn/kag-system/data/uploads")
    ap.add_argument("--pattern", default=".", help="regex по имени файла")
    ap.add_argument("--limit", type=int, default=0, help="0 = без ограничения")
    ap.add_argument("--qdrant-url", default="http://localhost:6333")
    ap.add_argument("--qdrant-key", default="")
    ap.add_argument("--neo4j-container", default="kag-neo4j")
    ap.add_argument("--neo4j-password", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", default="/tmp/reindex_subset.json")
    args = ap.parse_args()

    base = args.url.rstrip("/")
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
            _http(f"{base}/api/v1/upload/{new_id}/process", {}, args.token)
        except Exception as e:
            print(f"    process ошибка: {e}")
        status = wait_completed(base, new_id, args.token)
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
