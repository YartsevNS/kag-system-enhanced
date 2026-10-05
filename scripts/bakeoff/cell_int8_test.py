"""INT8 для детектора ЯЧЕЕК: держит ли точность и что со временем.

Нужен для решения о поставке: FP32 весит 129 МБ, INT8 обычно ~35 МБ. Критерий — совпадение боксов
(IoU) с FP32 и отсутствие лишних/потерянных ячеек, а не «размер меньше».
"""
import time
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

SRC = Path("/home/yartsevn/paddle-build/cell_wireless.onnx")
DST = Path("/home/yartsevn/paddle-build/cell_wireless_int8.onnx")
PAGES = [("конспект 13611481-3", "/home/yartsevn/table-bakeoff/img/13611481-3.pdf"),
         ("накладная", "/home/yartsevn/table-bakeoff/img/9e116f5c-edee-4574-a854-5361be9be855skan-nakladnoj3.png")]


def crop_of(path: str) -> np.ndarray:
    import fitz
    from cell_detector_test import page_image, table_box

    layout = ort.InferenceSession("/home/yartsevn/table-bakeoff/detector/pp_doclayout_v3_dynbatch.onnx",
                                  providers=["CPUExecutionProvider"])
    img = page_image(Path(path).name)
    _, x0, y0, x1, y1 = table_box(img, layout)
    return img[max(0, int(y0) - 4):int(y1) + 4, max(0, int(x0) - 4):int(x1) + 4]


def boxes(sess, crop: np.ndarray, conf: float = 0.4):
    h, w = crop.shape[:2]
    small = cv2.resize(crop, (640, 640), interpolation=cv2.INTER_LINEAR)
    rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    feed = {"image": np.transpose(rgb, (2, 0, 1))[None, ...],
            "im_shape": np.array([[640.0, 640.0]], np.float32),
            "scale_factor": np.array([[1.0, 1.0]], np.float32)}
    feed = {k: v for k, v in feed.items() if k in [i.name for i in sess.get_inputs()]}
    t0 = time.time()
    outs = sess.run(None, feed)
    secs = time.time() - t0
    sx, sy = w / 640.0, h / 640.0
    out = []
    for o in outs:
        a = np.asarray(o)
        if a.dtype != np.float32 or a.ndim < 2 or a.shape[-1] != 6:
            continue
        for row in a.reshape(-1, 6):
            cls, score, x0, y0, x1, y1 = (float(v) for v in row)
            if score >= conf and int(cls) == 0 and x1 > x0 and y1 > y0:
                out.append((score, x0 * sx, y0 * sy, x1 * sx, y1 * sy))
    return out, secs


def iou(a, b) -> float:
    x0, y0 = max(a[1], b[1]), max(a[2], b[2])
    x1, y1 = min(a[3], b[3]), min(a[4], b[4])
    inter = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    u = (a[3] - a[1]) * (a[4] - a[2]) + (b[3] - b[1]) * (b[4] - b[2]) - inter
    return inter / u if u > 0 else 0.0


def main() -> None:
    if not DST.exists():
        from onnxruntime.quantization import QuantType, quantize_dynamic
        quantize_dynamic(model_input=str(SRC), model_output=str(DST),
                         per_channel=True, weight_type=QuantType.QUInt8)
        print(f"INT8 собран: {DST.stat().st_size/1024/1024:.1f} МБ (FP32 {SRC.stat().st_size/1024/1024:.1f} МБ)")
    fp32 = ort.InferenceSession(str(SRC), providers=["CPUExecutionProvider"])
    int8 = ort.InferenceSession(str(DST), providers=["CPUExecutionProvider"])
    for label, path in PAGES:
        crop = crop_of(path)
        b_fp, t_fp = boxes(fp32, crop)
        b_i8, t_i8 = boxes(int8, crop)
        # сопоставление по максимуму IoU для каждого бокса FP32
        matched = 0
        ious = []
        for b in b_fp:
            best = max((iou(b, c) for c in b_i8), default=0.0)
            ious.append(best)
            if best >= 0.5:
                matched += 1
        print(f"{label}: кроп {crop.shape[1]}x{crop.shape[0]}")
        print(f"   FP32: ячеек {len(b_fp):3}, {t_fp:.2f} с | INT8: ячеек {len(b_i8):3}, {t_i8:.2f} с")
        print(f"   совпало по IoU>=0.5: {matched} из {len(b_fp)} | медиана IoU "
              f"{np.median(ious):.3f}" if ious else "   сопоставлять нечего")


if __name__ == "__main__":
    main()
