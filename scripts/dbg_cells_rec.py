"""Диагностика распознавания вырезанных ячеек: почему пусто и что помогает.

Проверяем на реальном скане: берём рамки из /ocr, режем вырезки и распознаём
(1) полным движком (с детектором, как сейчас), (2) распознавателем без детектора.
"""
import inspect
import json
import sys
import urllib.request

import numpy as np
from PIL import Image
from rapidocr import RapidOCR
from rapidocr.utils.typings import LangRec, ModelType, OCRVersion

IMG = sys.argv[1] if len(sys.argv) > 1 else "/home/yartsevn/ocr-bakeoff/naklad.png"


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


def text_of(out):
    if out is None:
        return ""
    texts = getattr(out, "txts", None) or []
    return " | ".join(str(t) for t in texts)


def main() -> int:
    print("сигнатура RapidOCR:", inspect.signature(RapidOCR.__init__))
    arr = np.array(Image.open(IMG).convert("RGB"))
    data = open(IMG, "rb").read()
    request = urllib.request.Request("http://127.0.0.1:8020/ocr", data, {"Content-Type": "image/png"})
    lines = json.loads(urllib.request.urlopen(request, timeout=300).read())["lines"]
    quads = [l["quad"] for l in lines if l.get("quad")][:6]

    full = make_engine()
    print("\n=== полный движок (детектор + распознавание) ===")
    for quad in quads:
        piece = crop(arr, quad)
        if piece.size == 0:
            print("   пустая вырезка")
            continue
        print(f"   {piece.shape[1]}x{piece.shape[0]} -> {text_of(full(piece))!r}")

    for options in ({"Det.use_det": False, "Cls.use_cls": False}, {"use_det": False},
                    {"Global.use_det": False}):
        print(f"\n=== без детектора: {options} ===")
        try:
            rec = make_engine(**options)
        except Exception as e:  # noqa: BLE001 — вариант может не поддерживаться этой версией
            print("   конфигурация не принята:", type(e).__name__, str(e)[:120])
            continue
        for quad in quads[:3]:
            piece = crop(arr, quad)
            print(f"   {piece.shape[1]}x{piece.shape[0]} -> {text_of(rec(piece))!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
