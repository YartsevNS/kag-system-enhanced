"""Подбор правильного порядка служебных входов детектора ячеек.

Модель отдаёт коробки за пределами кропа — значит постпроцессинг внутри графа ждёт im_shape/scale_factor
в другом порядке или с другим смыслом. Перебираем варианты и выбираем тот, где коробки лежат ВНУТРИ
кропа (это проверяемый критерий, а не догадка).
"""
import sys
from pathlib import Path

import cv2
import fitz
import numpy as np
import onnxruntime as ort

MODEL = Path("/home/yartsevn/paddle-build/cell_wireless.onnx")
IMG = Path("/home/yartsevn/table-bakeoff/img/13611481-3.pdf")
LAYOUT = Path("/home/yartsevn/table-bakeoff/detector/pp_doclayout_v3_dynbatch.onnx")


def crop_of_page() -> np.ndarray:
    png = fitz.open(str(IMG))[0].get_pixmap(dpi=200).tobytes("png")
    img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
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
    crop = crop_of_page()
    h, w = crop.shape[:2]
    print(f"кроп {w}x{h} — истина 4 строки x 3 колонки (12 ячеек)")
    sess = ort.InferenceSession(str(MODEL), providers=["CPUExecutionProvider"])
    img640 = cv2.resize(crop, (640, 640), interpolation=cv2.INTER_LINEAR)
    rgb = cv2.cvtColor(img640, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    tensor = np.transpose(rgb, (2, 0, 1))[None, ...]

    variants = {
        "im_shape=[h,w], scale=(640/h, 640/w)": (np.array([[h, w]], np.float32), np.array([[640.0 / h, 640.0 / w]], np.float32)),
        "im_shape=[w,h], scale=(640/w, 640/h)": (np.array([[w, h]], np.float32), np.array([[640.0 / w, 640.0 / h]], np.float32)),
        "im_shape=[h,w], scale=(640/w, 640/h)": (np.array([[h, w]], np.float32), np.array([[640.0 / w, 640.0 / h]], np.float32)),
        "im_shape=[[640,640]], scale=(1,1)": (np.array([[640.0, 640.0]], np.float32), np.array([[1.0, 1.0]], np.float32)),
    }
    for name, (im_shape, scale) in variants.items():
        outs = sess.run(None, {"image": tensor, "im_shape": im_shape, "scale_factor": scale})
        rows = np.asarray(outs[0]).reshape(-1, 6)
        sc = rows[:, 1].astype(float)
        mask = sc >= 0.4
        sel = rows[mask][:, 2:6].astype(float)
        if not len(sel):
            print(f"{name}: 0 ячеек")
            continue
        xs0, ys0, xs1, ys1 = sel[:, 0].min(), sel[:, 1].min(), sel[:, 2].max(), sel[:, 3].max()
        inside = 0 <= xs0 and 0 <= ys0 and xs1 <= w * 1.05 and ys1 <= h * 1.05
        # грубая оценка геометрии: сколько уникальных полос по Y и по X
        ys = sorted((sel[:, 1] + sel[:, 3]) / 2)
        bands = 1 + sum(1 for a, b in zip(ys, ys[1:]) if b - a > h * 0.03)
        print(f"{name}: ячеек {len(sel)} | x {xs0:.0f}..{xs1:.0f} (кроп {w}) | y {ys0:.0f}..{ys1:.0f} (кроп {h}) "
              f"| внутри кропа: {'ДА' if inside else 'НЕТ'} | полос по Y ~{bands}")


if __name__ == "__main__":
    main()
