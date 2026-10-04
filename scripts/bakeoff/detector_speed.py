"""Проверка двух гипотез ускорения: INT8 только для ДЕТЕКТОРА и OpenVINO Execution Provider.

Сравниваем с FP32 по: найденным таблицам (IoU с боксом FP32), ложным срабатываниям, времени.
Железо: Intel Xeon E5-2682 v4 (Broadwell) — AVX2 есть, AVX-512/VNNI/AMX нет.
"""
import time
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

DET = Path("/home/yartsevn/table-bakeoff/detector/pp_doclayout_v3_dynbatch.onnx")
QUANT = Path("/home/yartsevn/table-bakeoff/detector/pp_doclayout_v3_int8.onnx")
PAGES = {
    "смета_скан": "/home/yartsevn/table-bakeoff/img/smeta.pdf",
    "накладная_скан": "/home/yartsevn/table-bakeoff/img/9e116f5c-edee-4574-a854-5361be9be855skan-nakladnoj3.png",
    "титул_без_таблицы": "/home/yartsevn/table-bakeoff/img/13611481-1.pdf",
}
TABLE_CLASS = 21
CONF = 0.5


def page_image(path: str) -> np.ndarray | None:
    p = Path(path)
    if not p.exists():
        return None
    if p.suffix.lower() == ".pdf":
        import fitz
        return cv2.imdecode(np.frombuffer(fitz.open(path)[0].get_pixmap(dpi=200).tobytes("png"),
                                          np.uint8), cv2.IMREAD_COLOR)
    return cv2.imread(path)


def detect(sess, img):
    h, w = img.shape[:2]
    resized = cv2.resize(img, (800, 800), interpolation=cv2.INTER_CUBIC)
    rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    pixel = np.transpose(rgb, (2, 0, 1))[None, ...]
    logits, boxes = sess.run(None, {"pixel_values": pixel})[:2]
    logits, boxes = logits[0], boxes[0]
    prob = 1.0 / (1.0 + np.exp(-logits))
    cls, score = prob.argmax(axis=1), prob.max(axis=1)
    out = []
    for i in np.where(score >= CONF)[0]:
        if int(cls[i]) != TABLE_CLASS:
            continue
        cx, cy, bw, bh = boxes[i]
        out.append([float(score[i]), (cx - bw / 2) * w, (cy - bh / 2) * h,
                    (cx + bw / 2) * w, (cy + bh / 2) * h])
    return out


def iou(a, b) -> float:
    if a is None or b is None:
        return 0.0
    x0, y0 = max(a[1], b[1]), max(a[2], b[2])
    x1, y1 = min(a[3], b[3]), min(a[4], b[4])
    inter = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    ua = (a[3] - a[1]) * (a[4] - a[2]) + (b[3] - b[1]) * (b[4] - b[2]) - inter
    return inter / ua if ua > 0 else 0.0


def quantize_det() -> bool:
    if QUANT.exists():
        return True
    try:
        from onnxruntime.quantization import QuantType, quantize_dynamic
        quantize_dynamic(model_input=str(DET), model_output=str(QUANT),
                         per_channel=True, weight_type=QuantType.QUInt8)
        print(f"INT8-детектор собран: {QUANT.stat().st_size/1024/1024:.1f} МБ "
              f"(fp32 {DET.stat().st_size/1024/1024:.1f} МБ)")
        return True
    except Exception as e:  # noqa: BLE001
        print("квантование детектора не вышло:", type(e).__name__, str(e)[:160])
        return False


def run_variant(name: str, sess, pages):
    print(f"\n--- {name} | провайдеры: {sess.get_providers()[:1]} ---")
    res = {}
    for page, path in pages.items():
        img = page_image(path)
        if img is None:
            continue
        t0 = time.time()
        boxes = detect(sess, img)
        secs = time.time() - t0
        best = max(boxes, key=lambda b: b[0]) if boxes else None
        res[page] = best
        print(f"   {page:20} таблиц {len(boxes)} | лучший "
              f"{('скор %.2f @ [%d,%d,%d,%d]' % (best[0], *[round(v) for v in best[1:]])) if best else '—'}"
              f" | {secs:.2f} с")
        res[page + "_secs"] = secs
        res[page + "_n"] = len(boxes)
    return res


def main() -> None:
    base = ort.InferenceSession(str(DET), providers=["CPUExecutionProvider"])
    fp = run_variant("FP32 CPU", base, PAGES)

    if quantize_det():
        q = ort.InferenceSession(str(QUANT), providers=["CPUExecutionProvider"])
        qq = run_variant("INT8 CPU", q, PAGES)
        print("\n=== IoU INT8 против FP32 и время ===")
        for page in PAGES:
            if page + "_secs" not in fp:
                continue
            i = iou(fp.get(page), qq.get(page)) if fp.get(page) and qq.get(page) else 0.0
            print(f"   {page:20} IoU {i:.3f} | время {fp[page+'_secs']:.2f} → {qq[page+'_secs']:.2f} с "
                  f"| таблиц {fp[page+'_n']} → {qq[page+'_n']}")

    try:
        ov = ort.InferenceSession(str(DET), providers=[
            ("OpenVINOExecutionProvider", {"device_type": "CPU_FP32"}),
            "CPUExecutionProvider"])
        print("\nOpenVINO поднялся:", ov.get_providers())
        run_variant("OpenVINO CPU_FP32", ov, PAGES)
    except Exception as e:  # noqa: BLE001
        print("\nOpenVINO не поднялся:", type(e).__name__, str(e)[:200])


if __name__ == "__main__":
    main()
