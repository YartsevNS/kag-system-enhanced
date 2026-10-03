"""Проверки после выката: настройки читаются, локальный OCR работает, порядок чтения на месте."""
import time

print("--- 1. версия SQLAlchemy (должна быть < 2.1: иначе config_store молча не читает настройки) ---")
import sqlalchemy  # noqa: E402

print("sqlalchemy:", sqlalchemy.__version__)

print("--- 2. config_store: чтение и запись ---")
from src.api.services.config_store import config_store  # noqa: E402

ocr_cfg = config_store.get("ocr", "settings")
print("ocr/settings:", ocr_cfg)
print("чтение работает:", "да" if ocr_cfg is not None else "НЕТ (get вернул None)")
existing = dict(ocr_cfg or {})
written = config_store.set("ocr", "settings", existing)
back = config_store.get("ocr", "settings")
print("запись работает:", "да" if written and back is not None else "НЕТ")

print("--- 3. движок OCR в контейнере ---")
from src.indexing.ocr_client import local_available, service_enabled  # noqa: E402

print("служба включена в настройках:", service_enabled(), "(ожидаем False — ещё не включали)")
print("локальный PP-OCRv5 доступен:", local_available())

print("--- 4. освободился ли образ от Occular ---")
import importlib.util as u  # noqa: E402

for name in ("occular", "torch"):
    print(f"{name}:", "есть" if u.find_spec(name) else "нет")

print("--- 5. распознавание реального скана локальным движком ---")
import os  # noqa: E402

from src.indexing.ocr_client import lines_local  # noqa: E402

candidates = [f for f in os.listdir("/app/data/uploads") if f.endswith(".png")][:3]
print("файлы для пробы:", candidates)
for name in candidates[:1]:
    data = open(os.path.join("/app/data/uploads", name), "rb").read()
    started = time.time()
    lines = lines_local(data)
    print(f"{name}: строк {len(lines)} за {time.time() - started:.1f} с; примеры:",
          [l["text"][:40] for l in lines[:5]])

print("--- 6. текст страницы с порядком чтения (тот же путь, что в конвейере) ---")
from src.indexing.hybrid_parser import HybridDocumentParser  # noqa: E402

parser = HybridDocumentParser.__new__(HybridDocumentParser)
parser._dpi = 200
parser._ocular = None
parser._ocular_available = False
if candidates:
    path = os.path.join("/app/data/uploads", candidates[0])
    text = parser._text_from_service(open(path, "rb").read())
    print("длина текста:", len(text or ""), "| первые 120 символов:", (text or "")[:120].replace("\n", " | "))
