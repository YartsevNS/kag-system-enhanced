"""Сервис OCR для KAG: PP-OCRv5 с кириллическим распознавателем (RapidOCR/ONNX, CPU).

Зачем: наш конвейер должен уметь брать OCR-строки не только из Occular. Движок вынесен отдельным сервисом,
как модель зрения, — тогда его можно менять и подключать/отключать без пересборки конвейера, а позже добавить
PaddleOCR-VL на GPU тем же интерфейсом.

Что важно знать (проверено 28.09.2026):
  * по умолчанию RapidOCR берёт PP-OCRv6, а он кириллицу НЕ поддерживает — нужен явный выбор PP-OCRv5 и языка
    (eslav или cyrillic) ПЕРЕЧИСЛЕНИЯМИ, иначе конструктор падает;
  * библиотека не потокобезопасна (одна модель в процессе, результат живёт до следующего вызова) — поэтому
    инференс сериализован блокировкой, а сервис однопоточный по факту обработки;
  * на CPU страница счёта-фактуры обрабатывается ~3 секунды (против ~32 у Occular).

Запуск на сервере моделей:
    ./.venv/bin/python ocr_service.py --port 8020 --lang cyrillic
Проверка:
    curl -s http://127.0.0.1:8020/health
    curl -s --data-binary @scan.png -H 'Content-Type: image/png' http://127.0.0.1:8020/ocr
    curl -s -H 'Content-Type: application/json' -d '{"image_b64":"...","quads":[[[0,0],[10,0],[10,5],[0,5]]]}' \
        http://127.0.0.1:8020/cells
"""
from __future__ import annotations

import argparse
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List

LANG_CHOICES = ("cyrillic", "eslav")
_engine = None
_cells_engine = None
_lang = "cyrillic"
_lock = threading.Lock()

CELLS_TARGET_H = 40       # целевая высота строки для распознавания вырезок
CELLS_MAX_SCALE = 4.0     # предел увеличения: выше — только медленнее, текст не улучшается


def build_engine(lang: str):
    """Собрать движок страницы: PP-OCRv5 + нужный язык + mobile. Значения — перечисления, строки не принимаются."""
    from rapidocr import RapidOCR
    from rapidocr.utils.typings import LangRec, ModelType, OCRVersion

    lang_enum = {"cyrillic": LangRec.CYRILLIC, "eslav": LangRec.ESLAV}[lang]
    return RapidOCR(params={
        "Rec.ocr_version": OCRVersion.PPOCRV5,
        "Rec.lang_type": lang_enum,
        "Rec.model_type": ModelType.MOBILE,
    })


def build_cells_engine(lang: str):
    """Движок для вырезанных ячеек: БЕЗ детектора текста.

    Проверено 03.10.2026 на накладной: на вырезке одной строки детектор не находит текст вовсе —
    пусто и без увеличения, и при увеличении ×2..×4 (детектору нужен контекст страницы). Без детектора
    та же вырезка читается («Универсальный», «Исправление №», «15855», «23 июня 2025 г.»), а при
    увеличении до ~40 px по высоте — уверенно.
    """
    from rapidocr import RapidOCR
    from rapidocr.utils.typings import LangRec, ModelType, OCRVersion

    lang_enum = {"cyrillic": LangRec.CYRILLIC, "eslav": LangRec.ESLAV}[lang]
    return RapidOCR(params={
        "Rec.ocr_version": OCRVersion.PPOCRV5,
        "Rec.lang_type": lang_enum,
        "Rec.model_type": ModelType.MOBILE,
        "Global.use_det": False,
    })


def recognize(image_bytes: bytes) -> Dict[str, Any]:
    """Распознать изображение. Возвращает строки с текстом и рамками — формат совпадает с нашим OCR."""
    global _engine
    import numpy as np
    from PIL import Image
    import io

    if _engine is None:
        _engine = build_engine(_lang)
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    array = np.array(image)
    with _lock:                                   # RapidOCR не потокобезопасен — сериализуем
        started = time.time()
        out = _engine(array)
        seconds = round(time.time() - started, 2)

    texts: List[str] = [str(t) for t in (getattr(out, "txts", None) or [])]
    boxes = getattr(out, "boxes", None)
    scores = getattr(out, "scores", None)
    lines: List[Dict[str, Any]] = []
    for i, text in enumerate(texts):
        item: Dict[str, Any] = {"text": text, "confidence": None}
        try:
            if boxes is not None and len(boxes) > i:
                arr = np.asarray(boxes[i], dtype=float).reshape(-1, 2)
                item["bbox"] = [float(arr[:, 0].min()), float(arr[:, 1].min()),
                                float(arr[:, 0].max()), float(arr[:, 1].max())]
                item["quad"] = arr.tolist()
        except Exception:  # noqa: BLE001 — рамка не критична для текста
            pass
        try:
            if scores is not None and len(scores) > i:
                item["confidence"] = float(scores[i])
        except Exception:  # noqa: BLE001
            pass
        if item["text"].strip():
            # Строки распознаны на изображении, которое прислали (масштаб 1): клиент по этому признаку
            # понимает, что координаты уже в системе присланного изображения и пересчёт не нужен.
            item["raw_scale"] = 1.0
            lines.append(item)
    return {"engine": f"rapidocr-ppocrv5-{_lang}", "seconds": seconds, "lines": lines,
            "cyrillic_chars": len(re.findall(r"[А-Яа-яЁё]", " ".join(texts)))}


def recognize_cells(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Распознать текст в вырезанных ячейках: на вход изображение страницы (base64 PNG) и рамки.

    Зачем одним запросом, а не вырезкой на ячейку: вырезок в таблице десятки, отдельное сетевое
    обращение на каждую было бы в разы дороже самого распознавания. Движок — распознаватель БЕЗ
    детектора: детектору нужна страница целиком, на вырезке одной строки он не находит текст вовсе
    (проверено 03.10.2026). Мелкие вырезки увеличиваются до ~40 px по высоте.
    """
    global _engine, _cells_engine
    import base64 as _b64
    import io

    import cv2
    import numpy as np
    from PIL import Image

    raw = _b64.b64decode(payload.get("image_b64") or "")
    if not raw:
        return {"error": "нет изображения страницы"}
    quads = payload.get("quads") or []
    if not isinstance(quads, list) or not quads:
        return {"error": "нет рамок ячеек"}
    if _cells_engine is None:
        _cells_engine = build_cells_engine(_lang)
    array = np.array(Image.open(io.BytesIO(raw)).convert("RGB"))
    height, width = array.shape[:2]
    started = time.time()
    texts: List[Dict[str, Any]] = []
    with _lock:
        for quad in quads:
            try:
                points = np.asarray(quad, dtype=float).reshape(-1, 2)
                x0 = max(0, int(points[:, 0].min()))
                x1 = min(width, int(np.ceil(points[:, 0].max())))
                y0 = max(0, int(points[:, 1].min()))
                y1 = min(height, int(np.ceil(points[:, 1].max())))
                crop = array[y0:y1, x0:x1]
                if crop.size == 0 or crop.shape[0] < 3 or crop.shape[1] < 3:
                    texts.append({"text": "", "confidence": 0.0})
                    continue
                # Мелкую вырезку увеличиваем: распознаватель читает строку высотой ~40 px, а на скане
                # строка бывает 13–15 px. Предел увеличения — чтобы не платить временем впустую.
                scale = 1.0
                if crop.shape[0] < CELLS_TARGET_H:
                    scale = min(CELLS_MAX_SCALE, CELLS_TARGET_H / max(1, crop.shape[0]))
                    crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
                out = _cells_engine(crop)
                text = out.txts[0] if getattr(out, "txts", None) else ""
                score = 0.0
                scores = getattr(out, "scores", None)
                if scores is not None and len(scores):
                    score = float(scores[0])
                texts.append({"text": str(text or ""), "confidence": score,
                              "scale": round(scale, 2)})
            except Exception as e:  # noqa: BLE001 — одна плохая рамка не должна ронять пачку
                texts.append({"text": "", "confidence": 0.0, "error": f"{type(e).__name__}"})
    return {"engine": f"rapidocr-ppocrv5-{_lang}", "seconds": round(time.time() - started, 2),
            "texts": texts}


class Handler(BaseHTTPRequestHandler):
    server_version = "kag-ocr/1.0"

    def _json(self, payload: Dict[str, Any], code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 — так требует BaseHTTPRequestHandler
        if self.path.startswith("/health"):
            self._json({"status": "ok", "engine": f"rapidocr-ppocrv5-{_lang}", "lang": _lang,
                        "ready": _engine is not None})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path.startswith("/cells"):
            length = int(self.headers.get("Content-Length") or 0)
            data = self.rfile.read(length) if length else b""
            if not data:
                self._json({"error": "пустое тело: передайте JSON с изображением и рамками"}, 400)
                return
            try:
                self._json(recognize_cells(json.loads(data)))
            except Exception as e:  # noqa: BLE001 — сервис обязан отвечать, а не падать
                self._json({"error": f"{type(e).__name__}: {str(e)[:200]}"}, 500)
            return
        if not self.path.startswith("/ocr"):
            self._json({"error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(length) if length else b""
        if not data:
            self._json({"error": "пустое тело: передайте изображение"}, 400)
            return
        try:
            self._json(recognize(data))
        except Exception as e:  # noqa: BLE001 — сервис обязан отвечать, а не падать
            self._json({"error": f"{type(e).__name__}: {str(e)[:200]}"}, 500)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[ocr] {self.address_string()} {fmt % args}", flush=True)


def main() -> int:
    global _lang
    ap = argparse.ArgumentParser(description="OCR-сервис KAG (PP-OCRv5 + кириллица)")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8020)
    ap.add_argument("--lang", default="cyrillic", choices=LANG_CHOICES)
    ap.add_argument("--warmup", action="store_true", help="прогреть движок при старте")
    args = ap.parse_args()
    _lang = args.lang
    if args.warmup:
        build_engine(_lang)
        print(f"[ocr] движок прогрет: PP-OCRv5/{_lang}/mobile", flush=True)
    print(f"[ocr] слушаю {args.host}:{args.port}, язык {_lang}", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
