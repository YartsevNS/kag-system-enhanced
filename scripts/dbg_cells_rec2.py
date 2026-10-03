"""Диагностика ячеек, шаг 2: увеличение вырезки перед распознаванием.

Первая проверка показала: на вырезке высотой 13–15 px и детектор, и распознавание почти всегда пусты.
Причина — размер, а не язык. Здесь проверяем масштабы ×2..×4 на одном и том же наборе вырезок.
"""
import json
import sys
import urllib.request

import cv2
import numpy as np
from PIL import Image
from rapidocr import RapidOCR
from rapidocr.utils.typings import LangRec, ModelType, OCRVersion

IMG = sys.argv[1] if len(sys.argv) > 1 else "/home/yartsevn/ocr-bakeoff/naklad.png"
TARGET_H = 48   # целевая высота строки для распознавания


def make_engine(**extra):
    params = {"Rec.ocr_version": OCRVersion.PPOCRV5, "Rec.lang_type": LangRec.CYRILLIC,
              "Rec.model_type": ModelType.MOBILE}
    params.update(extra)
    return RapidOCR(params=params)


def crop(arr, quad):
    points = np.asarray(quad, dtype=float).reshape(-1, 2)
    x0 = max(0, int(points[:, 0].min()))
    x1 = min(arr.shape[1], int(np.ceil(points[:, 0].max())))
    y0 = max(0, int(points[:, 1].min()))
    y1 = min(arr.shape[0], int(np.ceil(points[:, 1].max())))
    return arr[y0:y1, x0:x1]


def upscale(piece, factor):
    if factor <= 1.0:
        return piece
    return cv2.resize(piece, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC)


def text_of(out):
    if out is None:
        return ""
    return " | ".join(str(t) for t in (getattr(out, "txts", None) or []))


def main() -> int:
    arr = np.array(Image.open(IMG).convert("RGB"))
    data = open(IMG, "rb").read()
    request = urllib.request.Request("http://127.0.0.1:8020/ocr", data, {"Content-Type": "image/png"})
    lines = json.loads(urllib.request.urlopen(request, timeout=300).read())["lines"]
    quads = [l["quad"] for l in lines if l.get("quad")][:5]
    # Контрольные числа накладной: по ним видно, стало ли распознавание полезным.
    control = ["13959.9", "16751.90", "45305.47", "43250.36"]

    engines = {
        "полный": make_engine(),
        "без детектора": make_engine(**{"Global.use_det": False}),
    }
    for name, engine in engines.items():
        for factor in (1.0, 2.0, 3.0, 4.0):
            pieces = [upscale(crop(arr, q), factor) for q in quads]
            pieces = [p for p in pieces if p.size]
            texts = [text_of(engine(p)) for p in pieces]
            joined = " ".join(texts)
            clean = joined.replace(",", ".")
            hits = sum(1 for c in control if c in clean)
            print(f"[{name} ×{factor}] высоты {[p.shape[0] for p in pieces]} "
                  f"непустых {sum(1 for t in texts if t.strip())}/{len(texts)}; текст: {texts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
