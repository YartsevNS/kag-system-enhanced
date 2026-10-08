"""Сколько пикселей нужно снимку с телефона, чтобы распознавание не пострадало.

Задача: снимки с телефона приходят по 5–8 МБ, для распознавания это избыточно. Нужно найти
размер (длинная сторона в пикселях) и качество JPEG, при которых распознанный текст и числа
не отличаются от полноразмерного снимка. Мерим именно ЧИСЛА отдельно от текста: в документах
важна каждая цифра, а текст терпим к мелким ошибкам.

Что делает:
  1. Берёт самые большие фотографии из /app/data/uploads (настоящие снимки владельца).
  2. Для каждой готовит варианты: длинная сторона 3000/2500/2000/1600/1200 px, качество JPEG 85 и 90,
     применяя поворот по EXIF (иначе телефонный снимок сохранится боком).
  3. Прогоняет распознавание (PP-OCRv5, кириллица — как в проде) на каждом варианте и на оригинале.
  4. Сравнивает с оригиналом: набор чисел, число строк, схожесть текста, время распознавания.

Запуск внутри контейнера api (там и фотографии, и распознавание):
  docker cp photo_size_check.py kag-api:/tmp/ && docker exec kag-api python /tmp/photo_size_check.py
"""
from __future__ import annotations

import io
import pathlib
import re
import time

UPLOADS = pathlib.Path("/app/data/uploads")
SIZES = [None, 3000, 2500, 2000, 1600, 1200]      # None = оригинал как есть
QUALITY = [90, 85]
NUM = re.compile(r"\d+(?:[.,]\d+)?")


def pick_photos(limit: int = 2) -> list[pathlib.Path]:
    """Самые большие jpg/png в загрузках — это и есть снимки с телефона."""
    files = [p for p in UPLOADS.glob("*") if p.suffix.lower() in (".jpg", ".jpeg", ".png")]
    return sorted(files, key=lambda p: p.stat().st_size, reverse=True)[:limit]


def prepare(img, long_side: int | None, quality: int):
    """Вариант снимка: поворот по EXIF, уменьшение длинной стороны, сохранение в JPEG."""
    from PIL import Image, ImageOps

    fixed = ImageOps.exif_transpose(img)          # обязательно: иначе снимок сохранится боком
    if long_side and max(fixed.size) > long_side:
        k = long_side / max(fixed.size)
        fixed = fixed.resize((max(1, round(fixed.width * k)), max(1, round(fixed.height * k))),
                             Image.LANCZOS)
    buf = io.BytesIO()
    fixed.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
    return fixed, buf.getvalue()


def main() -> None:
    from PIL import Image
    from rapidocr import RapidOCR
    from rapidocr.utils.typings import LangRec, ModelType, OCRVersion

    photos = pick_photos()
    if not photos:
        print("в /app/data/uploads нет фотографий")
        return

    eng = RapidOCR(params={"Rec.ocr_version": OCRVersion.PPOCRV5, "Rec.lang_type": LangRec.CYRILLIC,
                           "Rec.model_type": ModelType.MOBILE})

    def recognize(pil_img):
        import numpy as np
        arr = np.array(pil_img.convert("RGB"))[:, :, ::-1]      # движок ждёт BGR
        started = time.time()
        res = eng(arr)
        return res, time.time() - started

    verdicts = []
    for photo in photos:
        print("=" * 92)
        img = Image.open(photo)
        print(f"СНИМОК: {photo.name} | файл {photo.stat().st_size / 1e6:.2f} МБ | "
              f"{img.width}x{img.height} px | EXIF-поворот: {img.getexif().get(274, 1)}")

        base_img, base_bytes = prepare(img, None, 95)
        base_res, base_time = recognize(base_img)

        def lines_of(res) -> list[str]:
            """Строки распознавания: результат rapidocr хранит текст в .txts."""
            if res is None or getattr(res, "txts", None) is None:
                return []
            return [t for t in res.txts if t]

        base_lines = lines_of(base_res)
        base_text = " ".join(base_lines)
        base_nums = sorted(NUM.findall(base_text))
        print(f"  эталон: {base_img.width}x{base_img.height}, {base_bytes / 1e6:.2f} МБ, "
              f"строк {len(base_lines)}, чисел {len(base_nums)}, распознавание {base_time:.1f} с")

        for quality in QUALITY:
            for size in SIZES:
                if size is None and quality != QUALITY[0]:
                    continue
                variant, payload = prepare(img, size, quality)
                res, took = recognize(variant)
                lines = lines_of(res)
                text = " ".join(lines)
                nums = sorted(NUM.findall(text))
                same_nums = sum(1 for a, b in zip(base_nums, nums) if a == b)
                import difflib
                ratio = difflib.SequenceMatcher(None, base_text, text).ratio()
                print(f"  длинная сторона {str(size or 'как есть'):>7} px, JPEG {quality}: "
                      f"{variant.width}x{variant.height}, {len(payload) / 1e6:5.2f} МБ | "
                      f"строк {len(lines):>3} | чисел {len(nums):>3} (совпало {same_nums}) | "
                      f"текст {ratio * 100:5.1f}% | {took:5.1f} с")
                verdicts.append((size, quality, len(payload), same_nums, len(base_nums), ratio))

    print("=" * 92)
    print("ИТОГ: наименьший размер, где числа совпали полностью и текст не хуже 98%:")
    good = [v for v in verdicts if v[4] and v[3] == v[4] and v[5] >= 0.98]
    if not good:
        print("  такого размера в переборе нет — числа теряются даже на крупных вариантах")
    else:
        best = min(good, key=lambda v: (v[2], v[0] or 10 ** 6))
        print(f"  длинная сторона {best[0]} px, JPEG {best[1]}: файл {best[2] / 1e6:.2f} МБ, "
              f"числа {best[3]}/{best[4]}, текст {best[5] * 100:.1f}%")


if __name__ == "__main__":
    main()
