"""Досчитать контрольные числа PP-OCRv5 на 18 (тот же файл, что Occular)."""
import io
import re

import numpy as np
from PIL import Image

import rapidocr
from rapidocr.utils.typings import LangRec, ModelType, OCRVersion

CONTROL = ["13959.9", "2791.98", "16751.90", "57834.75", "45305.47", "43250.36"]


def main():
    png = "/app/data/uploads/9e116f5c-edee-4574-a854-5361be9be855_skan-nakladnoj3.png"
    data = open(png, "rb").read()
    ocr = rapidocr.RapidOCR(params={"Rec.ocr_version": OCRVersion.PPOCRV5,
                                    "Rec.lang_type": LangRec.CYRILLIC,
                                    "Rec.model_type": ModelType.MOBILE})
    img = Image.open(io.BytesIO(data)).convert("RGB")
    out = ocr(np.array(img))
    text = " ".join(str(x) for x in (getattr(out, "txts", None) or []))
    clean = re.sub(r"[^0-9,.]+", "", text).replace(",", ".")
    found = [n for n in CONTROL if n in clean]
    print(f"PP-OCRv5 на 18: символов {len(text)} | контрольных чисел {len(found)}/{len(CONTROL)} -> {found}")


if __name__ == "__main__":
    main()