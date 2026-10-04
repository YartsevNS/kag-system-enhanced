"""Разбор сырого вывода детектора ячеек: формат строк, распределение скоров, система координат.

Нужен, потому что «на глаз» формат не угадывается: на одном кропе вышло 300 боксов и почти пустые
ячейки. Печатаем ровно то, что вернула модель, и уже по фактам пишем постобработку.
"""
import sys
from pathlib import Path

import cv2
import fitz
import numpy as np
import onnxruntime as ort

MODEL = Path(sys.argv[1] if len(sys.argv) > 1 else "/home/yartsevn/paddle-build/cell_wireless.onnx")
IMG = Path("/home/yartsevn/table-bakeoff/img/13611481-3.pdf")
LAYOUT = Path("/home/yartsevn/table-bakeoff/detector/pp_doclayout_v3_dynbatch.onnx")


def page_image() -> np.ndarray:
    png = fitz.open(str(IMG))[0].get_pixmap(dpi=200).tobytes("png")
    return cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)


def table_crop(img: np.ndarray) -> np.ndarray:
    s = ort.InferenceSession(str(LAYOUT), providers=["CPUExecutionProvider"])
    h, w = img.shape[:2]
    small = cv2.resize(img, (800, 800), interpolation=cv2.INTER_CUBIC)
    rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    logits, boxes = s.run(None, {"pixel_values": np.transpose(rgb, (2, 0, 1))[None, ...]})[:2]
    prob = 1.0 / (1.0 + np.exp(-logits[0]))
    cls, score = prob.argmax(axis=1), prob.max(axis=1)
    best = None
    for i in np.where(score >= 0.5)[0]:
        if int(cls[i]) != 21:
            continue
        cx, cy, bw, bh = (float(v) for v in boxes[0][i])
        cand = (float(score[i]), (cx - bw / 2) * w, (cy - bh / 2) * h, (cx + bw / 2) * w, (cy + bh / 2) * h)
        if best is None or cand[0] > best[0]:
            best = cand
    _, x0, y0, x1, y1 = best
    return img[max(0, int(y0) - 4):int(y1) + 4, max(0, int(x0) - 4):int(x1) + 4]


def main() -> None:
    crop = table_crop(page_image())
    h, w = crop.shape[:2]
    print(f"кроп: {w}x{h}")
    sess = ort.InferenceSession(str(MODEL), providers=["CPUExecutionProvider"])
    print("входы:", [(i.name, i.shape, i.type) for i in sess.get_inputs()])
    print("выходы:", [(o.name, o.shape, o.type) for o in sess.get_outputs()])

    img = cv2.resize(crop, (640, 640), interpolation=cv2.INTER_LINEAR)
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    feed = {"image": np.transpose(rgb, (2, 0, 1))[None, ...],
            "im_shape": np.array([[float(h), float(w)]], np.float32),
            "scale_factor": np.array([[640.0 / h, 640.0 / w]], np.float32)}
    outs = sess.run(None, {k: v for k, v in feed.items() if k in [i.name for i in sess.get_inputs()]})

    for o, arr in zip(sess.get_outputs(), outs):
        a = np.asarray(arr)
        print(f"\n--- {o.name}: shape {a.shape} dtype {a.dtype} ---")
        if a.dtype == np.int32 or a.size <= 8:
            print("   значения:", a.reshape(-1)[:12].tolist())
            continue
        rows = a.reshape(-1, a.shape[-1])
        print("   первые 4 строки:")
        for r in rows[:4]:
            print("     ", [round(float(v), 3) for v in r])
        # распределение по каждому столбцу: где скоры, где координаты
        for col in range(rows.shape[1]):
            col_vals = rows[:, col].astype(float)
            print(f"   столбец {col}: min {col_vals.min():.3f} max {col_vals.max():.3f} "
                  f"| похоже на {'скор' if 0 <= col_vals.min() and col_vals.max() <= 1 else 'координату'}")
        # сколько боксов при разных порогах (если скор во 2-м столбце)
        for score_col in (1, 4, 5):
            if score_col >= rows.shape[1]:
                continue
            sc = rows[:, score_col].astype(float)
            for thr in (0.3, 0.5, 0.7, 0.9):
                print(f"   порог {thr} по столбцу {score_col}: {int((sc >= thr).sum())} боксов")
            break


if __name__ == "__main__":
    main()
