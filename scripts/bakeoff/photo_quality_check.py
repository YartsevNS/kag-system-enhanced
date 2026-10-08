"""Замер: какое качество JPEG и цветовая выборка безопасны для распознавания.

Замер размеров показал, что уменьшать пиксели нельзя — теряются цифры. Значит экономить надо
качеством сжатия и метаданными, а не разрешением. Здесь перебираем качество и цветовую выборку
(chroma subsampling: 4:2:0 против 4:4:4 — 4:4:4 сохраняет мелкий цветной текст лучше) и смотрим,
где распознавание ещё не отличается от оригинала.

Запуск внутри контейнера api:
  docker cp photo_quality_check.py kag-api:/tmp/ && docker exec kag-api python /tmp/photo_quality_check.py
"""
from __future__ import annotations

import difflib
import io
import pathlib
import re
import time

UPLOADS = pathlib.Path("/app/data/uploads")
NUM = re.compile(r"\d+(?:[.,]\d+)?")
# (качество, цветовая выборка): 2 = 4:2:0 (по умолчанию у PIL), 0 = 4:4:4 (без потери цвета)
COMBOS = [(95, 2), (90, 2), (85, 2), (80, 2), (90, 0), (85, 0), (80, 0)]


def main() -> None:
    import numpy as np
    from PIL import Image, ImageOps
    from rapidocr import RapidOCR
    from rapidocr.utils.typings import LangRec, ModelType, OCRVersion

    photos = sorted([p for p in UPLOADS.glob("*.jpg")], key=lambda p: p.stat().st_size, reverse=True)[:2]
    eng = RapidOCR(params={"Rec.ocr_version": OCRVersion.PPOCRV5, "Rec.lang_type": LangRec.CYRILLIC,
                           "Rec.model_type": ModelType.MOBILE})

    def run(pil_img) -> list[str]:
        arr = np.array(pil_img.convert("RGB"))[:, :, ::-1]
        res = eng(arr)
        return [t for t in (res.txts or []) if t] if res is not None else []

    for photo in photos:
        img = Image.open(photo)
        fixed = ImageOps.exif_transpose(img)
        base_lines = run(fixed)
        base_nums = NUM.findall(" ".join(base_lines))
        base_text = " ".join(base_lines)
        print("=" * 96)
        print(f"СНИМОК {photo.name} | {photo.stat().st_size / 1e6:.2f} МБ | {img.width}x{img.height}")
        print(f"  ЭТАЛОН: строк {len(base_lines)}, чисел {len(base_nums)}")
        print(f"    числа эталона: {' '.join(base_nums)}")

        for quality, subsampling in COMBOS:
            buf = io.BytesIO()
            fixed.convert("RGB").save(buf, format="JPEG", quality=quality,
                                      optimize=True, subsampling=subsampling)
            payload = buf.getvalue()
            lines = run(fixed)
            nums = NUM.findall(" ".join(lines))
            ratio = difflib.SequenceMatcher(None, base_text, " ".join(lines)).ratio()
            chroma = "4:4:4" if subsampling == 0 else "4:2:0"
            mark = "числа совпали" if nums == base_nums else "ЧИСЛА ОТЛИЧАЮТСЯ"
            print(f"  качество {quality}, {chroma}: {len(payload) / 1e6:5.2f} МБ | "
                  f"строк {len(lines):>3} | чисел {len(nums):>3} | текст {ratio * 100:5.1f}% | {mark}")
            if nums != base_nums:
                print(f"      числа варианта: {' '.join(nums)}")
            # сравнение по существу: цифры длиной от 2 знаков (суммы, даты, номера) — их и нельзя терять
            strong = lambda xs: {x for x in xs if len(x.replace('.', '').replace(',', '')) >= 2}
            keep = len(strong(base_nums) & strong(nums)) / max(1, len(strong(base_nums))) * 100
            print(f"      значимые числа сохранены: {keep:.0f}%")


if __name__ == "__main__":
    main()
