"""Сервис OCR для KAG: PP-OCRv5 (RapidOCR/ONNX, CPU) — строки страницы и текст вырезанных ячеек.

Зачем отдельный сервис: движок распознавания надо менять и подключать/отключать без пересборки
конвейера, а позже добавить PaddleOCR-VL на GPU тем же интерфейсом.

ДВА ЯЗЫКА — ЭТО НЕ ПРИХОТЬ (замер 03.10.2026 на накладной, scripts/compare_lang.py):
  * `cyrillic` читает прозу чище («Счет-фактура»), но на плотной таблице путает похожие символы:
    контрольных чисел 1 из 6, название позиции «Балка» не нашлось;
  * `eslav` читает числа и названия позиций (4 из 6 контрольных чисел, «Балка» найдена), но прозу
    портит («Счет-фатура», «23 ион 2025 г.»).
Поэтому язык — параметр запроса: текст страницы распознаём одним, вырезки ячеек таблиц — другим.
Так один и тот же сервис даёт лучшее из двух моделей.

Что важно знать (проверено 28.09.2026 и 03.10.2026):
  * по умолчанию RapidOCR берёт PP-OCRv6, а он кириллицу НЕ поддерживает — нужен явный выбор PP-OCRv5 и
    языка ПЕРЕЧИСЛЕНИЯМИ, иначе конструктор падает;
  * библиотека не потокобезопасна: инференс сериализован блокировкой, сервис однопоточный по факту;
  * на вырезке одной строки детектор текста не находит ничего — вырезки распознаёт движок БЕЗ детектора
    (Global.use_det=False) с увеличением до ~40 px по высоте.

Запуск на сервере моделей:
    ./.venv/bin/python ocr_service.py --port 8020 --lang cyrillic --warmup
Проверка:
    curl -s http://127.0.0.1:8020/health
    curl -s --data-binary @scan.png -H 'Content-Type: image/png' "http://127.0.0.1:8020/ocr?lang=cyrillic"
    curl -s -H 'Content-Type: application/json' -d '{"image_b64":"...","quads":[[[0,0],[10,0],[10,5],[0,5]]],"lang":"eslav"}' \
        http://127.0.0.1:8020/cells
"""
from __future__ import annotations

import argparse
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

LANG_CHOICES = ("cyrillic", "eslav")
DEFAULT_LANG = "cyrillic"

_engines: Dict[str, Any] = {}          # язык -> движок страницы (с детектором)
_cells_engines: Dict[str, Any] = {}    # язык -> движок вырезок (без детектора)
_default_lang = DEFAULT_LANG
_lock = threading.Lock()

CELLS_TARGET_H = 40       # целевая высота строки для распознавания вырезок
CELLS_MAX_SCALE = 4.0     # предел увеличения: выше — только медленнее, текст не улучшается


def _lang_enum(lang: str):
    from rapidocr.utils.typings import LangRec

    return {"cyrillic": LangRec.CYRILLIC, "eslav": LangRec.ESLAV}[lang]


def _normalize_lang(lang: Optional[str]) -> str:
    """Привести запрошенный язык к поддерживаемому; неизвестный — язык по умолчанию (сервис не падает)."""
    value = str(lang or "").strip().lower()
    return value if value in LANG_CHOICES else _default_lang


def build_engine(lang: str):
    """Собрать движок страницы: PP-OCRv5 + нужный язык + mobile. Значения — перечисления, строки не принимаются."""
    from rapidocr import RapidOCR
    from rapidocr.utils.typings import ModelType, OCRVersion

    return RapidOCR(params={
        "Rec.ocr_version": OCRVersion.PPOCRV5,
        "Rec.lang_type": _lang_enum(lang),
        "Rec.model_type": ModelType.MOBILE,
    })


def build_cells_engine(lang: str):
    """Движок для вырезанных ячеек: БЕЗ детектора текста.

    На вырезке одной строки детектор не находит текст вовсе — ни без увеличения, ни при ×2..×4
    (детектору нужен контекст страницы). Без детектора та же вырезка читается, а с увеличением
    до ~40 px — уверенно.
    """
    from rapidocr import RapidOCR
    from rapidocr.utils.typings import ModelType, OCRVersion

    return RapidOCR(params={
        "Rec.ocr_version": OCRVersion.PPOCRV5,
        "Rec.lang_type": _lang_enum(lang),
        "Rec.model_type": ModelType.MOBILE,
        "Global.use_det": False,
    })


def get_engine(lang: str):
    """Движок страницы для языка: собирается при первом запросе этого языка и живёт в процессе."""
    if lang not in _engines:
        _engines[lang] = build_engine(lang)
    return _engines[lang]


def get_cells_engine(lang: str):
    """Движок вырезок для языка: собирается при первом запросе этого языка."""
    if lang not in _cells_engines:
        _cells_engines[lang] = build_cells_engine(lang)
    return _cells_engines[lang]


def recognize(image_bytes: bytes, lang: Optional[str] = None) -> Dict[str, Any]:
    """Распознать изображение. Возвращает строки с текстом и рамками — формат совпадает с нашим OCR."""
    import io

    import numpy as np
    from PIL import Image

    lang = _normalize_lang(lang)
    engine = get_engine(lang)
    array = np.array(Image.open(io.BytesIO(image_bytes)).convert("RGB"))
    with _lock:                                   # RapidOCR не потокобезопасен — сериализуем
        started = time.time()
        out = engine(array)
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
    return {"engine": f"rapidocr-ppocrv5-{lang}", "lang": lang, "seconds": seconds, "lines": lines,
            "cyrillic_chars": len(re.findall(r"[А-Яа-яЁё]", " ".join(texts)))}


def recognize_cells(payload: Dict[str, Any], lang: Optional[str] = None) -> Dict[str, Any]:
    """Распознать текст в вырезанных ячейках: на вход изображение страницы (base64 PNG) и рамки.

    Зачем одним запросом, а не вырезкой на ячейку: вырезок в таблице десятки, отдельное сетевое
    обращение на каждую было бы в разы дороже самого распознавания.
    """
    import base64 as _b64
    import io

    import cv2
    import numpy as np
    from PIL import Image

    lang = _normalize_lang(payload.get("lang") or lang)
    raw = _b64.b64decode(payload.get("image_b64") or "")
    if not raw:
        return {"error": "нет изображения страницы"}
    quads = payload.get("quads") or []
    if not isinstance(quads, list) or not quads:
        return {"error": "нет рамок ячеек"}
    engine = get_cells_engine(lang)
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
                out = engine(crop)
                text = out.txts[0] if getattr(out, "txts", None) else ""
                score = 0.0
                scores = getattr(out, "scores", None)
                if scores is not None and len(scores):
                    score = float(scores[0])
                texts.append({"text": str(text or ""), "confidence": score, "scale": round(scale, 2)})
            except Exception as e:  # noqa: BLE001 — одна плохая рамка не должна ронять пачку
                texts.append({"text": "", "confidence": 0.0, "error": f"{type(e).__name__}"})
    return {"engine": f"rapidocr-ppocrv5-{lang}", "lang": lang,
            "seconds": round(time.time() - started, 2), "texts": texts}


class Handler(BaseHTTPRequestHandler):
    server_version = "kag-ocr/2.0"

    def _json(self, payload: Dict[str, Any], code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _query_lang(self) -> Optional[str]:
        """Язык из строки запроса (?lang=eslav). Неизвестный игнорируем — ответит язык по умолчанию."""
        try:
            params = parse_qs(urlparse(self.path).query)
            return (params.get("lang") or [None])[0]
        except Exception:  # noqa: BLE001 — разбор запроса не должен ломать ответ
            return None

    def do_GET(self) -> None:  # noqa: N802 — так требует BaseHTTPRequestHandler
        if self.path.startswith("/health"):
            self._json({"status": "ok", "engine": f"rapidocr-ppocrv5-{_default_lang}",
                        "lang": _default_lang, "languages": list(LANG_CHOICES),
                        "loaded_page": sorted(_engines), "loaded_cells": sorted(_cells_engines),
                        "ready": bool(_engines)})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        lang = self._query_lang()
        if self.path.startswith("/cells"):
            length = int(self.headers.get("Content-Length") or 0)
            data = self.rfile.read(length) if length else b""
            if not data:
                self._json({"error": "пустое тело: передайте JSON с изображением и рамками"}, 400)
                return
            try:
                self._json(recognize_cells(json.loads(data), lang))
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
            self._json(recognize(data, lang))
        except Exception as e:  # noqa: BLE001 — сервис обязан отвечать, а не падать
            self._json({"error": f"{type(e).__name__}: {str(e)[:200]}"}, 500)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[ocr] {self.address_string()} {fmt % args}", flush=True)


def main() -> int:
    global _default_lang
    ap = argparse.ArgumentParser(description="OCR-сервис KAG (PP-OCRv5: cyrillic/eslav)")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8020)
    ap.add_argument("--lang", default=DEFAULT_LANG, choices=LANG_CHOICES,
                    help="язык по умолчанию, если в запросе не передан ?lang=")
    ap.add_argument("--warmup", action="store_true",
                    help="прогреть оба языка (текст и вырезки), чтобы первый запрос был быстрым")
    args = ap.parse_args()
    _default_lang = args.lang
    if args.warmup:
        for lang in LANG_CHOICES:
            build_engine(lang)
            build_cells_engine(lang)
        print(f"[ocr] движки прогреты: {', '.join(LANG_CHOICES)} (текст и вырезки)", flush=True)
    print(f"[ocr] слушаю {args.host}:{args.port}, язык по умолчанию {_default_lang}", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
