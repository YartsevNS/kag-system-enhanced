"""Сравнение OCR-движков на одном файле: печатает строки, время и символы.

Запускается в двух образах (старом, с Occular, и новом, с PP-OCRv5) — так приёмка получает честное
сравнение на одном и том же файле, а не сравнение по памяти.
"""
import sys
import time

path = sys.argv[1]
engine = "auto"
for arg in sys.argv[2:]:
    if arg.startswith("--engine="):
        engine = arg.split("=", 1)[1]

texts = []
started = time.time()

if engine in ("auto", "occular"):
    pipe = None
    for module_name in ("occular", "ocr_skel"):
        try:
            module = __import__(module_name)
            pipe = module.OCRPipeline()
            engine = f"{module_name}"
            break
        except Exception:
            continue
    if pipe is not None:
        try:
            results = pipe.process_image(path)
        except TypeError:
            results = pipe.process_image(str(path))
        texts = [str(r.get("text") or "") for r in (results or []) if isinstance(r, dict)]

if not texts and engine in ("auto", "ppocrv5", "occular"):
    try:
        import numpy as np
        from PIL import Image
        from rapidocr import RapidOCR
        from rapidocr.utils.typings import LangRec, ModelType, OCRVersion

        ocr = RapidOCR(params={"Rec.ocr_version": OCRVersion.PPOCRV5, "Rec.lang_type": LangRec.CYRILLIC,
                               "Rec.model_type": ModelType.MOBILE})
        out = ocr(np.array(Image.open(path).convert("RGB")))
        texts = [str(t) for t in (getattr(out, "txts", None) or [])]
        engine = "rapidocr-ppocrv5-cyrillic"
    except Exception as e:  # noqa: BLE001
        print("движок недоступен:", type(e).__name__, str(e)[:120])

joined = " ".join(texts)
print(f"движок: {engine} | время: {time.time() - started:.1f} с | строк: {len(texts)} | символов: {len(joined)}")
print("примеры строк:")
for line in texts[:12]:
    print("   ", line[:80])
control = ["13959.9", "16751.90", "45305.47", "43250.36", "2791.98", "57834.75"]
clean = joined.replace(",", ".")
found = [c for c in control if c in clean]
print("контрольные числа:", f"{len(found)}/{len(control)}", found)
