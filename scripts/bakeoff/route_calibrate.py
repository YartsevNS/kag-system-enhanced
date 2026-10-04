"""Калибровка поколочного признака «числовая / прозаическая таблица».

Считаем по каждому столбцу области таблицы (детектор PP-DocLayoutV3 даёт бокс):
долю блоков, которые парсятся как числа. Печатаем сырые доли, чтобы правило взять из данных,
а не выдумать.
"""
import json
import re
from pathlib import Path

import cv2
import fitz
import numpy as np
import onnxruntime as ort
from rapidocr import RapidOCR
from rapidocr.utils.typings import LangRec, ModelType, OCRVersion

DET = Path("/home/yartsevn/table-bakeoff/detector/pp_doclayout_v3_dynbatch.onnx")
TABLE_CLASS, CONF = 21, 0.5
NUM_ONLY = re.compile(r"^[-+]?\d*\.?\d+$")
DATE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
CASES = [
    ("смета «Договор 09.11.2020» (числовая 8x3)", "/home/yartsevn/table-bakeoff/img/smeta.pdf"),
    ("13611481-3 конспект (прозаическая 4x3)", "/home/yartsevn/table-bakeoff/img/13611481-3.pdf"),
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


def is_number(text: str) -> bool:
    t = text.strip().replace(" ", "").replace("\u00a0", "").replace(",", ".")
    return bool(NUM_ONLY.match(t) or DATE.match(t))


def columns_of(items: list[tuple]) -> list[list[tuple]]:
    """Группировка блоков в столбцы: 1-D кластеризация центров по X.

    Допуск — 0,6 медианной ширины блока: внутри столбца центры стоят близко, между столбцами
    расстояние заметно больше. Простой и объяснимый признак; для кривых сканов не идеален, но
    нам он нужен только чтобы понять ТИП таблицы, а не собрать её.
    """
    if not items:
        return []
    widths = sorted(max(1.0, box[2] - box[0]) for _, box, _ in items)
    median_w = widths[len(widths) // 2]
    tol = 0.6 * median_w
    cols: list[list[tuple]] = []
    for item in sorted(items, key=lambda i: i[0]):
        for col in cols:
            if abs(item[0] - sum(c[0] for c in col) / len(col)) <= tol:
                col.append(item)
                break
        else:
            cols.append([item])
    return cols


def main() -> None:
    global DET_SESS
    DET_SESS = ort.InferenceSession(str(DET), providers=["CPUExecutionProvider"])
    eng = RapidOCR(params={"Rec.ocr_version": OCRVersion.PPOCRV5, "Rec.lang_type": LangRec.CYRILLIC,
                           "Rec.model_type": ModelType.MOBILE})
    report = {}
    for label, path in CASES:
        img = page_image(path)
        out = eng(img)
        texts = [t for t in (out.txts or []) if t and t.strip()]
        boxes = [np.asarray(b, dtype=float).reshape(-1, 2) for b in (list(out.boxes) if out.boxes is not None else [])]
        det = table_box(img)
        items = []
        if det is not None:
            _, x0, y0, x1, y1 = det
            for b, t in zip(boxes, texts):
                cx, cy = b[:, 0].mean(), b[:, 1].mean()
                if x0 <= cx <= x1 and y0 <= cy <= y1:
                    items.append((float(cx), (float(b[:, 0].min()), float(b[:, 1].min()),
                                              float(b[:, 0].max()), float(b[:, 1].max())), t))
        cols = columns_of(items)
        print(f"\n=== {label} ===")
        print(f"   блоков в области таблицы: {len(items)}, столбцов по геометрии: {len(cols)}")
        stats = []
        for i, col in enumerate(cols):
            col = sorted(col, key=lambda c: c[1][1])
            nums = sum(1 for c in col if is_number(c[2]))
            share = nums / len(col) if col else 0.0
            head = " / ".join(c[2] for c in col[:2])[:46]
            stats.append({"i": i, "blocks": len(col), "numeric_ratio": round(share, 2)})
            print(f"   столбец {i + 1}: блоков {len(col):3} | доля чисел {share:.2f} | примеры: {head}")
        numeric_cols = [s for s in stats if s["numeric_ratio"] >= 0.5 and s["blocks"] >= 2]
        report[label] = {"blocks": len(items), "columns": stats, "numeric_columns": len(numeric_cols)}
        print(f"   ИТОГ: столбцов {len(stats)}, из них числовых (>=0.5 и >=2 блоков) {len(numeric_cols)} | "
              f"вердикт {'numeric' if len(numeric_cols) >= 2 else 'prose'}")
    Path("/home/yartsevn/table-bakeoff/route_calibration.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
