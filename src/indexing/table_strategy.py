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
    to_html,
    to_markdown,
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


def _raw_lines_from_occular(image: bytes) -> List[Dict[str, Any]]:
    """Сырые строки OCR от Occular: текст, четырёхточечная рамка, уверенность.

    Нужны и табличному пути (там важны координаты и уверенность), и обычному (там достаточно текста и рамки).
    """
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
        out: List[Dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            quad = item.get("quad") or item.get("bbox")
            if text and quad is not None:
                out.append({"text": text, "quad": quad,
                            "confidence": float(item.get("confidence") or 0.0)})
        return out
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[tables] строки OCR от Occular получить не удалось: {e}")
        return []
    finally:
        if path:
            try:
                Path(path).unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass


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


_cell_recognizer: Any = None
_cell_recognizer_failed = False


def make_cell_recognizer() -> Optional[Any]:
    """Распознаватель Occular для вырезанных ячеек: один экземпляр на процесс.

    Проверено 27.09.2026: на вырезке строки он даёт тот же текст, что полный конвейер (уверенность 0,78–0,97),
    но принимает сразу список рамок — то есть все ячейки распознаются одним вызовом (~50 мс на ячейку).
    """
    global _cell_recognizer, _cell_recognizer_failed
    if _cell_recognizer is not None or _cell_recognizer_failed:
        return _cell_recognizer
    try:
        from occular import CRNNRecognizerONNX

        instance = CRNNRecognizerONNX(num_threads=4, lm=True)

        def recognize(image: Any, quads: Sequence[Any]) -> List[tuple]:
            return instance.recognize(image, list(quads))

        _cell_recognizer = recognize
    except Exception as e:  # noqa: BLE001 — без распознавателя путь всё равно работает (хуже)
        _cell_recognizer_failed = True
        logger.debug(f"[tables] распознаватель ячеек недоступен: {e}")
    return _cell_recognizer


def recognize_grid_tables(image: bytes, raw_lines: Optional[Sequence[Dict[str, Any]]] = None
                          ) -> Tuple[List[RecoveredTable], str]:
    """Табличный путь «сетка по линиям + раскладка текста по ячейкам».

    Возвращает (таблицы, причина). Причина заполнена всегда — по ней видно, почему пошли другим путём.
    """
    if not isinstance(image, (bytes, bytearray)):
        # Путь сетки работает с изображением: без байтов пропускаем его (и не ломаем вызовы с заглушкой).
        return [], "изображение передано не байтами — путь по линиям пропущен"
    try:
        import cv2
        import numpy as np

        try:
            from src.indexing.table_grid import SCALE, detect_grid, fill_cells
        except ModuleNotFoundError:      # проверка рядом со скриптом (модули лежат в data)
            from table_grid import SCALE, detect_grid, fill_cells
    except Exception as e:  # noqa: BLE001
        return [], f"сетка по линиям недоступна: {type(e).__name__}: {str(e)[:80]}"

    array = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_COLOR)
    if array is None:
        return [], "не удалось прочитать изображение"

    started = time.time()
    grid, scaled = detect_grid(array)
    if grid is None:
        return [], f"линий сетки не найдено за {time.time() - started:.2f} с (не бланк с линиями)"

    lines = list(raw_lines) if raw_lines is not None else _raw_lines_from_occular(image)
    if not lines:
        return [], "строки распознавания не получены"

    # Координаты строк переводим в масштаб сетки: сетку строим на увеличенном изображении.
    scaled_lines = []
    for line in lines:
        quad = line.get("quad")
        if quad is None:
            continue
        try:
            arr = np.asarray(quad, dtype=np.float32).reshape(-1, 2) * SCALE
        except Exception:  # noqa: BLE001
            continue
        scaled_lines.append({"text": line["text"], "quad": arr.tolist()})

    table = fill_cells(scaled, grid, scaled_lines, recognize_cells=make_cell_recognizer())
    table.seconds = round(time.time() - started, 1)
    reason = (f"сетка по линиям: {grid.n_rows} строк × до {grid.max_cols} колонок, "
              f"качество {table.quality:.2f}, {time.time() - started:.1f} с")
    if table.n_rows < 2 or table.n_cols < 2 or table.quality < 0.25:
        return [], reason + " — признано непригодным"
    return [table], reason


def recover_tables(image: bytes, *, ocr_lines: Optional[Sequence[OcrLine]] = None, page: int = 0,
                   recognizer: Any = None, vlm_caller: Any = None,
                   config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Полная схема: сначала свой Occular, затем (если нужно и разрешено) модель зрения.

    Возвращает отчёт: список таблиц, каким путём получены, причина и время. Причина заполняется всегда —
    по ней в журнале видно, почему страница осталась без таблицы (выключенная опция не должна выглядеть
    как сбой распознавания).
    """
    started = time.time()
    # Сначала свой разбор по линиям бланка: он даёт ячейки с текстом (библиотечная модель структуры
    # на плотных сканах ломает строки — 23 из 30 нулевой высоты, проверено 27.09.2026).
    grid_tables, grid_reason = recognize_grid_tables(image)
    if grid_tables:
        return {"tables": grid_tables, "technique": "occular-grid", "reason": grid_reason,
                "seconds": round(time.time() - started, 1)}

    tables, reason = recognize_occular_tables(image, ocr_lines=ocr_lines, recognizer=recognizer)
    reason = f"{grid_reason}; библиотечный разбор: {reason}"
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


# Расширения картинок: для них нет текстового слоя, поэтому таблицу может дать только OCR и модель зрения.
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".tiff", ".tif", ".bmp")


def recover_tables_for_image_document(parsed: Any, file_path: str, config: Optional[Dict[str, Any]] = None
                                      ) -> Dict[str, Any]:
    """Восстановить таблицы для документа-картинки и вписать их в разбор документа.

    Зачем именно так: дальше по конвейеру уже работает `_save_document_tables` — он берёт
    `parsed.pages[].tables` и сохраняет их в document_tables + строчный слой. Поэтому восстановленные
    таблицы достаточно положить в тот же разбор: не нужен отдельный путь записи, а провенанс
    (`extraction_method` = occular-table / vlm-<модель>) сохраняется штатно.
    """
    from pathlib import Path

    path = Path(file_path)
    if path.suffix.lower() not in IMAGE_SUFFIXES:
        return {"applied": False, "reason": "не картинка — этот путь только для изображений",
                "saved": 0}

    pages = getattr(parsed, "pages", None) or []
    if not pages:
        return {"applied": False, "reason": "в разборе документа нет страниц", "saved": 0}

    try:
        image = path.read_bytes()
    except Exception as e:  # noqa: BLE001
        return {"applied": False, "reason": f"файл не прочитан: {e}", "saved": 0}

    report = recover_tables(image, page=getattr(pages[0], "page_num", 1) or 1, config=config)
    if not report["tables"]:
        return {"applied": False, "reason": report["reason"], "saved": 0, "seconds": report["seconds"]}

    page = pages[0]
    tables = getattr(page, "tables", None)
    if tables is None:
        tables = []
        try:
            page.tables = tables
        except Exception:  # noqa: BLE001
            return {"applied": False, "reason": "в разборе страницы нельзя записать таблицы", "saved": 0}

    for table in report["tables"]:
        _data_rows = max(0, len(table.rows) - 1)          # без строки-шапки, как в document_tables
        tables.append({
            "rows": table.rows,
            "headers": table.rows[0] if table.rows else [],
            # Поля ниже ждёт этап сборки сегментов (chunk_type=table): без них от таблицы в метаданных
            # фрагмента остались бы нули.
            "row_count": _data_rows,
            "col_count": table.n_cols,
            "complex": False,
            "quality": 0.6,                     # распознавание моделью/OCR: качество ниже, чем у текстового PDF
            "markdown": to_markdown(table.rows),
            "html": to_html(table.rows),
            "bbox": list(table.bbox or []),
            "extraction_method": (f"vlm-{table.source_model}" if table.source == "vlm" else "occular-table"),
        })
    logger.info(f"[tables] восстановлено таблиц для картинки: {len(report['tables'])} ({report['reason']})")
    return {"applied": True, "saved": len(report["tables"]), "technique": report["technique"],
            "reason": report["reason"], "seconds": report["seconds"]}
