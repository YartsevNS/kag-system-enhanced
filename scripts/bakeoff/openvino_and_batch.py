"""Проверка гипотез (а) OpenVINO FP32 и (в) батч — на НАШЕЙ модели распознавателя и реальных строках.

Как: берём строки OCR с накладной, режем вырезки, приводим к входу rec-модели (3x48x320),
затем сравниваем: поштучные вызовы против одного батча, и CPU EP против OpenVINO EP.
Метрика: время и совпадение выходов (argmax по классам — то, что решает, поедет ли цифра).
"""
import io
import time
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

IMG = Path("/home/yartsevn/table-bakeoff/img/9e116f5c-edee-4574-a854-5361be9be855skan-nakladnoj3.png")
REC = Path("/home/yartsevn/table-bakeoff/.venv/lib/python3.12/site-packages/rapidocr/models/cyrillic_PP-OCRv5_rec_mobile.onnx")
H, W = 48, 320


def crops_from_page(limit: int = 120):
    from rapidocr import RapidOCR
    from rapidocr.utils.typings import LangRec, ModelType, OCRVersion
    eng = RapidOCR(params={"Rec.ocr_version": OCRVersion.PPOCRV5, "Rec.lang_type": LangRec.CYRILLIC,
                           "Rec.model_type": ModelType.MOBILE})
    out = eng(cv2.imread(str(IMG)))
    img = cv2.imread(str(IMG))
    boxes = list(out.boxes) if out.boxes is not None else []
    crops = []
    for b in boxes[:limit]:
        b = np.asarray(b, dtype=float).reshape(-1, 2)
        x0, y0 = int(b[:, 0].min()), int(b[:, 1].min())
        x1, y1 = int(b[:, 0].max()), int(b[:, 1].max())
        if x1 - x0 < 4 or y1 - y0 < 4:
            continue
        crop = img[max(0, y0):y1, max(0, x0):x1]
        crop = cv2.resize(crop, (W, H), interpolation=cv2.INTER_LINEAR)
        arr = crop.astype(np.float32) / 255.0
        arr = (arr - 0.5) / 0.5
        crops.append(np.transpose(arr, (2, 0, 1)))
    return np.stack(crops, axis=0) if crops else np.empty((0, 3, H, W), np.float32)


def session(providers):
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = 4
    opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    return ort.InferenceSession(str(REC), opts, providers=providers)


def run(sess, x):
    name = sess.get_inputs()[0].name
    t0 = time.time()
    y = sess.run(None, {name: x})[0]
    return y, time.time() - t0


def main() -> None:
    batch = crops_from_page()
    print(f"вырезок строк: {batch.shape[0]}, тензор {batch.shape}")
    if batch.shape[0] == 0:
        return

    cpu = session(["CPUExecutionProvider"])
    try:
        ov = session([("OpenVINOExecutionProvider", {"device_type": "CPU"}), "CPUExecutionProvider"])
        ov_name = ov.get_providers()[0]
    except Exception as e:  # noqa: BLE001
        ov, ov_name = None, f"нет ({type(e).__name__})"

    print(f"\n--- поштучно (по одному вызову) ---")
    y_cpu_single, t_cpu_s = run(cpu, batch[:1])
    per = np.vstack([run(cpu, batch[i:i + 1])[0] for i in range(batch.shape[0])])
    t_cpu_loop = 0.0
    t0 = time.time()
    outs = [cpu.run(None, {cpu.get_inputs()[0].name: batch[i:i + 1]})[0] for i in range(batch.shape[0])]
    t_cpu_loop = time.time() - t0
    per = np.vstack(outs)
    print(f"   CPU EP: {batch.shape[0]} вызовов за {t_cpu_loop:.2f} с")

    print(f"--- одним батчем ---")
    y_cpu_batch, t_cpu_b = run(cpu, batch)
    print(f"   CPU EP: один вызов за {t_cpu_b:.2f} с")
    diff_batch = float(np.abs(y_cpu_batch - per).max())
    same_argmax = float((y_cpu_batch.argmax(-1) == per.argmax(-1)).mean())
    print(f"   батч против поштучно: макс |Δ| {diff_batch:.2e}, совпадение argmax {same_argmax:.4f}")

    if ov is not None:
        y_ov_batch, t_ov_b = run(ov, batch)
        diff_ov = float(np.abs(y_ov_batch - y_cpu_batch).max())
        same_ov = float((y_ov_batch.argmax(-1) == y_cpu_batch.argmax(-1)).mean())
        print(f"--- OpenVINO ({ov_name}) ---")
        print(f"   один батч за {t_ov_b:.2f} с | против CPU-батча: макс |Δ| {diff_ov:.2e}, "
              f"совпадение argmax {same_ov:.4f}")
        print(f"   ускорение против CPU: поштучно {t_cpu_loop/t_ov_b:.2f}x, батчем {t_cpu_b/t_ov_b:.2f}x")
    print(f"\nускорение батча против поштучных вызовов (CPU): {t_cpu_loop/t_cpu_b:.2f}x")


if __name__ == "__main__":
    main()
