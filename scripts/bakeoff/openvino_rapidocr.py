"""Обёртка OpenVINO для rapidocr + замер полного пайплайна страницы.

Идея: не переписывать препроцессинг и CTC-декодер rapidocr, а подменить список провайдеров
в его же `ProviderConfig.get_ep_list` — тогда сравниваем честно (тот же код, меняется движок).

Метрики: время на страницу, число строк, числа (совпадение с CPU — подстановки цифр).
"""
import re
import time
from pathlib import Path

import fitz
import numpy as np
from PIL import Image
from rapidocr import RapidOCR
from rapidocr.utils.typings import LangRec, ModelType, OCRVersion

IMGS = Path("/home/yartsevn/table-bakeoff/img")
PAGES = [
    ("накладная_скан", "9e116f5c-edee-4574-a854-5361be9be855skan-nakladnoj3.png"),
    ("смета_скан", "smeta.pdf"),
]
NUM = re.compile(r"\d[\d\s]*(?:[.,]\d+)?")


def page_png(name: str) -> bytes:
    p = IMGS / name
    if p.suffix.lower() == ".pdf":
        return fitz.open(str(p))[0].get_pixmap(dpi=200).tobytes("png")
    return p.read_bytes()


def numbers(texts) -> list[str]:
    out = []
    for t in texts:
        for m in NUM.findall(t):
            try:
                out.append("%.2f" % float(m.replace(" ", "").replace(",", ".")))
            except ValueError:
                pass
    return out


def digits_only(s: str) -> str:
    return re.sub(r"\D", "", s)


def make_engine():
    return RapidOCR(params={"Rec.ocr_version": OCRVersion.PPOCRV5, "Rec.model_type": ModelType.MOBILE,
                            "Rec.lang_type": LangRec.CYRILLIC})


def run(engine, image: bytes):
    t0 = time.time()
    out = engine(image)
    secs = time.time() - t0
    texts = [t for t in (out.txts or []) if t and t.strip()]
    return secs, texts


def main() -> None:
    print("=== 1) база: CPU EP (как в проде) ===")
    base_eng = make_engine()
    base = {}
    for name, f in PAGES:
        secs, texts = run(base_eng, page_png(f))
        base[name] = (secs, texts, numbers(texts))
        print(f"   {name:18} {secs:5.1f} с | строк {len(texts):4} | чисел {len(base[name][2])}")

    print("\n=== 2) тот же код, но провайдер OpenVINO ===")
    from rapidocr.inference_engine.onnxruntime import provider_config as pc

    orig = pc.ProviderConfig.get_ep_list

    def patched(self):
        res = list(orig(self))
        return [("OpenVINOExecutionProvider", {"device_type": "CPU"})] + res

    pc.ProviderConfig.get_ep_list = patched
    ov_eng = make_engine()
    # подтверждаем, что сессии реально на OpenVINO
    try:
        sess = ov_eng.text_rec.session if hasattr(ov_eng, "text_rec") else None
        prov = sess.session.get_providers() if sess is not None else None
        print("   провайдеры сессии распознавания:", prov)
    except Exception as e:  # noqa: BLE001
        print("   провайдеры посмотреть не вышло:", type(e).__name__)

    for name, f in PAGES:
        secs, texts = run(ov_eng, page_png(f))
        b_secs, b_texts, b_nums = base[name]
        nums = numbers(texts)
        n = min(len(b_nums), len(nums))
        subs = sum(1 for i in range(n) if digits_only(b_nums[i]) != digits_only(nums[i]))
        subs += abs(len(b_nums) - len(nums))
        lost = len(b_texts) - len(texts)
        print(f"   {name:18} {secs:5.1f} с | строк {len(texts):4} (Δ {-lost:+d}) | "
              f"чисел {len(nums):4} (эталон {len(b_nums)}) | подстановок цифр {subs} | "
              f"ускорение {b_secs/max(secs, 0.01):.2f}x")


if __name__ == "__main__":
    main()
