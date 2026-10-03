"""Проверка содержимого образа: что убрано, что добавлено, на месте ли код."""
import importlib.util as u
import os

print("occular:", "есть" if u.find_spec("occular") else "НЕТ")
print("torch:", "есть" if u.find_spec("torch") else "НЕТ")
print("pyctcdecode:", "есть" if u.find_spec("pyctcdecode") else "НЕТ")
import cv2  # noqa: E402

print("cv2:", cv2.__version__)
print("ocr_client.py:", "на месте" if os.path.exists("/app/src/indexing/ocr_client.py") else "ОТСУТСТВУЕТ")

# Главная проверка: движок собирается БЕЗ сети (веса должны лежать в образе).
os.environ["HF_HUB_OFFLINE"] = "1"
from rapidocr import RapidOCR  # noqa: E402
from rapidocr.utils.typings import LangRec, ModelType, OCRVersion  # noqa: E402

engine = RapidOCR(params={"Rec.ocr_version": OCRVersion.PPOCRV5, "Rec.lang_type": LangRec.CYRILLIC,
                          "Rec.model_type": ModelType.MOBILE})
cells = RapidOCR(params={"Rec.ocr_version": OCRVersion.PPOCRV5, "Rec.lang_type": LangRec.CYRILLIC,
                         "Rec.model_type": ModelType.MOBILE, "Global.use_det": False})
print("движок страницы:", type(engine).__name__, "| движок вырезок:", type(cells).__name__)

import numpy as np  # noqa: E402

sample = np.full((60, 300, 3), 255, dtype=np.uint8)
out = engine(sample)
print("офлайн-инференс прошёл (пустая картинка):", out is None or not getattr(out, "txts", None))

from src.indexing.ocr_client import local_available  # noqa: E402
from src.indexing.hybrid_parser import HybridDocumentParser  # noqa: E402
print("local_available():", local_available(), "| HybridDocumentParser импортируется:",
      bool(HybridDocumentParser))
