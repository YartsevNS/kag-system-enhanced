"""Во сколько раз server-модель распознавания медленнее mobile на НАШЕМ железе.

Зачем: server-варианта для кириллицы в rapidocr нет (только mobile), поэтому «переключить кириллицу на
server» нельзя. Но фактор скорости можно измерить на китайской паре, где оба варианта есть
(ch_PP-OCRv5_rec_mobile против ch_PP-OCRv5_rec_server), — это и есть ответ «насколько медленнее»,
с оговоркой, что измерено на другой языковой паре.

Метрика: время на одну строку (одна и та же пачка реальных вырезок), размер файла модели.
"""
import time
from pathlib import Path

import cv2
import numpy as np
from rapidocr import RapidOCR
from rapidocr.utils.typings import ModelType, OCRVersion

IMG = Path("/home/yartsevn/table-bakeoff/img/9e116f5c-edee-4574-a854-5361be9be855skan-nakladnoj3.png")
H, W = 48, 320
LIMIT = 120


def crops(limit: int = LIMIT) -> list:
    eng = RapidOCR(params={"Rec.ocr_version": OCRVersion.PPOCRV5, "Rec.model_type": ModelType.MOBILE})
    img = cv2.imread(str(IMG))
    out = eng(img)
    boxes = list(out.boxes) if out.boxes is not None else []
    crops = []
    for b in boxes[:limit]:
        a = np.asarray(b, dtype=float).reshape(-1, 2)
        x0, y0 = int(a[:, 0].min()), int(a[:, 1].min())
        x1, y1 = int(a[:, 0].max()), int(a[:, 1].max())
        if x1 - x0 < 4 or y1 - y0 < 4:
            continue
        crops.append(cv2.resize(img[max(0, y0):y1, max(0, x0):x1], (W, H), interpolation=cv2.INTER_LINEAR))
    return crops


def engine(model_type):
    return RapidOCR(params={"Rec.ocr_version": OCRVersion.PPOCRV5, "Rec.model_type": model_type,
                            "Global.use_det": False})


def time_engine(eng, items: list) -> tuple[float, int]:
    t0 = time.time()
    texts = 0
    for crop in items:
        out = eng(crop)
        if out.txts:
            texts += sum(1 for t in out.txts if t and t.strip())
    return time.time() - t0, texts


def model_size(name: str) -> str:
    p = Path("/home/yartsevn/table-bakeoff/.venv/lib/python3.12/site-packages/rapidocr/models") / name
    return f"{p.stat().st_size/1024/1024:.1f} МБ" if p.exists() else "нет файла"


def main() -> None:
    items = crops()
    print(f"вырезок строк: {len(items)}")

    mobile = engine(ModelType.MOBILE)
    server = engine(ModelType.SERVER)

    print("=== прогрев (первый вызов собирает движок) ===")
    time_engine(mobile, items[:2])
    time_engine(server, items[:2])

    t_mob, n_mob = time_engine(mobile, items)
    t_srv, n_srv = time_engine(server, items)

    print(f"mobile: {t_mob:6.2f} с на {len(items)} строк | {t_mob/len(items)*1000:6.1f} мс/строка | строк распознано {n_mob}")
    print(f"server: {t_srv:6.2f} с на {len(items)} строк | {t_srv/len(items)*1000:6.1f} мс/строка | строк распознано {n_srv}")
    print(f"server медленнее mobile в {t_srv/max(t_mob, 1e-6):.2f} раза")
    print("\n=== размеры моделей ===")
    for name in ("ch_PP-OCRv5_rec_mobile.onnx", "ch_PP-OCRv5_rec_server.onnx",
                 "cyrillic_PP-OCRv5_rec_mobile.onnx", "eslav_PP-OCRv5_rec_mobile.onnx"):
        print(f"  {name}: {model_size(name)}")


if __name__ == "__main__":
    main()
