"""Замер на последнем загруженном документе (снимок текста): сколько можно сжать без потери чисел.

Документ уже прошёл нормализацию системы (q85, 4:4:4, без уменьшения пикселей), то есть на диске
лежит ровно то, что распознаётся. Здесь:
  1. показываем факты по файлу: вес, размеры, поворот по EXIF, что система уже распознала;
  2. перебираем дальнейшее сжатие (качество и формат) и уменьшение пикселей до 80% и 60%,
     распознавая ИМЕННО декодированный файл (исправленная методика);
  3. сравниваем числа и строки с текущим состоянием — это и есть запас прочности.

Запуск внутри контейнера api:
  docker cp photo_last_doc_check.py kag-api:/tmp/ && docker exec kag-api python /tmp/photo_last_doc_check.py
"""
from __future__ import annotations

import glob
import io
import json
import os
import pathlib
import re

UPLOADS = pathlib.Path("/app/data/uploads")
OCR = pathlib.Path("/app/data/ocr_results")
NUM = re.compile(r"\d+(?:[.,]\d+)?")


def save_jpeg(img, quality: int) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True, subsampling=0)
    return buf.getvalue()


def save_webp(img, quality: int) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="WEBP", quality=quality, method=6)
    return buf.getvalue()


def scale(img, k: float):
    from PIL import Image
    return img.resize((max(1, round(img.width * k)), max(1, round(img.height * k))), Image.LANCZOS)


def main() -> None:
    import numpy as np
    from PIL import Image, ImageOps
    from rapidocr import RapidOCR
    from rapidocr.utils.typings import LangRec, ModelType, OCRVersion

    imgs = [p for p in UPLOADS.glob("*") if p.suffix.lower() in (".jpg", ".jpeg", ".png")]
    if not imgs:
        print("нет снимков в uploads")
        return
    latest = max(imgs, key=lambda p: p.stat().st_mtime)
    doc_id = latest.name.split("_")[0]

    raw = Image.open(latest)
    print("=" * 96)
    print(f"ФАЙЛ на диске: {latest.name}")
    print(f"  вес {latest.stat().st_size / 1e6:.2f} МБ | размер {raw.width}x{raw.height} "
          f"| EXIF-поворот {raw.getexif().get(274, 1)}")
    base_img = ImageOps.exif_transpose(raw)
    print(f"  после поворота: {base_img.width}x{base_img.height}")

    for j in sorted(OCR.glob(f"{doc_id}*.lines.json")):
        d = json.load(open(j))
        lines = d.get("lines", [])
        print(f"  распознано системой: строк {len(lines)} | страница {d.get('width')}x{d.get('height')}")
        print("    первые строки: " + " | ".join(l.get("text", "")[:28] for l in lines[:4]))

    eng = RapidOCR(params={"Rec.ocr_version": OCRVersion.PPOCRV5, "Rec.lang_type": LangRec.CYRILLIC,
                           "Rec.model_type": ModelType.MOBILE})

    def ocr(img_obj) -> list[str]:
        arr = np.array(img_obj.convert("RGB"))[:, :, ::-1]
        res = eng(arr)
        return [t for t in (res.txts or []) if t] if res is not None else []

    base_lines = ocr(base_img)
    base_nums = set(NUM.findall(" ".join(base_lines)))
    base_text = " ".join(base_lines)
    print(f"\nЭТАЛОН (то, что лежит на диске): строк {len(base_lines)}, чисел {len(base_nums)}")
    print(f"  числа: {' '.join(sorted(base_nums))}")

    variants = [
        ("JPEG 95", lambda im: save_jpeg(im, 95)),
        ("JPEG 90", lambda im: save_jpeg(im, 90)),
        ("JPEG 85 (текущее)", lambda im: save_jpeg(im, 85)),
        ("JPEG 80", lambda im: save_jpeg(im, 80)),
        ("JPEG 70", lambda im: save_jpeg(im, 70)),
        ("WebP 95", lambda im: save_webp(im, 95)),
        ("WebP 90", lambda im: save_webp(im, 90)),
        ("пиксели 80% + JPEG 85", lambda im: save_jpeg(scale(im, 0.8), 85)),
        ("пиксели 60% + JPEG 85", lambda im: save_jpeg(scale(im, 0.6), 85)),
    ]

    print("\nто же изображение, сжатое сильнее (распознаём декодированный файл):")
    best_ok = None
    for label, make in variants:
        payload = make(base_img)
        lines = ocr(Image.open(io.BytesIO(payload)))
        nums = set(NUM.findall(" ".join(lines)))
        import difflib
        lost = sorted(base_nums - nums)
        extra = sorted(nums - base_nums)
        ratio = difflib.SequenceMatcher(None, base_text, " ".join(lines)).ratio()
        if nums == base_nums and len(lines) == len(base_lines) and ratio > 0.995:
            verdict = "совпало с текущим"
            # берём самый компактный вариант, а не первый попавшийся
            if best_ok is None or len(payload) < best_ok[1]:
                best_ok = (label, len(payload))
        else:
            verdict = f"расхождение: строк {len(lines)}/{len(base_lines)}, текст {ratio * 100:.1f}%, " \
                      f"числа потеряны {lost or '—'}, лишние {extra or '—'}"
        print(f"  {label:22} {len(payload) / 1e6:6.3f} МБ | строк {len(lines):>3}/{len(base_lines)} "
              f"| текст {ratio * 100:5.1f}% | {verdict}")

    if best_ok:
        print(f"\nвывод: самый компактный вариант, где числа и строки не изменились — "
              f"{best_ok[0]} ({best_ok[1] / 1e6:.2f} МБ)")
    else:
        print("\nвывод: ни один вариант не сохранил числа полностью — значит запас исчерпан, "
              "и то, что лежит на диске, уже на границе")


if __name__ == "__main__":
    main()
