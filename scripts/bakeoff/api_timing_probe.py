"""Замер времени ответа админских ручек, от которых зависит страница Docker.

Зачем: страница держала панели в состоянии «Загрузка…», потому что ждала самый медленный запрос.
Здесь видно, сколько именно идёт каждый, и помогает ли фоновый снимок.

Запуск в контейнере api:
  docker cp api_timing_probe.py kag-api:/tmp/ && docker exec kag-api python /tmp/api_timing_probe.py
"""
from __future__ import annotations

import json
import os
import time
import urllib.request

API = "http://127.0.0.1:8000/api/v1"
PATHS = ["/admin/models/system/info", "/admin/models/storage/raw", "/admin/models/docker/stats"]


def main() -> None:
    password = os.environ.get("ADMIN_PASSWORD", "")
    req = urllib.request.Request(API + "/auth/login",
                                data=json.dumps({"username": "admin", "password": password}).encode(),
                                headers={"Content-Type": "application/json"}, method="POST")
    cookie = urllib.request.urlopen(req, timeout=30).headers.get("Set-Cookie", "").split(";")[0]
    for path in PATHS:
        for attempt in (1, 2):
            t0 = time.time()
            r = urllib.request.Request(API + path, headers={"Cookie": cookie})
            data = json.load(urllib.request.urlopen(r, timeout=180))
            dt = time.time() - t0
            size = len(json.dumps(data)) if isinstance(data, (dict, list)) else 0
            print(f"{path:34} попытка {attempt}: {dt:6.2f} с, {size} байт ответа")


if __name__ == "__main__":
    main()
