"""Проверка детектора области таблицы: скачиваем ONNX, смотрим подпись модели и запускаем на наших страницах.

Источники:
  1) RapidAI/RapidDoc на modelscope — PP-DocLayout_plus-L (ONNX, экосистема Apache-2.0, как rapidocr/rapid_table);
  2) HF-зеркало PP-DocLayoutV3 (RT-DETR, pixel_values -> logits/pred_boxes).

Что печатаем: входы/выходы, метки из метаданных, найденные боксы с классом и скором, время.
Что сохраняем: PNG с нарисованными боксами — посмотреть глазами.
"""
import json
import time
import urllib.request
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

OUT = Path("/home/yartsevn/table-bakeoff/detector")
OUT.mkdir(parents=True, exist_ok=True)

SOURCES = {
    "plus_l": "https://www.modelscope.cn/models/RapidAI/RapidDoc/resolve/v1.0.0/layout/PP-DocLayout_plus-L/pp_doclayout_plus_l.onnx",
    "v3": "https://huggingface.co/thesanogoeffect/PP-DocLayoutV3-ONNX/resolve/main/pp_doclayout_v3_dynbatch.onnx",
}

PAGES = {
    "смета_скан": "/home/yartsevn/table-bakeoff/img/smeta.pdf",
    "накладная_скан": "/home/yartsevn/table-bakeoff/img/9e116f5c-edee-4574-a854-5361be9be855skan-nakladnoj3.png",
    "титул_без_таблицы": "/home/yartsevn/table-bakeoff/img/13611481-1.pdf",
}


def fetch(name: str) -> Path | None:
    url = SOURCES[name]
    dst = OUT / url.split("/")[-1]
    if not dst.exists():
        try:
            print(f"скачиваю {name} …")
            urllib.request.urlretrieve(url, dst)
        except Exception as e:  # noqa: BLE001
            print(f"  не скачалось: {type(e).__name__} {str(e)[:120]}")
            return None
    print(f"{name}: {dst.name} {dst.stat().st_size/1024/1024:.1f} МБ")
    return dst


def page_image(path: str) -> np.ndarray | None:
    p = Path(path)
    if not p.exists():
        return None
    if p.suffix.lower() == ".pdf":
        import fitz
        return cv2.imdecode(np.frombuffer(fitz.open(path)[0].get_pixmap(dpi=200).tobytes("png"),
                                          np.uint8), cv2.IMREAD_COLOR)
    return cv2.imread(path)


def labels_of(sess) -> list[str]:
    meta = sess.get_modelmeta().custom_metadata_map or {}
    for key in ("character", "labels", "classes", "label_list"):
        if meta.get(key):
            return meta[key].splitlines()
    return []


def describe(sess) -> None:
    print("   входы:", [(i.name, i.shape, i.type) for i in sess.get_inputs()])
    print("   выходы:", [(o.name, o.shape) for o in sess.get_outputs()])
    lab = labels_of(sess)
    if lab:
        print(f"   метки из метаданных ({len(lab)}):", lab[:26])
        for probe in ("table", "табл"):
            idx = [i for i, s in enumerate(lab) if probe in s.lower()]
            if idx:
                print(f"   индекс класса {probe!r}:", idx)


def run_paddle_style(sess, img: np.ndarray):
    """Подпись image/im_shape/scale_factor (как у Paddle-экспорта)."""
    h, w = img.shape[:2]
    size = 800
    resized = cv2.resize(img, (size, size), interpolation=cv2.INTER_CUBIC)
    chw = np.transpose(resized.astype(np.float32) / 255.0, (2, 0, 1))[None, ...]
    feeds = {
        "image": chw,
        "im_shape": np.array([[float(size), float(size)]], dtype=np.float32),
        "scale_factor": np.array([[size / h, size / w]], dtype=np.float32),
    }
    names = [i.name for i in sess.get_inputs()]
    out = sess.run(None, {n: feeds[n] for n in names if n in feeds})
    boxes = out[0]
    cnt = int(out[1][0]) if len(out) > 1 and np.size(out[1]) else len(boxes)
    return boxes[:cnt] if cnt > 0 else np.empty((0, 6))


def run_vlm_style(sess, img: np.ndarray):
    """Подпись pixel_values -> logits[300,25] + pred_boxes[300,4] (RT-DETR из transformers)."""
    h, w = img.shape[:2]
    size = 800
    resized = cv2.resize(img, (size, size), interpolation=cv2.INTER_CUBIC)
    rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    pixel = np.transpose(rgb, (2, 0, 1))[None, ...]
    logits, boxes = sess.run(None, {"pixel_values": pixel})[:2]
    logits = logits[0]
    boxes = boxes[0]
    prob = 1.0 / (1.0 + np.exp(-logits))          # sigmoid: RT-DETR отдаёт логиты
    cls = prob.argmax(axis=1)
    score = prob.max(axis=1)
    keep = score >= 0.5
    # cxcywh в нормализованных координатах -> пиксели
    cx, cy, bw, bh = boxes[keep].T
    out = []
    for i, idx in enumerate(np.where(keep)[0]):
        out.append([float(cls[idx]), float(score[idx]),
                    (cx[i] - bw[i] / 2) * w, (cy[i] - bh[i] / 2) * h,
                    (cx[i] + bw[i] / 2) * w, (cy[i] + bh[i] / 2) * h])
    return np.array(out) if out else np.empty((0, 6))


def main() -> None:
    for name in SOURCES:
        model = fetch(name)
        if model is None:
            continue
        print(f"\n=== {name}: подпись модели ===")
        sess = ort.InferenceSession(str(model), providers=["CPUExecutionProvider"])
        describe(sess)
        labels = labels_of(sess)
        table_ids = [i for i, s in enumerate(labels) if "table" in s.lower()] or [15, 21]
        for page_name, path in PAGES.items():
            img = page_image(path)
            if img is None:
                print(f"   {page_name}: нет картинки")
                continue
            t0 = time.time()
            try:
                boxes = (run_paddle_style(sess, img)
                         if any(i.name == "image" for i in sess.get_inputs())
                         else run_vlm_style(sess, img))
            except Exception as e:  # noqa: BLE001
                print(f"   {page_name}: ошибка инференса {type(e).__name__} {str(e)[:120]}")
                continue
            secs = time.time() - t0
            tables = [b for b in boxes if int(b[0]) in table_ids]
            print(f"   {page_name}: {len(boxes)} боксов за {secs:.1f} с | таблиц {len(tables)}")
            for b in tables[:3]:
                nm = labels[int(b[0])] if labels and int(b[0]) < len(labels) else f"cls{int(b[0])}"
                print(f"      {nm} {b[1]:.2f} @ {[round(v) for v in b[2:6]]}")
            vis = img.copy()
            for b in tables:
                cv2.rectangle(vis, (int(b[2]), int(b[3])), (int(b[4]), int(b[5])), (0, 0, 255), 4)
            cv2.imwrite(str(OUT / f"{name}_{page_name}.png"), vis)


if __name__ == "__main__":
    main()
