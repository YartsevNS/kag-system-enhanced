"""Замер детектора ячеек таблицы (RT-DETR wired/wireless) на НЕСКОЛЬКИХ страницах.

Цель — не подогнать одну страницу, а проверить обобщение: берём 6 страниц разного типа
(числовые смета/накладная, прозаические конспекты), для каждой:
  наша детекция области таблицы → кроп → детектор ячеек (wired и wireless) →
  раскладка НАШИХ строк OCR по ячейкам (боевая функция merge_cell_lines) →
  группировка ячеек в логические строки → матрица.

Печатаем по странице: боксы ячеек, строк×колонок, пустых ячеек, время, и саму матрицу
(первые строки) для глазами-проверки.
"""
import json
import sys
import time
from pathlib import Path

import cv2
import fitz
import numpy as np
import onnxruntime as ort
from rapidocr import RapidOCR
from rapidocr.utils.typings import LangRec, ModelType, OCRVersion

sys.path.insert(0, "/home/yartsevn/table-bakeoff")          # table_type_route.py (боевая логика)
from table_type_route import line_box, line_text, merge_cell_lines   # noqa: E402

IMGS = Path("/home/yartsevn/table-bakeoff/img")
LAYOUT = Path("/home/yartsevn/table-bakeoff/detector/pp_doclayout_v3_dynbatch.onnx")
CELL_MODELS = {
    "wired": Path("/home/yartsevn/paddle-build/cell_wired.onnx"),
    "wireless": Path("/home/yartsevn/paddle-build/cell_wireless.onnx"),
}
TABLE_CLASS, CONF = 21, 0.5
PAGES = [
    ("смета Договор 09.11.2020 (числовая 8x3)", "smeta.pdf"),
    ("накладная скан (числовая ~27x20)", "9e116f5c-edee-4574-a854-5361be9be855skan-nakladnoj3.png"),
    ("конспект 13611481-3 (проза 4x3)", "13611481-3.pdf"),
    ("HumynLabs 13611481-4 (проза)", "13611481-4.pdf"),
    ("HumynLabs 13611481-5 (проза)", "13611481-5.pdf"),
    ("HumynLabs 13611481-2", "13611481-2.pdf"),
]


def page_image(name: str) -> np.ndarray | None:
    p = IMGS / name
    if not p.exists():
        return None
    if p.suffix.lower() == ".pdf":
        png = fitz.open(str(p))[0].get_pixmap(dpi=200).tobytes("png")
        return cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    return cv2.imread(str(p))


def table_box(img: np.ndarray, sess) -> tuple | None:
    h, w = img.shape[:2]
    small = cv2.resize(img, (800, 800), interpolation=cv2.INTER_CUBIC)
    rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    logits, boxes = sess.run(None, {"pixel_values": np.transpose(rgb, (2, 0, 1))[None, ...]})[:2]
    prob = 1.0 / (1.0 + np.exp(-logits[0]))
    cls, score = prob.argmax(axis=1), prob.max(axis=1)
    best = None
    for i in np.where(score >= CONF)[0]:
        if int(cls[i]) != TABLE_CLASS:
            continue
        cx, cy, bw, bh = (float(v) for v in boxes[0][i])
        cand = (float(score[i]), (cx - bw / 2) * w, (cy - bh / 2) * h, (cx + bw / 2) * w, (cy + bh / 2) * h)
        if best is None or cand[0] > best[0]:
            best = cand
    return best


def detect_cells(crop: np.ndarray, sess, conf: float = 0.4) -> tuple[list, float]:
    """Боксы ячеек детектором RT-DETR.

    Контракт снят С ФАЙЛА (paddle2onnx, opset 16):
      входы:  im_shape [N,2] float32 (высота, ширина кропа), image [N,3,640,640],
              scale_factor [N,2] float32 (640/h, 640/w);
      выходы: fetch_name_0 [300*N, 6] float32 — строки [cls_id, score, x0, y0, x1, y1] в пикселях кропа,
              fetch_name_1 [N] int32 — сколько найденных ячеек действительно валидны.
    """
    h, w = crop.shape[:2]
    size = 640
    t0 = time.time()
    small = cv2.resize(crop, (size, size), interpolation=cv2.INTER_LINEAR)
    rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    # Служебные входы: проверено приборно (scripts/bakeoff/cell_input_probe.py) — модель ожидает
    # im_shape=[[640,640]] и scale_factor=(1,1), а боксы отдаёт в системе 640x640, не в пикселях кропа.
    feed = {
        "image": np.transpose(rgb, (2, 0, 1))[None, ...],
        "im_shape": np.array([[float(size), float(size)]], np.float32),
        "scale_factor": np.array([[1.0, 1.0]], np.float32),
    }
    feed = {k: v for k, v in feed.items() if k in [i.name for i in sess.get_inputs()]}
    outs = sess.run(None, feed)
    secs = time.time() - t0
    sx, sy = w / float(size), h / float(size)

    boxes: list = []
    counts: list = []
    for out in outs:
        arr = np.asarray(out)
        if arr.dtype == np.int32:                       # счётчик валидных детекций
            counts = arr.reshape(-1).tolist()
            continue
        if arr.ndim == 1:
            continue
        rows = arr.reshape(-1, arr.shape[-1])
        for row in rows:
            vals = [float(v) for v in row]
            cls, score, x0, y0, x1, y1 = vals[:6]
            if score < conf or x1 <= x0 or y1 <= y0 or cls != 0:
                continue
            box = [x0 * sx, y0 * sy, x1 * sx, y1 * sy]   # 640-пространство → пиксели кропа
            bw, bh = box[2] - box[0], box[3] - box[1]
            if bw < 8 or bh < 8 or bw * bh > 0.8 * w * h:
                continue                                  # мусор и «вся таблица» — не ячейка
            boxes.append([float(v) for v in box])
    keep: list = []
    for b in sorted(boxes, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]), reverse=True):
        if all(_iou(b, k) < 0.5 for k in keep):
            keep.append(b)
    return keep, secs


def _iou(a, b) -> float:
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def group_rows(cells: list[list[float]], min_overlap: float = 0.5) -> list[list[list[float]]]:
    """Ячейки → логические строки: по перекрытию высот (та же логика, что в merge_cell_lines)."""
    rows: list[list[list[float]]] = []
    for cell in sorted(cells, key=lambda c: (c[1], c[0])):
        for row in rows:
            top = min(c[1] for c in row)
            bottom = max(c[3] for c in row)
            height = min(bottom - top, cell[3] - cell[1])
            shift = min(bottom, cell[3]) - max(top, cell[1])
            if height > 0 and shift >= min_overlap * height:
                row.append(cell)
                break
        else:
            rows.append([cell])
    return [sorted(r, key=lambda c: c[0]) for r in rows]


def main() -> None:
    layout = ort.InferenceSession(str(LAYOUT), providers=["CPUExecutionProvider"])
    cell_sessions = {k: ort.InferenceSession(str(v), providers=["CPUExecutionProvider"])
                     for k, v in CELL_MODELS.items() if v.exists()}
    if not cell_sessions:
        print("нет ONNX детекторов ячеек:", ", ".join(str(p) for p in CELL_MODELS.values()))
        return
    for name, path in CELL_MODELS.items():
        if path.exists():
            s = cell_sessions[name]
            print(f"[{name}] входы: {[(i.name, i.shape) for i in s.get_inputs()]}"
                  f" | выходы: {[(o.name, o.shape) for o in s.get_outputs()]}")

    eng = RapidOCR(params={"Rec.ocr_version": OCRVersion.PPOCRV5, "Rec.lang_type": LangRec.CYRILLIC,
                           "Rec.model_type": ModelType.MOBILE})
    summary = {}
    for label, fname in PAGES:
        img = page_image(fname)
        if img is None:
            print(f"\n=== {label}: файла нет ({fname}) ===")
            continue
        print(f"\n=== {label} ===")
        det = table_box(img, layout)
        if det is None:
            print("   область таблицы не найдена — пропуск")
            continue
        _, x0, y0, x1, y1 = det
        crop = img[max(0, int(y0) - 4):int(y1) + 4, max(0, int(x0) - 4):int(x1) + 4]
        ocr = eng(img)
        lines = []
        for q, t in zip(list(ocr.boxes) if ocr.boxes is not None else [],
                        [t for t in (ocr.txts or [])]):
            arr = np.asarray(q, dtype=float).reshape(-1, 2)
            lines.append({"text": t, "bbox": [float(arr[:, 0].min()) - x0 + 4, float(arr[:, 1].min()) - y0 + 4,
                                              float(arr[:, 0].max()) - x0 + 4, float(arr[:, 1].max()) - y0 + 4]})
        in_crop = [l for l in lines if -5 <= l["bbox"][0] and l["bbox"][2] <= crop.shape[1] + 20]
        print(f"   кроп {crop.shape[1]}x{crop.shape[0]}, строк OCR в области: {len(in_crop)}")
        for kind, sess in cell_sessions.items():
            cells, secs = detect_cells(crop, sess)
            if not cells:
                print(f"   [{kind}] ячеек не найдено ({secs:.2f} с)")
                continue
            rows = group_rows(cells)
            texts = [merge_cell_lines(c, in_crop) for c in cells]
            empty = sum(1 for t in texts if not t)
            widths = [len(r) for r in rows]
            print(f"   [{kind}] ячеек {len(cells):3} | строк {len(rows):3} (колонок {min(widths)}..{max(widths)}) "
                  f"| пустых ячеек {empty:3} | {secs:.2f} с")
            for r in rows[:4]:
                sample = [merge_cell_lines(c, in_crop)[:22] for c in r[:4]]
                print("        " + " | ".join(sample))
            summary[f"{label} | {kind}"] = {"cells": len(cells), "rows": len(rows),
                                            "cols_min": min(widths), "cols_max": max(widths),
                                            "empty": empty, "seconds": round(secs, 2)}
    Path("/home/yartsevn/table-bakeoff/cell_det_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nсводка сохранена: /home/yartsevn/table-bakeoff/cell_det_summary.json")


if __name__ == "__main__":
    main()
