"""Смена адреса OCR-службы в настройках через config_store (с проверкой записи и чтения обратно).

Запускать внутри контейнера: docker exec -i kag-api python /tmp/set_ocr_url.py http://192.168.50.41:8021
"""
import sys

from src.api.services.config_store import config_store


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("укажите новый URL службы")
    new = sys.argv[1].rstrip("/")
    cfg = dict(config_store.get("ocr", "settings") or {})
    old = cfg.get("service_url")
    cfg["service_url"] = new
    ok = config_store.set("ocr", "settings", cfg)
    back = (config_store.get("ocr", "settings") or {}).get("service_url")
    print(f"было: {old}")
    print(f"запись принята: {ok}")
    print(f"прочитано обратно: {back}")
    print("OK" if ok and back == new else "ПРОВАЛ: настройка не сохранилась")


if __name__ == "__main__":
    main()
