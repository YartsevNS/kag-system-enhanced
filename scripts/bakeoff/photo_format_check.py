"""Замер: какой формат и качество сохраняют распознавание снимка.

ИСПРАВЛЕНИЕ МЕТОДИКИ. В прошлом замере (photo_quality_check) распознавание запускалось на картинке
ДО пересохранения, а вес мерился у пересохранённого файла — поэтому «все качества совпали» было
артефактом. Здесь варианты декодируются из полученных байтов и только потом распознаются, то есть
проверяется ровно то, что легло бы на диск.

Перебираются: JPEG (качество 95/92/90/85, выборка 4:4:4), WebP (95/90/85, без потерь) и PNG.
Сравнение — по множеству чисел (документы важны цифрами), и по числу строк.

Запуск внутри контейнера api:
  docker cp photo_format_check.py kag-api:/tmp/ && docker exec kag-api python /tmp/photo_format_check.py
"""
from __future__ import annotations

import io
import pathlib
import re

UPLOADS = pathlib.Path("/app/data/uploads")
NUM = re.compile(r"\d+(?:[.,]\d+)?")


def variants():
    """Что пробуем: (подпись, функция сохранения)."""
    for q in (95, 92, 90, 85):
        yield f"JPEG {q}, 4:4:4", lambda img, q=q: _save(img, "JPEG", quality=q, optimize=True, subsampling=0)
    for q in (95, 90, 85):
        yield f"WebP {q}", lambda img, q=q: _save(img, "WEBP", quality=q, method=6)
    yield "WebP без потерь", lambda img: _save(img, "WEBP", lossless=True, method=6)
    yield "PNG", lambda img: _save(img, "PNG", optimize=True)


def _save(img, fmt: str, **kw) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format=fmt, **kw)
    return buf.getvalue()


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
            base_lines = ocr(base_img)
            base_nums = set(NUM.findall(" ".join(base_lines)))
            print("=" * 96)
            print(f"СНИМОК {photo.name} | {photo.stat().st_size / 1e6:.2f} МБ")
            print(f"  ЭТАЛОН: строк {len(base_lines)}, чисел {len(base_nums)}: {' '.join(sorted(base_nums))}")

            for label, save in variants():
                payload = save(base_img)
                decoded = Image.open(io.BytesIO(payload))       # ← распознаём именно то, что сохранили
                lines = ocr(decoded)
                nums = set(NUM.findall(" ".join(lines)))
                ok_lines = len(lines) == len(base_lines)
                if nums == base_nums and ok_lines:
                    verdict = "СОВПАЛО"
                elif base_nums <= nums:
                    verdict = "числа сохранены, есть лишние"
                else:
                    verdict = f"ПОТЕРЯНО: {' '.join(sorted(base_nums - nums)) or '—'}"
                print(f"  {label:16} {len(payload) / 1e6:6.2f} МБ | строк {len(lines):>3} "
                      f"({'=' if ok_lines else '≠'}{len(base_lines)}) | {verdict}")


if __name__ == "__main__":
    main()
