"""Единая схема распознавания таблиц для страниц, где текстовый слой не помог.

Порядок и обоснование (замеры 27.09.2026, сервер моделей 41):

  1. **Свой Occular** (`TableRecognizer`) — первый шаг, потому что он быстрый и детерминированный:
     структура таблицы берётся по геометрии, объединённые ячейки приходят как `rowspan`/`colspan`.
     На квитанции: 2 таблицы за 0,6 с, сетки 8×9 и 21×13, после раскладки текста в ячейках числа
     («Итого к оплате 12 503,51», «Капитальный ремонт 83,4 м2 | 25,8 | 2 133,3»).
     Ограничение по устройству: детектор ищет ЛИНИИ, поэтому таблицу, нарисованную цветными заливками
     без рамок, он не видит (0 таблиц даже с полными моделями).

  2. **Модель зрения (VL) через API** — только если первый шаг не дал таблиц И опция включена в админке.
     Она читает таблицу без линий и возвращает markdown с числами. Дорогая (десятки секунд на CPU),
     поэтому по умолчанию выключена, а выключенная — страница просто пропускается.

Модуль не бросает исключений и не теряет страницы: любой сбой шага превращается в причину в отчёте.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Sequence

from src.indexing.table_recovery import (
    OcrLine,
    RecoveredTable,
    SOURCE_OCCULAR_GRID,
    cells_from_grid_annotated,
)
from src.indexing.vlm_tables import get_vlm_tables_config, recognize_table

logger = logging.getLogger(__name__)


def recognize_occular_tables(image: bytes, ocr_lines: Optional[Sequence[OcrLine]] = None,
                             recognizer: Any = None) -> tuple[List[RecoveredTable], str]:
    """Шаг 1: таблицы своим Occular — сетка + текст по ячейкам.

    `recognizer` можно подставить в тестах: без него модуль пытается импортировать occular, и если его
    нет (например, на машине разработки), это не ошибка — просто честная причина в отчёте.
    """
    if recognizer is None:
        try:
            import cv2
            import numpy as np
            from occular import TableRecognizer
        except Exception as e:  # noqa: BLE001 — occular может быть не установлен
            return [], f"Occular недоступен: {type(e).__name__}: {str(e)[:120]}"
        recognizer = TableRecognizer()

    started = time.time()
    try:
        if isinstance(image, (bytes, bytearray)):
            import cv2
            import numpy as np
            array = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_COLOR)
            if array is None:
                return [], "не удалось прочитать изображение"
        else:
            array = image            # изображение уже подготовлено (готовый массив, тесты)
        tables = recognizer(array) or []
    except Exception as e:  # noqa: BLE001
        return [], f"Occular не смог обработать страницу: {type(e).__name__}: {str(e)[:120]}"

    if not tables:
        return [], f"Occular таблиц не нашёл за {time.time() - started:.1f} с (нет линий сетки)"

    # Строки OCR нужны, чтобы положить текст в ячейки; если их не передали — берём их у Occular.
    if not ocr_lines:
        ocr_lines = _lines_from_occular(image)
        if not ocr_lines:
            return [], "Occular нашёл таблицу, но не отдал строки текста для ячеек"

    result: List[RecoveredTable] = []
    for table in tables:
        rows, cols = table.get("rows") or [], table.get("cols") or []
        if not rows or not cols:
            continue
        grid, notes = cells_from_grid_annotated(rows, cols, list(ocr_lines))
        recovered = RecoveredTable(
            rows=grid,
            source=SOURCE_OCCULAR_GRID,
            source_model="occular-table",
            bbox=tuple(table["bbox"][:4]) if table.get("bbox") else None,
            confidence=float(table["bbox"][4]) if table.get("bbox") and len(table["bbox"]) > 4 else None,
            notes=notes,
        )
        recovered.seconds = round(time.time() - started, 1)
        if recovered.is_usable():
            result.append(recovered)
    if not result:
        return [], "Occular нашёл таблицу, но она получилась непригодной (меньше двух строк или колонок)"
    return result, f"таблиц от Occular: {len(result)} за {time.time() - started:.1f} с"


def _lines_from_occular(image: bytes) -> List[OcrLine]:
    """Строки текста с координатами от Occular (конвейер с порядком чтения, если модель есть)."""
    import tempfile
    from pathlib import Path

    path = ""
    try:
        from occular import OCRPipeline, Settings
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp.write(image)
            path = tmp.name
        try:
            pipe = OCRPipeline(Settings(reading_order=True))
        except Exception:  # noqa: BLE001 — модель порядка чтения может быть не скачана
            pipe = OCRPipeline(Settings())
        items = pipe.process_image(path) or []
        lines: List[OcrLine] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            quad = item.get("quad") or item.get("bbox")
            box = _quad_to_box(quad)
            if text and box:
                lines.append(OcrLine(text=text, bbox=box))
        return lines
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[tables] строки OCR от Occular получить не удалось: {e}")
        return []
    finally:
        if path:
            try:
                Path(path).unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass


def _quad_to_box(quad: Any) -> Optional[tuple]:
    """Привести координаты к виду (x0, y0, x1, y1): Occular отдаёт четыре точки."""
    if not quad:
        return None
    try:
        if isinstance(quad[0], (list, tuple)) and len(quad[0]) >= 2:
            xs = [float(p[0]) for p in quad]
            ys = [float(p[1]) for p in quad]
            return (min(xs), min(ys), max(xs), max(ys))
        if len(quad) >= 4:
            return (float(quad[0]), float(quad[1]), float(quad[2]), float(quad[3]))
    except Exception:  # noqa: BLE001
        return None
    return None


def recover_tables(image: bytes, *, ocr_lines: Optional[Sequence[OcrLine]] = None, page: int = 0,
                   recognizer: Any = None, vlm_caller: Any = None,
                   config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Полная схема: сначала свой Occular, затем (если нужно и разрешено) модель зрения.

    Возвращает отчёт: список таблиц, каким путём получены, причина и время. Причина заполняется всегда —
    по ней в журнале видно, почему страница осталась без таблицы (выключенная опция не должна выглядеть
    как сбой распознавания).
    """
    started = time.time()
    tables, reason = recognize_occular_tables(image, ocr_lines=ocr_lines, recognizer=recognizer)
    if tables:
        return {"tables": tables, "technique": "occular-grid", "reason": reason,
                "seconds": round(time.time() - started, 1)}

    cfg = config or get_vlm_tables_config()
    if not cfg.get("enabled"):
        return {"tables": [], "technique": "none",
                "reason": f"{reason}; модель зрения выключена в админке — страница пропущена",
                "seconds": round(time.time() - started, 1)}

    caller = vlm_caller or recognize_table
    table, vlm_reason = caller(image, page=page, config=cfg)
    if table is None:
        return {"tables": [], "technique": "none", "reason": f"{reason}; {vlm_reason}",
                "seconds": round(time.time() - started, 1)}
    return {"tables": [table], "technique": "vlm", "reason": f"{reason}; {vlm_reason}",
            "seconds": round(time.time() - started, 1)}
