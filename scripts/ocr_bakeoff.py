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
import re
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
    return {"engine": "occular", "seconds": round(time.time() - started, 1), "lines": lines,
            "text": " ".join(str(l.get("text") or "") for l in lines)}


def run_service_ocr(image: str, base_url: str = "http://192.168.50.41:8020/ocr") -> Dict[str, Any]:
    """Распознавание через службу на 41 (PP-OCRv5 cyrillic). Строки — с рамками, как у Occular."""
    import json
    import urllib.request

    data = pathlib.Path(image).read_bytes()
    started = time.time()
    req = urllib.request.Request(base_url, data=data, headers={"Content-Type": "image/png"})
    with urllib.request.urlopen(req, timeout=300) as r:
        payload = json.loads(r.read().decode())
    seconds = round(time.time() - started, 1)
    lines = payload.get("lines") or []
    return {"engine": payload.get("engine", "service"), "seconds": seconds, "lines": lines,
            "text": " ".join(str(l.get("text") or "") for l in lines)}


def _collect_strings(obj: Any, out: List[str], depth: int = 0) -> None:
    """Собрать все строки из структуры результата (у чужих библиотек структура меняется между версиями)."""
    if depth > 4 or len(out) > 5000:
        return
    if isinstance(obj, str):
        if obj.strip():
            out.append(obj)
        return
    if isinstance(obj, dict):
        for v in obj.values():
            _collect_strings(v, out, depth + 1)
        return
    if isinstance(obj, (list, tuple, set)):
        for v in obj:
            _collect_strings(v, out, depth + 1)
        return
    for attr in ("text", "value", "ocr_text", "words", "ocr_words", "text_lines", "lines"):
        v = getattr(obj, attr, None)
        if v is not None and not callable(v):
            _collect_strings(v, out, depth + 1)
    for attr in ("to_dict", "dict", "as_dict", "model_dump"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                _collect_strings(fn(), out, depth + 1)
            except Exception:  # noqa: BLE001
                pass
            break


def run_rdocs(image: str) -> Dict[str, Any]:
    """RussianDocsOCR: структура результата зависит от версии — собираем строки обходом."""
    from document_processing import Pipeline  # type: ignore

    started = time.time()
    pipeline = Pipeline(device="cpu", ocr="accurate")
    results = pipeline.process_img(image)
    seconds = round(time.time() - started, 1)
    words: List[str] = []
    # Текст у RussianDocsOCR лежит в .ocr_normalized (нормализованный) или .ocr — проверено на 4.6.0.
    for attr in ("ocr_normalized", "ocr"):
        value = getattr(results, attr, None)
        if value:
            if isinstance(value, str):
                words = [value]
            else:
                words = [str(x) for x in value]
            break
    if not words:
        _collect_strings(results, words)
    if not words:
        words = [f"<результат типа {type(results).__name__}; атрибуты: "
                 f"{[a for a in dir(results) if not a.startswith('_')][:14]}>"]
    return {"engine": "rdocs", "seconds": seconds, "lines": len(words), "text": " ".join(words),
            "doctype": str(getattr(results, "doctype", ""))[:40]}


def _make_rapidocr() -> Any:
    """Собрать RapidOCR с КИРИЛЛИЧЕСКИМ распознавателем (eslav), а не с латинским по умолчанию.

    По умолчанию RapidOCR берёт модель латиницы/китайского, и русский текст читается похожими латинскими
    буквами («Y Vicnpasnerse Ne 15855»): цифры выживают, слова нет. Конструктор в разных версиях принимает
    разные ключи, поэтому пробуем по очереди и запоминаем, что сработало.
    """
    from rapidocr import RapidOCR  # type: ignore

    # Важно (проверено 28.09.2026): по умолчанию RapidOCR 3.9 берёт распознаватель PP-OCRv6 small, а PP-OCRv6
    # кириллицу НЕ поддерживает — на попытку включить eslav он отвечает «Unsupported rec.lang_type». Поэтому
    # явно выбираем PP-OCRv5 (там есть eslav и cyrillic). Значения — ПЕРЕЧИСЛЕНИЯ, не строки: RapidOCR
    # отвергает строки с «must be Enum Type».
    attempts: List[Any] = []
    try:
        from rapidocr.utils.typings import LangRec, ModelType, OCRVersion  # type: ignore

        # Рабочая комбинация (подобрана перебором 28.09.2026): PP-OCRv5 + ESLAV + mobile. Без model_type
        # RapidOCR отвечает ошибкой «must be Enum Type»/«Unsupported», с PP-OCRv6 кириллицы нет вовсе.
        attempts.extend([
            ({"params": {"Rec.ocr_version": OCRVersion.PPOCRV5, "Rec.lang_type": LangRec.ESLAV,
                         "Rec.model_type": ModelType.MOBILE}}, "PP-OCRv5/eslav/mobile"),
            ({"params": {"Rec.ocr_version": OCRVersion.PPOCRV5, "Rec.lang_type": LangRec.CYRILLIC,
                         "Rec.model_type": ModelType.MOBILE}}, "PP-OCRv5/cyrillic/mobile"),
        ])
    except Exception:  # noqa: BLE001 — в другой версии перечисления могут называться иначе
        pass
    attempts.extend([
        ({"params": {"Rec.lang_type": "eslav"}}, "Rec.lang_type=eslav (строка)"),
        ({}, "по умолчанию (PP-OCRv6, без кириллицы)"),
    ])
    last_error = ""
    for kwargs, label in attempts:
        try:
            return RapidOCR(**kwargs), label
        except Exception as e:  # noqa: BLE001
            last_error = f"{type(e).__name__}: {str(e)[:80]}"
    raise RuntimeError(f"не удалось создать RapidOCR: {last_error}")


def run_paddle(image: str) -> Dict[str, Any]:
    """PP-OCRv5 (распознаватель eslav/cyrillic) через RapidOCR — ONNX на CPU."""
    started = time.time()
    engine, model_label = _make_rapidocr()
    out = engine(image)                      # в RapidOCR 3.x это единый объект, а не пара (result, elapsed)
    if isinstance(out, tuple):
        out = out[0]
    seconds = round(time.time() - started, 1)
    texts: List[str] = []
    for attr in ("txts", "texts"):
        v = getattr(out, attr, None)
        if v:
            texts = [str(x) for x in v]
            break
    if not texts and isinstance(out, (list, tuple)):
        texts = [str(item[1]) for item in out if isinstance(item, (list, tuple)) and len(item) > 1]
    if not texts:
        texts = [f"<результат типа {type(out).__name__}; атрибуты: "
                 f"{[a for a in dir(out) if not a.startswith('_')][:14]}>"]
    return {"engine": "paddle", "model": model_label, "seconds": seconds,
            "lines": len(texts), "text": " ".join(texts)}


ENGINES = {"occular": run_occular, "rdocs": run_rdocs, "paddle": run_paddle, "service": run_service_ocr}


def acceptance(result: Dict[str, Any]) -> Dict[str, Any]:
    """Приёмка: сколько строк таблицы проходит арифметику (количество × цена = стоимость).

    Используем тот же путь, что и прод: recognize_grid_tables (сетка по линиям + распознавание ячеек), только с
    переданными строками OCR (occular / service). Затем table_validate считает расхождения.
    """
    from src.indexing.table_strategy import recognize_grid_tables
    from src.indexing.table_validate import check_table

    lines = result.get("lines") or []
    image = result.get("image") or ""
    if not lines:
        return {"checked": 0, "ok": 0, "mismatch": 0, "verdict": "нет строк"}
    try:
        data = pathlib.Path(image).read_bytes()
        tables, reason = recognize_grid_tables(data, raw_lines=lines)
        if not tables:
            return {"checked": 0, "ok": 0, "mismatch": 0, "verdict": f"таблица не собрана: {reason}"}
        table = tables[0]
        verdict = check_table(table.rows, header_rows=1)
        return {"checked": verdict.checked, "ok": verdict.ok, "mismatch": verdict.mismatch,
                "verdict": verdict.verdict, "quality": getattr(table, "quality", 0.0),
                "reason": reason}
    except Exception as e:  # noqa: BLE001
        return {"checked": 0, "ok": 0, "mismatch": 0, "verdict": f"{type(e).__name__}: {str(e)[:120]}"}


def score(result: Dict[str, Any]) -> Dict[str, Any]:
    """Оценка текста: контрольные числа ищем ПО ЦИФРАМ, а не по формату с пробелами.

    Первая версия сравнивала строки целиком («13 959,9»), и это мерило разметку разрядов, а не распознавание:
    движок с верной кириллицей писал «13959,9» и получал ноль. Теперь из текста и эталона убираются все
    символы, кроме цифр, точки и запятой.
    """
    text = normalize(result.get("text", ""))
    cleaned_text = re.sub(r"[^0-9,.]+", "", text).replace(",", ".")
    found: List[str] = []
    for n in CONTROL_NUMBERS:
        key = re.sub(r"[^0-9,.]+", "", n).replace(",", ".")
        if key and key in cleaned_text:
            found.append(n)
    digits = sum(1 for ch in text if ch.isdigit())
    return {"control_found": len(found), "control_total": len(CONTROL_NUMBERS),
            "control_list": found, "digits": digits, "chars": len(text),
            "seconds": result.get("seconds"), "lines": result.get("lines")}


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
