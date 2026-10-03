"""Честный замер: Occular vs PP-OCRv5 (RapidOCR) НА ОДНОМ ХОСТЕ (18, в контейнере api).

Для PP-OCRv5 используем распознаватель eslav/cyrillic через RapidOCR, если он установлен в этом контейнере;
если нет — помечаем, что нужно поставить.
"""
import io
import re
import time

import numpy as np
from PIL import Image


def main():
    png = "/app/data/uploads/9e116f5c-edee-4574-a854-5361be9be855_skan-nakladnoj3.png"
    data = open(png, "rb").read()

    # Occular
    from src.indexing.table_strategy import _raw_lines_from_occular
    t0 = time.time()
    lines_o = _raw_lines_from_occular(data)
    dt_occ = time.time() - t0
    text_o = " ".join(str(l.get("text") or "") for l in lines_o)
    print(f"OCCULAR на 18: {dt_occ:.1f} с | строк {len(lines_o)} | символов {len(text_o)}")

    # PP-OCRv5
    try:
        import rapidocr
        from rapidocr.utils.typings import LangRec, ModelType, OCRVersion
        ocr = rapidocr.RapidOCR(params={"Rec.ocr_version": OCRVersion.PPOCRV5,
                                        "Rec.lang_type": LangRec.CYRILLIC,
                                        "Rec.model_type": ModelType.MOBILE})
        img = Image.open(io.BytesIO(data)).convert("RGB")
        t1 = time.time()
        out = ocr(np.array(img))
        dt_pp = time.time() - t1
        txt_pp = " ".join(str(x) for x in (getattr(out, "txts", None) or []))
        print(f"PP-OCRv5 на 18: {dt_pp:.1f} с | символов {len(txt_pp)}")
    except Exception as e:
        print(f"PP-OCRv5 на 18 НЕДОСТУПЕН: {type(e).__name__}: {str(e)[:90]}")

    def nums(t):
        cl = re.sub(r"[^0-9,.]+", "", t).replace(",", ".")
        return [n for n in ["13959.9","2791.98","16751.90","57834.75","45305.47","43250.36"] if n in cl]

    print("контрольные числа Occular:", len(nums(text_o)), nums(text_o))


if __name__ == "__main__":
    main()