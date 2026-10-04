"""Нарисовать найденные ячейки на кропе таблицы — чтобы ГЛАЗАМИ проверить истину.

Нужно потому, что «истина 4x3» для конспекта была принята ранее на глаз и может быть неверной:
детектор даёт 5 полос. Рисуем боксы с номерами полос и смотрим страницу.
"""
import sys
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

sys.path.insert(0, "/home/yartsevn/table-bakeoff")
from cell_detector_test import detect_cells, group_rows, page_image, table_box  # noqa: E402

LAYOUT = Path("/home/yartsevn/table-bakeoff/detector/pp_doclayout_v3_dynbatch.onnx")
MODEL = Path("/home/yartsevn/paddle-build/cell_wireless.onnx")
OUT = Path("/home/yartsevn/table-bakeoff/cells_vis.png")


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "13611481-3.pdf"
    img = page_image(name)
    layout = ort.InferenceSession(str(LAYOUT), providers=["CPUExecutionProvider"])
    _, x0, y0, x1, y1 = table_box(img, layout)
    crop = img[max(0, int(y0) - 4):int(y1) + 4, max(0, int(x0) - 4):int(x1) + 4].copy()
    sess = ort.InferenceSession(str(MODEL), providers=["CPUExecutionProvider"])
    cells, secs = detect_cells(crop, sess)
    rows = group_rows(cells)
    for i, row in enumerate(rows, 1):
        for c in row:
            cv2.rectangle(crop, (int(c[0]), int(c[1])), (int(c[2]), int(c[3])), (0, 0, 255), 3)
            cv2.putText(crop, f"r{i}", (int(c[0]) + 6, int(c[1]) + 34),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.1, (255, 0, 0), 3)
    cv2.imwrite(str(OUT), crop)
    print(f"{name}: ячеек {len(cells)}, полос {len(rows)}, {secs:.2f} с -> {OUT}")


if __name__ == "__main__":
    main()
