"""Замер службы OCR на 41 через её же API: время, строки, числа (для сравнения CPU и OpenVINO)."""
import base64
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

import fitz

IMGS = Path("/home/yartsevn/table-bakeoff/img")
PAGES = [
    ("накладная", "9e116f5c-edee-4574-a854-5361be9be855skan-nakladnoj3.png"),
    ("смета", "smeta.pdf"),
]
NUM = re.compile(r"\d[\d\s]*(?:[.,]\d+)?")
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8020
URL = f"http://127.0.0.1:{PORT}/ocr?lang=cyrillic"


def page_png(name: str) -> bytes:
    p = IMGS / name
    if p.suffix.lower() == ".pdf":
        return fitz.open(str(p))[0].get_pixmap(dpi=200).tobytes("png")
    return p.read_bytes()


def call(png: bytes):
    # контракт службы: /ocr принимает СЫРЫЕ байты PNG (Content-Type: image/png), не JSON
    req = urllib.request.Request(URL, data=png, headers={"Content-Type": "image/png"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=600) as r:
        data = json.loads(r.read().decode())
    return time.time() - t0, data


def texts_of(data) -> list[str]:
    lines = data.get("lines")
    if isinstance(lines, list):
        return [str(x.get("text") or "") if isinstance(x, dict) else str(x) for x in lines]
    for key in ("texts", "result", "rows"):
        v = data.get(key)
        if isinstance(v, list):
            return [t if isinstance(t, str) else (t.get("text") or "") for t in v]
    return []


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "?"
    print(f"=== режим службы: {mode} ===")
    for label, f in PAGES:
        png = page_png(f)
        secs, data = call(png)
        ts = [t for t in texts_of(data) if t and t.strip()]
        nums = [m for t in ts for m in NUM.findall(t)]
        print(f"   {label:10} {secs:5.1f} с | строк {len(ts):4} | чисел {len(nums):4} | "
              f"ключи ответа {sorted(data.keys())[:5]}")


if __name__ == "__main__":
    main()
