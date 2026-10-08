"""Показать сохранённые строки распознавания последнего снимка (для проверки качества).

Запуск в контейнере api:
  docker cp show_ocr_lines.py kag-api:/tmp/ && docker exec kag-api python /tmp/show_ocr_lines.py
"""
from __future__ import annotations

import glob
import json
import pathlib

UPLOADS = pathlib.Path("/app/data/uploads")
OCR = pathlib.Path("/app/data/ocr_results")


def main() -> None:
    imgs = [p for p in UPLOADS.glob("*") if p.suffix.lower() in (".jpg", ".jpeg", ".png")]
    if not imgs:
        print("нет снимков")
        return
    latest = max(imgs, key=lambda p: p.stat().st_mtime)
    doc_id = latest.name.split("_")[0]
    print(f"документ: {latest.name} | {latest.stat().st_size / 1e6:.2f} МБ")
    found = False
    for j in sorted(OCR.glob(f"{doc_id}*.lines.json")):
        found = True
        d = json.load(open(j))
        lines = d.get("lines", [])
        print(f"файл строк: {pathlib.Path(j).name} | строк {len(lines)} | "
              f"страница {d.get('width')}x{d.get('height')}")
        for i, l in enumerate(lines, 1):
            print(f"{i:3}. {l.get('text', '')[:120]}")
    if not found:
        print("сохранённых строк нет — распознаю сам (тот же движок, что в системе)")
        import numpy as np
        from PIL import Image, ImageOps
        from rapidocr import RapidOCR
        from rapidocr.utils.typings import LangRec, ModelType, OCRVersion

        eng = RapidOCR(params={"Rec.ocr_version": OCRVersion.PPOCRV5, "Rec.lang_type": LangRec.CYRILLIC,
                               "Rec.model_type": ModelType.MOBILE})
        img = ImageOps.exif_transpose(Image.open(latest))
        res = eng(np.array(img.convert("RGB"))[:, :, ::-1])
        lines = [t for t in (res.txts or []) if t] if res is not None else []
        print(f"строк {len(lines)}")
        for i, t in enumerate(lines, 1):
            print(f"{i:3}. {t[:120]}")


if __name__ == "__main__":
    main()
