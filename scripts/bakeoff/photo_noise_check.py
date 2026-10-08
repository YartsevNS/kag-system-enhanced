"""Проверка: насколько сам движок распознавания повторяем, и что это значит для сжатия.

Повод: при сквозной проверке нормализации один токен-число то появлялся, то исчезал. Прежде чем
делать выводы «сжатие потеряло число», нужно измерить собственный разброс движка:
один и тот же файл прогоняем несколько раз и смотрим, совпадают ли числа.

Затем тем же способом прогоняем варианты сжатия (без уменьшения пикселей) и сравниваем их разброс
с разбросом оригинала. Если разница варианта не выходит за разброс оригинала — сжатие безопасно.

Запуск внутри контейнера api:
  docker cp photo_noise_check.py kag-api:/tmp/ && docker exec kag-api python /tmp/photo_noise_check.py
"""
from __future__ import annotations

import io
import pathlib
import re

UPLOADS = pathlib.Path("/app/data/uploads")
NUM = re.compile(r"\d+(?:[.,]\d+)?")
RUNS = 3
VARIANTS = [("оригинал без изменений", None, None),
            ("JPEG 85, 4:4:4", 85, 0),
            ("JPEG 80, 4:4:4", 80, 0)]


def main() -> None:
    import numpy as np
    from PIL import Image, ImageOps
    from rapidocr import RapidOCR
    from rapidocr.utils.typings import LangRec, ModelType, OCRVersion

    photos = sorted([p for p in UPLOADS.glob("*.jpg")], key=lambda p: p.stat().st_size, reverse=True)[:2]
    eng = RapidOCR(params={"Rec.ocr_version": OCRVersion.PPOCRV5, "Rec.lang_type": LangRec.CYRILLIC,
                           "Rec.model_type": ModelType.MOBILE})

    def ocr(img_obj) -> list[str]:
        arr = np.array(img_obj.convert("RGB"))[:, :, ::-1]
        res = eng(arr)
        return [t for t in (res.txts or []) if t] if res is not None else []

    for photo in photos:
        with Image.open(photo) as raw:
            base_img = ImageOps.exif_transpose(raw)
            print("=" * 96)
            print(f"СНИМОК {photo.name} | {photo.stat().st_size / 1e6:.2f} МБ | {raw.width}x{raw.height}")
            results = {}
            for label, quality, subsampling in VARIANTS:
                if quality is None:
                    img = base_img
                    size = photo.stat().st_size
                else:
                    buf = io.BytesIO()
                    base_img.convert("RGB").save(buf, format="JPEG", quality=quality,
                                                 optimize=True, subsampling=subsampling)
                    img = Image.open(io.BytesIO(buf.getvalue()))
                    size = len(buf.getvalue())
                runs = []
                for _ in range(RUNS):
                    runs.append(NUM.findall(" ".join(ocr(img))))
                sets = [set(r) for r in runs]
                stable = all(s == sets[0] for s in sets)
                results[label] = sets[0]
                print(f"  {label:22} {size / 1e6:5.2f} МБ | прогонов {RUNS}: "
                      f"{'числа одинаковы' if stable else 'числа РАЗЛИЧАЮТСЯ'} | "
                      f"набор {' '.join(sorted(sets[0]))}")

            base_set = results.get("оригинал без изменений", set())
            for label, _, _ in VARIANTS[1:]:
                s = results[label]
                lost = sorted(base_set - s)
                added = sorted(s - base_set)
                print(f"  -> {label}: потеряно {lost or 'ничего'}, добавлено {added or 'ничего'}")

        if len(photos) == 1:
            break


if __name__ == "__main__":
    main()
