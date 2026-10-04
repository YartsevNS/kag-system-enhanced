"""Диагностика раскладки строк по ячейке: что именно попадает внутрь."""
import sys
from pathlib import Path

import cv2
import fitz
import numpy as np
import onnxruntime as ort
from rapidocr import RapidOCR
from rapidocr.utils.typings import LangRec, ModelType, OCRVersion

sys.path.insert(0, "/home/yartsevn/table-bakeoff")
from table_type_route import line_box, merge_cell_lines  # noqa: E402
from cell_detector_test import detect_cells, page_image, table_box  # noqa: E402

LAYOUT = Path("/home/yartsevn/table-bakeoff/detector/pp_doclayout_v3_dynbatch.onnx")
MODEL = Path("/home/yartsevn/paddle-build/cell_wireless.onnx")

img = page_image("13611481-3.pdf")
print("страница:", img.shape[1], "x", img.shape[0])
layout = ort.InferenceSession(str(LAYOUT), providers=["CPUExecutionProvider"])
det = table_box(img, layout)
print("бокс таблицы:", [round(v) for v in det[1:]])
_, x0, y0, x1, y1 = det
crop = img[max(0, int(y0) - 4):int(y1) + 4, max(0, int(x0) - 4):int(x1) + 4]
print("кроп:", crop.shape[1], "x", crop.shape[0])

eng = RapidOCR(params={"Rec.ocr_version": OCRVersion.PPOCRV5, "Rec.lang_type": LangRec.CYRILLIC,
                       "Rec.model_type": ModelType.MOBILE})
out = eng(img)
raw_boxes = list(out.boxes) if out.boxes is not None else []
print("строк OCR:", len(raw_boxes), "| пример рамки:", np.asarray(raw_boxes[0]).reshape(-1, 2).tolist() if raw_boxes else None)
lines = []
for q, t in zip(raw_boxes, [t for t in (out.txts or [])]):
    a = np.asarray(q, dtype=float).reshape(-1, 2)
    lines.append({"text": t, "bbox": [float(a[:, 0].min()) - x0 + 4, float(a[:, 1].min()) - y0 + 4,
                                      float(a[:, 0].max()) - x0 + 4, float(a[:, 1].max()) - y0 + 4]})
print("первые 5 строк с координатами в кропе:")
for l in lines[:5]:
    print("   ", [round(v) for v in l["bbox"]], "|", l["text"][:40])

sess = ort.InferenceSession(str(MODEL), providers=["CPUExecutionProvider"])
cells, _ = detect_cells(crop, sess)
c0 = sorted(cells, key=lambda c: (c[1], c[0]))[0]
print("\nпервая ячейка:", [round(v) for v in c0])
inside, overlap_hit = [], []
for l in lines:
    b = line_box(l)
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    w = max(1.0, b[2] - b[0])
    ov = max(0.0, min(c0[2], b[2]) - max(c0[0], b[0]))
    if c0[0] <= cx <= c0[2] and c0[1] <= cy <= c0[3]:
        inside.append((round(b[1]), round(b[3]), l["text"][:30]))
    elif ov / w >= 0.5:
        overlap_hit.append((round(b[1]), round(b[3]), round(ov / w, 2), l["text"][:30]))
print("внутри по центру:", len(inside))
for row in inside[:6]:
    print("   y", row[0], "..", row[1], "|", row[2])
print("по перекрытию:", len(overlap_hit))
for row in overlap_hit[:6]:
    print("   y", row[0], "..", row[1], "перекрытие", row[2], "|", row[3])
print("\nитог склейки:", merge_cell_lines(c0, lines)[:120])
