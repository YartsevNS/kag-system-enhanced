"""Сторона облака: один вопрос -> облачный порядок фрагментов + их тексты (JSON в stdout).

Запуск внутри контейнера api (там ключи и доступ к своей же службе):
    docker exec kag-api python /tmp/cmp_fetch.py "текст вопроса" [limit]
"""

import json
import os
import sys

import requests

Q = sys.argv[1] if len(sys.argv) > 1 else "Требования к защите персональных данных"
LIMIT = int(sys.argv[2]) if len(sys.argv) > 2 else 8
BASE = "http://127.0.0.1:8000"

s = requests.Session()
user = os.environ.get("ADMIN_USERNAME", "admin")
pwd = os.environ.get("ADMIN_PASSWORD", "")
s.post(f"{BASE}/api/v1/auth/login", json={"username": user, "password": pwd}, timeout=30)
r = s.post(f"{BASE}/api/v1/chat/search", json={"query": Q, "limit": LIMIT}, timeout=120)
if r.status_code != 200:
    print(json.dumps({"error": f"поиск вернул {r.status_code}", "body": r.text[:200]}, ensure_ascii=False))
    raise SystemExit(1)
data = r.json()
items = data.get("chunks") or []
chunks = []
cloud = []
for c in items:
    cid = str(c.get("chunk_id") or c.get("id") or "")
    text = str(c.get("content") or c.get("text") or "")
    if not cid or not text:
        continue
    chunks.append({"chunk_id": cid, "text": text})
    cloud.append(cid)
out = {
    "question": Q,
    "model_cloud": "GigaChat EmbeddingsGigaR (2560)",
    "cloud_top": cloud,
    "chunks": chunks,
}
print(json.dumps(out, ensure_ascii=False))
