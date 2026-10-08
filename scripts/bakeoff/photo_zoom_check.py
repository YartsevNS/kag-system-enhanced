"""Разбор: почему уменьшение снимка ломает распознавание и что безопасно.

Первый замер показал: пересохранение снимка в JPEG q90 без уменьшения пикселей сохраняет текст
полностью (5,21 → 3,02 МБ), а любое уменьшение длинной стороны резко портит результат на одном
из снимков. Здесь смотрим по строкам, что именно теряется, и сравниваем фильтры уменьшения
(LANCZOS / BICUBIC / BOX) — это отвечает на вопрос, виноват размер или способ уменьшения.

Запуск внутри контейнера api:
  docker cp photo_zoom_check.py kag-api:/tmp/ && docker exec kag-api python /tmp/photo_zoom_check.py
"""
from __future__ import annotations

import io
import pathlib
import re
import time

UPLOADS = pathlib.Path("/app/data/uploads")
NUM = re.compile(r"\d+(?:[.,]\d+)?")
SIZES = [None, 3600, 3200, 3000, 2500, 2000]
FILTERS = ["LANCZOS", "BICUBIC", "BOX"]
QUALITY = 90


def main() -> None:
    import numpy as np
    from PIL import Image, ImageOps
    from rapidocr import RapidOCR
    from rapidocr.utils.typings import LangRec, ModelType, OCRVersion

    photos = sorted([p for p in UPLOADS.glob("*.jpg")], key=lambda p: p.stat().st_size, reverse=True)[:2]
    eng = RapidOCR(params={"Rec.ocr_version": OCRVersion.PPOCRV5, "Rec.lang_type": LangRec.CYRILLIC,
                           "Rec.model_type": ModelType.MOBILE})

    def run(pil_img) -> tuple[list[str], float]:
        arr = np.array(pil_img.convert("RGB"))[:, :, ::-1]
        t0 = time.time()
        res = eng(arr)
        texts = [t for t in (res.txts or []) if t] if res is not None else []
        return texts, time.time() - t0

    for photo in photos:
        img = Image.open(photo)
        print("=" * 96)
        print(f"СНИМОК {photo.name} | {photo.stat().st_size / 1e6:.2f} МБ | {img.width}x{img.height}")
        base_lines, took = run(ImageOps.exif_transpose(img))
        base_nums = NUM.findall(" ".join(base_lines))
        print(f"  ЭТАЛОН (без изменений): строк {len(base_lines)}, чисел {len(base_nums)}, {took:.1f} с")
        print(f"    первые строки: {base_lines[:3]}")
        print(f"    числа: {' '.join(base_nums)}")

        for name in FILTERS:
            for size in SIZES:
                if size is None and name != FILTERS[0]:
                    continue
                fixed = ImageOps.exif_transpose(img)
                if size and max(fixed.size) > size:
                    k = size / max(fixed.size)
                    fixed = fixed.resize((max(1, round(fixed.width * k)), max(1, round(fixed.height * k))),
                                         getattr(Image, name if name != "BOX" else "BOX"))
                buf = io.BytesIO()
                fixed.convert("RGB").save(buf, format="JPEG", quality=QUALITY, optimize=True)
                lines, took = run(fixed)
                nums = NUM.findall(" ".join(lines))
                import difflib
                ratio = difflib.SequenceMatcher(None, " ".join(base_lines), " ".join(lines)).ratio()
                mark = "СОВПАЛО" if nums == base_nums else "отличается"
                print(f"  {name:8} {str(size or 'как есть'):>8} px, {len(buf.getvalue()) / 1e6:5.2f} МБ | "
                      f"строк {len(lines):>3} | чисел {len(nums):>3} {mark} | текст {ratio * 100:5.1f}% | {took:4.1f} с")
                if nums != base_nums:
                    print(f"        числа варианта: {' '.join(nums)}")


if __name__ == "__main__":
    main()
