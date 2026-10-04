"""Гипотеза INT8: квантуем распознаватели и замеряем время И подстановки цифр на наших сканах.

Варианты:
  fp32            — как в проде;
  int8_all        — динамическое квантование всех MatMul (per-channel);
  int8_head_fp32  — то же, но голова (последние MatMul + Softmax) остаётся в FP32.

Метрика: время на страницу и совпадение ЧИСЕЛ с эталоном FP32 (подстановки цифр).
"""
import glob
import io
import re
import time
from pathlib import Path

import fitz
import numpy as np
from PIL import Image
from rapidocr import RapidOCR
from rapidocr.utils.typings import LangRec, ModelType, OCRVersion

MODELS = Path("/home/yartsevn/table-bakeoff/.venv/lib/python3.12/site-packages/rapidocr/models")
WORK = Path("/home/yartsevn/table-bakeoff/int8")
WORK.mkdir(exist_ok=True)
IMGS = Path("/home/yartsevn/table-bakeoff/img")

NUM = re.compile(r"\d[\d\s]*(?:[.,]\d+)?")


def quantize(src: Path, dst: Path, exclude_head) -> list[str]:
    import onnx
    from onnxruntime.quantization import QuantType, quantize_dynamic

    model = onnx.load(str(src))
    excluded: list[str] = []
    if exclude_head == "backward":
        # Исправленный рецепт извне: идём по графу С КОНЦА от выходного тензора и защищаем
        # узлы головы (MatMul/Gemm/Softmax/Add), пока не наберём 15 узлов.
        target = {model.graph.output[0].name}
        for node in reversed(model.graph.node):
            if any(o in target for o in node.output):
                if node.op_type in ("MatMul", "Gemm", "Softmax", "Add"):
                    excluded.append(node.name)
                    target.update(node.input)
            if len(excluded) > 15:
                break
    elif exclude_head:
        matmuls = [n.name for n in model.graph.node if n.op_type == "MatMul"]
        softmax = [n.name for n in model.graph.node if n.op_type == "Softmax"]
        excluded = matmuls[-2:] + softmax
    quantize_dynamic(model_input=str(src), model_output=str(dst),
                     per_channel=True, weight_type=QuantType.QUInt8,
                     nodes_to_exclude=excluded)
    return excluded


def page_png(name: str) -> bytes | None:
    p = IMGS / name
    if not p.exists():
        return None
    if p.suffix.lower() == ".pdf":
        return fitz.open(str(p))[0].get_pixmap(dpi=200).tobytes("png")
    return p.read_bytes()


def run(model_path: str | None, lang: str, image: bytes):
    params = {"Rec.ocr_version": OCRVersion.PPOCRV5, "Rec.model_type": ModelType.MOBILE,
              "Rec.lang_type": LangRec.CYRILLIC if lang == "cyrillic" else LangRec.ESLAV}
    if model_path:
        params["Rec.model_path"] = model_path
    eng = RapidOCR(params=params)
    t0 = time.time()
    out = eng(image)
    secs = time.time() - t0
    texts = [t for t in (out.txts or []) if t and t.strip()]
    return secs, texts


def numbers_of(texts) -> list[str]:
    got = []
    for t in texts:
        for m in NUM.findall(t):
            v = m.replace(" ", "").replace(",", ".")
            try:
                f = float(v)
            except ValueError:
                continue
            got.append(("%.2f" % f))
    return got


def digit_diff(a: str, b: str) -> int:
    """Сколько позиций цифр различается между двумя числовыми строками."""
    da = re.sub(r"\D", "", a)
    db = re.sub(r"\D", "", b)
    return sum(1 for x, y in zip(da, db) if x != y) + abs(len(da) - len(db))


def main() -> None:
    cyr = MODELS / "cyrillic_PP-OCRv5_rec_mobile.onnx"
    variants: list[tuple[str, str | None]] = [("fp32", None)]
    for tag, excl in (("int8_all", False), ("int8_head_fp32", True),
                      ("int8_head15_backward", "backward")):
        dst = WORK / f"cyrillic_rec_{tag}.onnx"
        if not dst.exists():
            ex = quantize(cyr, dst, excl)
            print(f"[{tag}] исключено узлов: {len(ex)} {ex[:4]} | размер "
                  f"{dst.stat().st_size/1024/1024:.1f} МБ (fp32 {cyr.stat().st_size/1024/1024:.1f} МБ)")
        variants.append((tag, str(dst)))

    pages = [("накладная_скан", "9e116f5c-edee-4574-a854-5361be9be855skan-nakladnoj3.png"),
             ("смета_скан", "smeta.pdf")]
    for page_name, fname in pages:
        img = page_png(fname)
        if img is None:
            print(f"{page_name}: файла {fname} нет, пропуск")
            continue
        print(f"\n=== {page_name}: сравнение вариантов ===")
        base_nums: list[str] = []
        for tag, path in variants:
            secs, texts = run(path, "cyrillic", img)
            nums = numbers_of(texts)
            if tag == "fp32":
                base_nums = nums
                print(f"   {tag:15} {secs:6.1f} с | строк {len(texts)} | чисел {len(nums)}  ← эталон")
                continue
            # сопоставляем числа по позиции: считаем подстановки цифр
            n = min(len(base_nums), len(nums))
            subs = sum(digit_diff(base_nums[i], nums[i]) for i in range(n))
            subs += abs(len(base_nums) - len(nums)) * 1  # потерянные/лишние числа — тоже расхождение
            print(f"   {tag:15} {secs:6.1f} с | строк {len(texts)} | чисел {len(nums)} | "
                  f"подстановок цифр против fp32: {subs}")
        print(f"   (для справки: эталонных чисел {len(base_nums)})")


if __name__ == "__main__":
    main()
