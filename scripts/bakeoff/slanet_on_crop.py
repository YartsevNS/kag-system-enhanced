"""Финальная матрица: SLANet-plus на КРОПЕ от нашего детектора таблиц против полной страницы.

Зачем: SLANet обучен на самой таблице, а не на странице. Раньше мы подавали полную страницу —
получали лишние строки. Здесь проверяем связку «наш детектор PP-DocLayoutV3 → кроп → SLANet-plus».
Разметка структурой: строки по <tr>, ячейки по <td> (включая colspan/rowspan в подсчёте ячеек).
"""
import re
import time
from pathlib import Path

import cv2
import fitz
import numpy as np
import onnxruntime as ort
from rapid_table import RapidTable, RapidTableInput
from rapid_table.utils.typings import ModelType

DET = Path("/home/yartsevn/table-bakeoff/detector/pp_doclayout_v3_dynbatch.onnx")
TABLE_CLASS, CONF = 21, 0.5
CASES = [
    ("смета «Договор 09.11.2020»", "/home/yartsevn/table-bakeoff/img/smeta.pdf", (8, 3)),
    ("13611481-3 конспект", "/home/yartsevn/table-bakeoff/img/13611481-3.pdf", (4, 3)),
]


def page_image(path: str) -> np.ndarray:
    p = Path(path)
    if p.suffix.lower() == ".pdf":
        png = fitz.open(path)[0].get_pixmap(dpi=200).tobytes("png")
        return cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    return cv2.imread(path)


def table_box(img: np.ndarray):
    h, w = img.shape[:2]
    small = cv2.resize(img, (800, 800), interpolation=cv2.INTER_CUBIC)
    rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    logits, boxes = DET_SESS.run(None, {"pixel_values": np.transpose(rgb, (2, 0, 1))[None, ...]})[:2]
    prob = 1.0 / (1.0 + np.exp(-logits[0]))
    cls, score = prob.argmax(axis=1), prob.max(axis=1)
    best = None
    for i in np.where(score >= CONF)[0]:
        if int(cls[i]) != TABLE_CLASS:
            continue
        cx, cy, bw, bh = boxes[0][i]
        cand = (float(score[i]), (cx - bw / 2) * w, (cy - bh / 2) * h, (cx + bw / 2) * w, (cy + bh / 2) * h)
        if best is None or cand[0] > best[0]:
            best = cand
    return best


def shape_of(html: str) -> tuple[int, int, int]:
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.S)
    cells = [len(re.findall(r"<td", r)) for r in rows]
    return len(rows), (max(cells) if cells else 0), sum(cells)


def run_case(engine, img, label: str):
    t0 = time.time()
    out = engine(img)
    secs = time.time() - t0
    html = out.pred_htmls[0] if out.pred_htmls else ""
    rows, cols, cells = shape_of(html)
    n_cells_bbox = len(out.cell_bboxes[0]) if out.cell_bboxes else 0
    return rows, cols, cells, n_cells_bbox, secs


def main() -> None:
    global DET_SESS
    DET_SESS = ort.InferenceSession(str(DET), providers=["CPUExecutionProvider"])
    engine = RapidTable(RapidTableInput(model_type=ModelType.SLANETPLUS))
    print(f"движок: SLANet-plus (rapid_table {__import__('rapid_table').__version__ if hasattr(__import__('rapid_table'), '__version__') else '?'})")
    for label, path, truth in CASES:
        img = page_image(path)
        det = table_box(img)
        print(f"\n=== {label} | истина {truth[0]}x{truth[1]} ===")
        if det is None:
            print("   детектор не нашёл таблицу — пропуск")
            continue
        _, x0, y0, x1, y1 = det
        pad = 8
        crop = img[max(0, int(y0) - pad):int(y1) + pad, max(0, int(x0) - pad):int(x1) + pad]
        print(f"   кроп детектора: {(x1-x0):.0f}x{(y1-y0):.0f} px, скор {det[0]:.2f}")

        rows, cols, cells, nbox, secs = run_case(engine, crop, label)
        print(f"   SLANet на КРОПЕ:    строк {rows:3} | макс колонок {cols} | ячеек {cells:3} | "
              f"боксов ячеек {nbox:3} | {secs:.1f} с")

        rows_f, cols_f, cells_f, nbox_f, secs_f = run_case(engine, img, label)
        print(f"   SLANet на СТРАНИЦЕ: строк {rows_f:3} | макс колонок {cols_f} | ячеек {cells_f:3} | "
              f"боксов ячеек {nbox_f:3} | {secs_f:.1f} с")
        rough = abs(rows - truth[0]) <= 1 and cols == truth[1]
        print(f"   кроп против истины: {'СОШЛОСЬ (в пределах строки)' if rough else 'расходится'}")


if __name__ == "__main__":
    main()
