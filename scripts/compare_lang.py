"""Сравнение языковых вариантов PP-OCRv5 на одном скане: cyrillic против eslav.

Зачем: на плотном мелком шрифте (накладная) cyrillic-вариант путает похожие буквы и цифры. В прошлых
замерах eslav давал больше верных чисел — проверяем на том же файле, что и приёмка.
"""
import re
import sys
import time

import numpy as np
from PIL import Image
from rapidocr import RapidOCR
from rapidocr.utils.typings import LangRec, ModelType, OCRVersion

IMG = sys.argv[1] if len(sys.argv) > 1 else "/home/yartsevn/ocr-bakeoff/naklad.png"
CONTROL = ["13959.9", "2791.98", "16751.90", "57834.75", "45305.47", "43250.36"]
PRINTABLE = "Балка MS Pro Универсальный Счет-фактура Продавец"   # по этим словам видно качество букв


def run(lang_name: str, lang_enum) -> None:
    engine = RapidOCR(params={"Rec.ocr_version": OCRVersion.PPOCRV5, "Rec.lang_type": lang_enum,
                              "Rec.model_type": ModelType.MOBILE})
    started = time.time()
    out = engine(np.array(Image.open(IMG).convert("RGB")))
    texts = [str(t) for t in (getattr(out, "txts", None) or [])]
    joined = " ".join(texts)
    clean = joined.replace(",", ".")
    found = [c for c in CONTROL if c in clean]
    cyr = len(re.findall(r"[А-Яа-яЁё]", joined))
    lat = len(re.findall(r"[A-Za-z]", joined))
    print(f"--- {lang_name} ---")
    print(f"время: {time.time() - started:.1f} с | строк: {len(texts)} | символов: {len(joined)} "
          f"| кириллица: {cyr} | латиница: {lat}")
    print(f"контрольные числа: {len(found)}/{len(CONTROL)} {found}")
    for word in PRINTABLE.split(" "):
        if word and word in joined:
            print(f"   найдено слово: {word}")
    print("   примеры строк:", [t[:50] for t in texts[:6]])


run("cyrillic", LangRec.CYRILLIC)
run("eslav", LangRec.ESLAV)
