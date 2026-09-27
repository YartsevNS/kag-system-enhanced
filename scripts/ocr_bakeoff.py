"""Сравнительный прогон OCR-движков на НАШИХ сканах (запускается последовательно, не параллельно).

Зачем: вендорские цифры показывают, что по русскому движки расходятся сильно (у RussianDocsOCR в их замере
0,9745 против 0,92 у PaddleOCR и 0,41 у Tesseract), но проверять надо на своих документах и по своему критерию.
Критерий приёмки — не «красиво выглядит», а: (1) сколько строк таблицы проходит арифметику (количество × цена =
стоимость), (2) сколько контрольных чисел найдено, (3) время на страницу.

Движки:
  * occular     — текущий: конвейер с языковой моделью (для сравнения с тем, что есть);
  * rdocs       — RussianDocsOCR: MIT и на код, и на веса, свой детектор/распознавание;
  * paddle      — PP-OCRv5, распознаватель eslav/cyrillic (Apache 2.0), через RapidOCR/ONNX на CPU.

Запуск (на сервере моделей 41, из venv эксперимента):
    python ocr_bakeoff.py --engine all --image /path/скан.png --out /path/результаты.json
Результаты дописываются в JSON-файл — прогон можно прерывать и продолжать, двигаясь последовательно.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import time
from typing import Any, Dict, List

CONTROL_NUMBERS = ["13 959,9", "2 791.98", "16 751,90", "57 834,75", "23 651.83", "45 305,47", "43 250,36"]


def normalize(text: str) -> str:
    """Привести текст к виду для сравнения чисел: убрать пробелы-разделители тысяч и унифицировать запятую."""
    out = text.replace("\u00a0", " ").replace("\u2009", " ")
    for ch in ("\n", "\t", "|"):
        out = out.replace(ch, " ")
    return " ".join(out.split())


def run_occular(image: str) -> Dict[str, Any]:
    from src.indexing.table_strategy import _raw_lines_from_occular

    data = pathlib.Path(image).read_bytes()
    started = time.time()
    lines = _raw_lines_from_occular(data)
    return {"engine": "occular", "seconds": round(time.time() - started, 1),
            "lines": len(lines), "text": " ".join(str(l.get("text") or "") for l in lines)}


def run_rdocs(image: str) -> Dict[str, Any]:
    """RussianDocsOCR: конвейер отдаёт поля документа; берём весь распознанный текст строк."""
    from document_processing import Pipeline  # type: ignore

    started = time.time()
    pipeline = Pipeline(device="cpu", ocr="accurate")
    results = pipeline.process_img(image)
    seconds = round(time.time() - started, 1)
    words: List[str] = []
    for attr in ("ocr_words", "words", "text_lines", "lines"):
        value = getattr(results, attr, None)
        if value:
            words.extend([str(w) for w in value])
    if not words:
        for field in getattr(results, "fields", []) or []:
            value = getattr(field, "value", None) or (field.get("value") if isinstance(field, dict) else None)
            if value:
                words.append(str(value))
    return {"engine": "rdocs", "seconds": seconds, "lines": len(words), "text": " ".join(words)}


def run_paddle(image: str) -> Dict[str, Any]:
    """PP-OCRv5 (распознаватель eslav/cyrillic) через RapidOCR — ONNX на CPU."""
    from rapidocr import RapidOCR  # type: ignore

    started = time.time()
    engine = RapidOCR()
    result, _ = engine(image)
    seconds = round(time.time() - started, 1)
    texts = [str(item[1]) for item in (result or [])]
    return {"engine": "paddle", "seconds": seconds, "lines": len(texts), "text": " ".join(texts)}


ENGINES = {"occular": run_occular, "rdocs": run_rdocs, "paddle": run_paddle}


def score(result: Dict[str, Any]) -> Dict[str, Any]:
    text = normalize(result.get("text", ""))
    found = sum(1 for n in CONTROL_NUMBERS if normalize(n) in text)
    digits = sum(1 for ch in text if ch.isdigit())
    return {"control_found": found, "control_total": len(CONTROL_NUMBERS),
            "digits": digits, "chars": len(text), "seconds": result.get("seconds"),
            "lines": result.get("lines")}


def main() -> int:
    ap = argparse.ArgumentParser(description="Сравнение OCR-движков на наших сканах")
    ap.add_argument("--engine", default="all", help="occular | rdocs | paddle | all")
    ap.add_argument("--image", required=True)
    ap.add_argument("--out", default="/home/yartsevn/ocr-bakeoff/results.json")
    args = ap.parse_args()

    names = list(ENGINES) if args.engine == "all" else [args.engine]
    out_path = pathlib.Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    store: Dict[str, Any] = {}
    if out_path.exists():
        try:
            store = json.loads(out_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            store = {}

    for name in names:                      # последовательно: движки грузят модели и мешают друг другу в RAM
        print(f"--- движок {name}: запуск")
        try:
            result = ENGINES[name](args.image)
        except Exception as e:  # noqa: BLE001 — падение одного движка не должно останавливать замер
            result = {"engine": name, "error": f"{type(e).__name__}: {str(e)[:200]}"}
            print(f"    ОШИБКА: {result['error']}")
        else:
            result["score"] = score(result)
            print(f"    время {result['seconds']} с | строк {result['lines']} | "
                  f"контрольных чисел найдено {result['score']['control_found']}/{result['score']['control_total']}")
        store[os.path.basename(args.image) + "::" + name] = result
        out_path.write_text(json.dumps(store, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"    сохранено в {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
