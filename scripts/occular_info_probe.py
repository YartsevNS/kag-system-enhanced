"""Проверка готовности весов Occular (как в README: model_info()) — что есть локально, что докачивать.

Запуск в контейнере прода:
    docker exec kag-api python /app/data/occular_info_probe.py
"""
import os
import sys

try:
    import occular
    print(f"версия occular: {getattr(occular, '__version__', '?')}")
except Exception as e:  # noqa: BLE001
    print(f"occular не импортируется: {type(e).__name__}: {e}")
    sys.exit(1)

try:
    from occular import model_info
    print("--- model_info() (как в README) ---")
    model_info()
except Exception as e:  # noqa: BLE001
    print(f"model_info недоступен: {type(e).__name__}: {e}")

print("--- что публикует модуль весов ---")
try:
    from occular import model_files as m
    for name in sorted(dir(m)):
        if name.startswith("_"):
            continue
        val = getattr(m, name)
        if isinstance(val, (str, int, float, tuple, list, dict)):
            short = str(val)
            print(f"  {name} = {short[:160]}")
except Exception as e:  # noqa: BLE001
    print(f"  недоступно: {type(e).__name__}: {e}")

print("--- офлайн-готовность весов таблиц и порядка чтения ---")
os.environ["HF_HUB_OFFLINE"] = "1"
try:
    from huggingface_hub import hf_hub_download
except Exception as e:  # noqa: BLE001
    print(f"  huggingface_hub недоступен: {e}")
    hf_hub_download = None

candidates = []
try:
    from occular import model_files as m
    for name in dir(m):
        val = getattr(m, name)
        if isinstance(val, str) and (".onnx" in val or ".pt" in val or ".npz" in val):
            candidates.append((name, val))
except Exception:  # noqa: BLE001
    pass

if hf_hub_download:
    for name, fname in candidates:
        try:
            path = hf_hub_download("Shivin11/occular-ocr", fname)
            print(f"  есть локально: {name} -> {os.path.basename(path)}")
        except Exception as e:  # noqa: BLE001
            print(f"  НЕТ локально: {name} ({fname}) — {type(e).__name__}")

print("--- доступные имена моделей ---")
for attr in ("MODELS", "READING_ORDER_DIR"):
    if hasattr(m, attr):
        print(f"  {attr}: {getattr(m, attr)}")
