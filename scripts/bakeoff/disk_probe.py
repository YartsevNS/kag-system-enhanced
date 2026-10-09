"""Диагностика дисков: что именно показывает страница Docker и что видит служба отчётов.

Повод: на странице Docker строка «/app/data» показывала 98%, при этом каталог занимает 538 МБ,
а корневой диск сервера занят на 57%. Нужно понять, откуда берётся процент.

Запуск в контейнере api:
  docker cp disk_probe.py kag-api:/tmp/ && docker exec kag-api python /tmp/disk_probe.py
"""
from __future__ import annotations

import json
import os
import urllib.request


def main() -> None:
    # 1) что видит psutil внутри контейнера
    import psutil
    print("=== psutil.disk_partitions (глазами контейнера) ===")
    for part in psutil.disk_partitions(all=False):
        try:
            u = psutil.disk_usage(part.mountpoint)
        except Exception as e:
            print(f"  {part.mountpoint:22} ошибка: {e}")
            continue
        print(f"  {part.device:28} {part.mountpoint:22} {part.fstype:8} "
              f"{u.used / 1e9:6.1f}/{u.total / 1e9:6.1f} ГБ = {u.percent:5.1f}%")

    # 2) что отдают оба дисковых эндпоинта админки (один из них кормит страницу Docker)
    api = "http://127.0.0.1:8000/api/v1"
    password = os.environ.get("ADMIN_PASSWORD", "")
    cookie = ""
    if password:
        req = urllib.request.Request(
            f"{api}/auth/login",
            data=json.dumps({"username": "admin", "password": password}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        cookie = urllib.request.urlopen(req, timeout=30).headers.get("Set-Cookie", "").split(";")[0]

    for path in ("/admin/disk-usage", "/admin/models/system/disk"):
        try:
            req = urllib.request.Request(f"{api}{path}", headers={"Cookie": cookie})
            data = json.load(urllib.request.urlopen(req, timeout=60))
        except Exception as e:
            print(f"\n=== {path}: ошибка {type(e).__name__}: {e}")
            continue
        print(f"\n=== {path} ===")
        rows = data.get("disks") if isinstance(data, dict) else None
        if rows is None and isinstance(data, dict):
            rows = data.get("data")
        if isinstance(rows, list):
            for dd in rows:
                if isinstance(dd, dict):
                    print(f"  {str(dd.get('device'))[:26]:28} {str(dd.get('mountpoint'))[:20]:22} "
                          f"{dd.get('used')} / {dd.get('total')} = {dd.get('percent')}%")
                else:
                    print("  ", dd)
        else:
            print("  ", json.dumps(data, ensure_ascii=False)[:500])

    # 3) что соберёт служба отчётов (она использует psutil и docker)
    print("\n=== служба отчётов: показатель «диски» ===")
    try:
        from src.api.services.report_service import report_service
        for sec in report_service.collect(["disks", "docker"]):
            print(f"— {sec['title']}" + (f" (ошибка: {sec['error']})" if sec.get("error") else ""))
            for label, value in sec["rows"]:
                print(f"    {label}: {value}")
    except Exception as e:
        print(f"  служба отчётов недоступна: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
