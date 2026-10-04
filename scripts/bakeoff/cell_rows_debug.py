"""Разбор структуры ячеек на прозаической таблице: откуда лишняя строка.

Печатаем все ячейки (координаты, текст) и текущую группировку по полосам, чтобы увидеть,
сливается ли одна смысловая строка из двух визуальных.
"""
import sys
from pathlib import Path

import cv2
import fitz
import numpy as np
import onnxruntime as ort
from rapidocr import RapidOCR
from rapidocr.utils.typings import LangRec, ModelType, OCRVersion

sys.path.insert(0, "/home/yartsevn/table-bakeoff")
from table_type_route import merge_cell_lines  # noqa: E402
from cell_detector_test import detect_cells, group_rows, page_image, table_box  # noqa: E402

MODEL = Path("/home/yartsevn/paddle-build/cell_wireless.onnx")
LAYOUT = Path("/home/yartsevn/table-bakeoff/detector/pp_doclayout_v3_dynbatch.onnx")
PAGE = sys.argv[1] if len(sys.argv) > 1 else "13611481-3.pdf"


def main() -> None:
    img = page_image(PAGE)
    layout = ort.InferenceSession(str(LAYOUT), providers=["CPUExecutionProvider"])
    det = table_box(img, layout)
    _, x0, y0, x1, y1 = det
    crop = img[max(0, int(y0) - 4):int(y1) + 4, max(0, int(x0) - 4):int(x1) + 4]
    eng = RapidOCR(params={"Rec.ocr_version": OCRVersion.PPOCRV5, "Rec.lang_type": LangRec.CYRILLIC,
                           "Rec.model_type": ModelType.MOBILE})
    ocr = eng(img)
    lines = []
    for q, t in zip(list(ocr.boxes) if ocr.boxes is not None else [], [t for t in (ocr.txts or [])]):
        a = np.asarray(q, dtype=float).reshape(-1, 2)
        lines.append({"text": t, "bbox": [float(a[:, 0].min()) - x0 + 4, float(a[:, 1].min()) - y0 + 4,
                                          float(a[:, 0].max()) - x0 + 4, float(a[:, 1].max()) - y0 + 4]})

    sess = ort.InferenceSession(str(MODEL), providers=["CPUExecutionProvider"])
    cells, secs = detect_cells(crop, sess)
    rows = group_rows(cells)
    print(f"страница {PAGE}: кроп {crop.shape[1]}x{crop.shape[0]}, ячеек {len(cells)}, полос {len(rows)}, {secs:.2f} с")
    for i, row in enumerate(rows, 1):
        top = min(c[1] for c in row)
        bottom = max(c[3] for c in row)
        print(f"\n  строка {i}: y {top:.0f}..{bottom:.0f} (высота {bottom - top:.0f}), ячеек {len(row)}")
        for c in row:
            text = merge_cell_lines(c, lines)
            print(f"    ячейка x {c[0]:.0f}..{c[2]:.0f} y {c[1]:.0f}..{c[3]:.0f} | {text[:70]}")


if __name__ == "__main__":
    main()
