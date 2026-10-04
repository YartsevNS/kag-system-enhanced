"""Смена настроек OCR-категории через config_store с проверкой записи и чтения обратно.

Запускать внутри контейнера:
    docker exec -i kag-api python /tmp/set_ocr_settings.py detector_enabled=true detector_path=/app/models/pp_doclayout_v3.onnx
Значения: true/false → bool, число → int/float, остальное → строка.
В вывод печатается только изменённое (без секретов).
"""
import sys

from src.api.services.config_store import config_store


def coerce(raw: str):
    low = raw.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def main() -> None:
    if not sys.argv[1:]:
        raise SystemExit("укажите пары ключ=значение")
    cfg = dict(config_store.get("ocr", "settings") or {})
    for pair in sys.argv[1:]:
        key, _, raw = pair.partition("=")
        cfg[key.strip()] = coerce(raw)
    ok = config_store.set("ocr", "settings", cfg)
    back = config_store.get("ocr", "settings") or {}
    print(f"запись принята: {ok}")
    for pair in sys.argv[1:]:
        key = pair.partition("=")[0].strip()
        print(f"  {key} = {back.get(key)!r}")
    print("OK" if ok else "ПРОВАЛ: настройка не сохранилась")


if __name__ == "__main__":
    main()
