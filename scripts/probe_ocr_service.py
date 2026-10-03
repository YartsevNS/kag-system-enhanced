"""Живая проверка службы OCR: строки страницы (/ocr) и текст ячеек (/cells).

Запуск на сервере моделей:
    ./.venv/bin/python probe_ocr_service.py [url] [image]
"""
import base64
import json
import sys
import time
import urllib.request

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8020"
IMG = sys.argv[2] if len(sys.argv) > 2 else "/home/yartsevn/ocr-bakeoff/naklad.png"


def post(path: str, body: bytes, content_type: str):
    request = urllib.request.Request(URL + path, body, {"Content-Type": content_type})
    with urllib.request.urlopen(request, timeout=600) as response:
        return json.loads(response.read())


def main() -> int:
    with urllib.request.urlopen(URL + "/health", timeout=15) as response:
        print("health:", response.read().decode())
    data = open(IMG, "rb").read()

    started = time.time()
    out = post("/ocr", data, "image/png")
    lines = out.get("lines") or []
    print(f"/ocr: {out.get('seconds')} с (всего {time.time() - started:.1f} с), строк {len(lines)}, "
          f"кириллических символов {out.get('cyrillic_chars')}")
    for line in lines[:5]:
        print("   ", line["text"][:70])

    quads = [line["quad"] for line in lines[:8] if line.get("quad")]
    payload = {"image_b64": base64.b64encode(data).decode(), "quads": quads}
    started = time.time()
    cells = post("/cells", json.dumps(payload).encode(), "application/json")
    texts = cells.get("texts") or []
    print(f"/cells: {cells.get('seconds')} с (всего {time.time() - started:.1f} с), вырезок {len(texts)}")
    for item in texts:
        print("   ", repr(str(item.get("text"))[:60]), round(float(item.get("confidence") or 0), 3))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
