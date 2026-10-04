"""Уточнение селектора numeric/prose: считать долю чисел по ОБЛАСТИ ТАБЛИЦЫ, а не по всей странице.

Сравниваем два варианта на документах с известной истиной:
  1) по всем строкам страницы (как в присланной эвристике);
  2) только по строкам, чей центр попал в бокс таблицы от нашего детектора PP-DocLayoutV3.
"""
import re
from pathlib import Path

import cv2
import fitz
import numpy as np
import onnxruntime as ort
from rapidocr import RapidOCR
from rapidocr.utils.typings import LangRec, ModelType, OCRVersion

DET = Path("/home/yartsevn/table-bakeoff/detector/pp_doclayout_v3_dynbatch.onnx")
TABLE_CLASS = 21
CONF = 0.5
NUM_ONLY = re.compile(r"^[-+]?\d*\.?\d+$")
DATE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
CASES = [
    ("смета «Договор 09.11.2020» (истина: таблица числовая, 8x3)", "/home/yartsevn/table-bakeoff/img/smeta.pdf"),
    ("13611481-3 конспект (истина: таблица прозаическая, 4x3)", "/home/yartsevn/table-bakeoff/img/13611481-3.pdf"),
]


def page_image(path: str) -> np.ndarray:
    p = Path(path)
    if p.suffix.lower() == ".pdf":
        png = fitz.open(path)[0].get_pixmap(dpi=200).tobytes("png")
        return cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    return cv2.imread(path)


def detect_table(img: np.ndarray):
    h, w = img.shape[:2]
    small = cv2.resize(img, (800, 800), interpolation=cv2.INTER_CUBIC)
    rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    pixel = np.transpose(rgb, (2, 0, 1))[None, ...]
    logits, boxes = DET_SESS.run(None, {"pixel_values": pixel})[:2]
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


def ratio(texts: list[str]) -> float:
    blocks = [t.strip() for t in texts if t and t.strip()]
    if not blocks:
        return 0.0
    hits = sum(1 for b in blocks
               if NUM_ONLY.match(b.replace(" ", "").replace(",", ".")) or DATE.match(b))
    return hits / len(blocks)


def verdict(r: float) -> str:
    return "numeric" if r >= 0.40 else "prose"


def main() -> None:
    global DET_SESS
    DET_SESS = ort.InferenceSession(str(DET), providers=["CPUExecutionProvider"])
    eng = RapidOCR(params={"Rec.ocr_version": OCRVersion.PPOCRV5, "Rec.lang_type": LangRec.CYRILLIC,
                           "Rec.model_type": ModelType.MOBILE})
    for label, path in CASES:
        img = page_image(path)
        out = eng(img)
        texts = [t for t in (out.txts or []) if t and t.strip()]
        boxes = list(out.boxes) if out.boxes is not None else []
        det = detect_table(img)
        inside = []
        if det is not None:
            _, x0, y0, x1, y1 = det
            for b, t in zip(boxes, texts):
                b = np.asarray(b, dtype=float).reshape(-1, 2)
                cx, cy = b[:, 0].mean(), b[:, 1].mean()
                if x0 <= cx <= x1 and y0 <= cy <= y1:
                    inside.append(t)
        r_page, r_tab = ratio(texts), ratio(inside)
        print(f"\n{label}")
        print(f"   детектор: {'бокс есть, скор %.2f' % det[0] if det else 'таблица не найдена'}"
              f"{' → %d строк внутри' % len(inside) if det else ''}")
        print(f"   по всей странице: строк {len(texts):4} | доля чисел {r_page:.2f} | вердикт {verdict(r_page)}")
        print(f"   по области таблицы: строк {len(inside):4} | доля чисел {r_tab:.2f} | вердикт {verdict(r_tab)}")


if __name__ == "__main__":
    main()
