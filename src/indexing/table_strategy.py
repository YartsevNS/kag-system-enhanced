"""Единая схема распознавания таблиц для страниц, где текстовый слой не помог.

Порядок и обоснование (замеры 27.09.2026 и 03.10.2026, сервер моделей 41):

  1. **Своя сетка по линиям** (`table_grid`) — первый шаг: она детерминированная и быстрая (доли секунды).
     Линии бланка видны отлично, а библиотечная модель структуры на плотных сканах ломает строки
     (23 из 30 нулевой высоты). Объединённые ячейки получаются сами: в полосе учитываются только реально
     нарисованные вертикальные линии. Текст в ячейки кладёт выбранный движок OCR (служба PP-OCRv5 или Occular).

  2. **Библиотечный разбор** (TableRecognizer из Occular) — только пока движок прежний. Он ищет те же ЛИНИИ,
     поэтому там, где сетка не нашлась, он бесполезен (0 таблиц даже с полными моделями). Со включённой
     службой PP-OCRv5 этот шаг пропускается: его работу закрывают своя сетка и модель зрения.

  3. **Модель зрения (VL) через API** — только если первый шаг не дал таблиц И опция включена в админке.
     Она читает таблицу без линий (скриншоты Excel с цветными заливками) и возвращает markdown с числами.
     Дорогая (десятки секунд на CPU), поэтому по умолчанию выключена, а выключенная — страница просто
     пропускается.

Движок OCR выбирается настройкой (`ocr/settings.service_enabled`): служба PP-OCRv5 на сервере моделей
(замер на одном хосте: 28,0 с и 4 контрольных числа против 43,9 с и 3 чисел у Occular) либо прежний
Occular в контейнере. Недоступная служба — откат на прежний движок с честной причиной, а не потеря страницы.

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


def raw_lines_from_engine(image: bytes) -> List[Dict[str, Any]]:
    """Строки OCR страницы от выбранного движка: служба PP-OCRv5, иначе прежний (Occular).

    Зачем переключатель, а не замена: движок — настройка, а не код. Пока служба выключена в админке,
    конвейер обязан работать ровно как раньше; недоступная служба — тоже откат, а не потеря страницы.
    Формат строк у обоих движков один (`text`, `quad`, `confidence`), поэтому вызывающий код не меняется.
    """
    try:
        from src.indexing.ocr_client import (
            lines_from_service,
            lines_local,
            service_available,
            service_enabled,
        )

        if service_enabled() and service_available():
            lines = lines_from_service(image)
            if lines:
                return lines
            logger.debug("[tables] служба OCR не вернула строк — пробую локальный движок")
        # Локальный запасной путь (PP-OCRv5 в контейнере): нужен, чтобы после удаления весов Occular
        # недоступная служба не оставляла сканы без распознавания вовсе.
        lines = lines_local(image)
        if lines:
            return lines
    except Exception as e:  # noqa: BLE001 — сбой клиента службы не должен ломать разбор
        logger.debug(f"[tables] движок службы недоступен, работаю прежним движком: {e}")
    return _raw_lines_from_occular(image)


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
    """Распознаватель вырезанных ячеек: служба PP-OCRv5, иначе прежний (Occular), один экземпляр на процесс.

    Проверено 27.09.2026: на вырезке строки Occular даёт тот же текст, что полный конвейер (уверенность
    0,78–0,97), но принимает сразу список рамок — то есть все ячейки распознаются одним вызовом
    (~50 мс на ячейку). Служба делает то же самое пачкой: изображение страницы + список рамок.
    """
    global _cell_recognizer, _cell_recognizer_failed
    if _cell_recognizer is not None or _cell_recognizer_failed:
        return _cell_recognizer

    try:
        from src.indexing.ocr_client import (
            cells_from_service,
            cells_local,
            service_available,
            service_enabled,
        )

        if service_enabled() and service_available():
            def recognize_via_service(image: Any, quads: Sequence[Any]) -> List[Tuple[str, float]]:
                got = cells_from_service(image, list(quads))
                return got or cells_local(image, list(quads))

            _cell_recognizer = recognize_via_service
            logger.info("[tables] распознавание ячеек: служба PP-OCRv5")
            return _cell_recognizer
        if cells_local:
            def recognize_local(image: Any, quads: Sequence[Any]) -> List[Tuple[str, float]]:
                return cells_local(image, list(quads))

            _cell_recognizer = recognize_local
            logger.info("[tables] распознавание ячеек: локальный PP-OCRv5")
            return _cell_recognizer
    except Exception as e:  # noqa: BLE001 — без службы работает прежний распознаватель
        logger.debug(f"[tables] служба OCR для ячеек недоступна: {e}")

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
            from src.indexing.table_grid import detect_grid, fill_cells
        except ModuleNotFoundError:      # проверка рядом со скриптом (модули лежат в data)
            from table_grid import detect_grid, fill_cells
    except Exception as e:  # noqa: BLE001
        return [], f"сетка по линиям недоступна: {type(e).__name__}: {str(e)[:80]}"

    array = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_COLOR)
    if array is None:
        return [], "не удалось прочитать изображение"

    started = time.time()
    grid, prepared = detect_grid(array)
    if grid is None:
        return [], f"линий сетки не найдено за {time.time() - started:.2f} с (не бланк с линиями)"

    # Распознавание идёт по ТОМУ ЖЕ изображению, что и сетка: страница уже выровнена, увеличение применено —
    # поэтому координаты строк и координаты сетки совпадают (на наклонённом скане это иначе ломает разбор).
    scale = float(grid.scale or 1.0)
    if raw_lines is None:
        ok, buf = cv2.imencode(".png", prepared)
        lines = raw_lines_from_engine(buf.tobytes() if ok else image)
        scale = 1.0                     # строки получены уже в системе координат сетки
    else:
        lines = list(raw_lines)         # координаты извне — переводим в масштаб сетки
    if not lines:
        return [], "строки распознавания не получены"

    scaled_lines = []
    for line in lines:
        quad = line.get("quad")
        if quad is None:
            continue
        try:
            arr = np.asarray(quad, dtype=np.float32).reshape(-1, 2) * scale
        except Exception:  # noqa: BLE001
            continue
        scaled_lines.append({"text": line["text"], "quad": arr.tolist()})

    recognizer = make_cell_recognizer()
    table = fill_cells(prepared, grid, scaled_lines, recognize_cells=recognizer)
    # Дочитывание ячеек вырезками — осознанная опция (по умолчанию выключена). Включает её смысл только
    # с отдельным движком для ячеек: тогда текст в ячейках читает выбранная для ячеек модель (например,
    # eslav, которая лучше берёт числа), а не общий проход по строкам страницы.
    if recognizer is not None:
        try:
            from src.indexing.ocr_client import cells_refine_enabled

            if cells_refine_enabled():
                from src.indexing.table_grid import refine_table_cells, table_quality

                changed = refine_table_cells(prepared, grid, table, recognizer)
                if changed:
                    table.notes.append(f"ячейки дочитаны вырезками: {changed}")
                    table.quality = table_quality(table.rows)
        except Exception as e:  # noqa: BLE001 — дочитывание не должно ломать разбор
            logger.debug(f"[tables] дочитывание ячеек не выполнено: {type(e).__name__}: {str(e)[:80]}")
    table.seconds = round(time.time() - started, 1)
    reason = (f"сетка по линиям: {grid.n_rows} строк × до {grid.max_cols} колонок, "
              f"качество {table.quality:.2f}, {time.time() - started:.1f} с")
    if table.n_rows < 2 or table.n_cols < 2 or table.quality < 0.25:
        return [], reason + " — признано непригодным"
    return [table], reason


def _translate_lines(lines: Optional[Sequence[Dict[str, Any]]], box: Sequence[float],
                     pad: int) -> List[Dict[str, Any]]:
    """Перевести строки страницы в координаты кропа (вычесть его начало).

    Нужно, чтобы не гонять распознавание дважды: строки страницы уже есть, а сетка по кропу работает
    в его системе координат.
    """
    if not lines:
        return []
    try:
        import numpy as np

        dx = max(0.0, float(box[0]) - pad)
        dy = max(0.0, float(box[1]) - pad)
        out: List[Dict[str, Any]] = []
        for line in lines:
            quad = line.get("quad")
            if quad is None:
                bbox = line.get("bbox")
                if bbox is None or len(bbox) < 4:
                    continue
                # Строки бывают и в формате bbox — не теряем их: разворачиваем в четырёхточечную рамку.
                quad = [[bbox[0], bbox[1]], [bbox[2], bbox[1]], [bbox[2], bbox[3]], [bbox[0], bbox[3]]]
            arr = np.asarray(quad, dtype=np.float32).reshape(-1, 2) - np.array([dx, dy], dtype=np.float32)
            out.append({"text": line.get("text", ""), "quad": arr.tolist()})
        return out
    except Exception as e:  # noqa: BLE001 — не смогли — пусть сетка распознает кроп сама
        logger.debug(f"[tables] строки в координаты кропа не переведены: {e}")
        return []


def recover_tables(image: bytes, *, ocr_lines: Optional[Sequence[OcrLine]] = None, page: int = 0,
                   recognizer: Any = None, vlm_caller: Any = None,
                   config: Optional[Dict[str, Any]] = None,
                   raw_lines: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Полная схема: сначала своя сетка по линиям, затем (если нужно и разрешено) модель зрения.

    Возвращает отчёт: список таблиц, каким путём получены, причина и время. Причина заполняется всегда —
    по ней в журнале видно, почему страница осталась без таблицы (выключенная опция не должна выглядеть
    как сбой распознавания).

    `raw_lines` — уже распознанные строки страницы (если они есть у вызывающего кода): по ним строится
    сетка (точнее по координатам) и работает предохранитель перед дорогим вызовом модели зрения.
    """
    started = time.time()
    detector_note = ""

    # Сначала свой разбор по линиям бланка: он даёт ячейки с текстом (библиотечная модель структуры
    # на плотных сканах ломает строки — 23 из 30 нулевой высоты, проверено 27.09.2026).
    grid_tables, grid_reason = recognize_grid_tables(image, raw_lines=raw_lines)
    if grid_tables:
        return {"tables": grid_tables, "technique": "occular-grid", "reason": grid_reason,
                "seconds": round(time.time() - started, 1)}

    # ── Второй шанс (опция, по умолчанию выключена): найти ОБЛАСТЬ таблицы и разобрать кроп, а не
    # страницу. Порядок именно такой, потому что кроп — не замена, а лекарство от конкретной болезни:
    # замер 04.10.2026 на чистом скане-бланке сетка по СТРАНИЦЕ дала 8×3 с качеством 0,98, а по кропу
    # того же бланка — 9×5 с качеством 0,59 (обрезанные линии путают детектор линий). Кроп нужен там,
    # где страница не сводится к одной таблице: мешает окружающий текст, или таблица прозаическая.
    try:
        from src.indexing import table_detector

        boxes = table_detector.detect_tables(image)
    except Exception as e:  # noqa: BLE001 — детектор не должен ломать разбор страницы
        logger.debug(f"[tables] этап детектора пропущен: {type(e).__name__}: {e}")
        boxes = None

    if boxes:
        cfg_det = table_detector.detector_config()
        pad = int(cfg_det.get("pad") or 8)
        best = boxes[0]
        crop_bytes = table_detector.crop_table(image, best["bbox"], pad=pad)
        if crop_bytes:
            crop_lines = _translate_lines(raw_lines, best["bbox"], pad)
            crop_tables, crop_reason = recognize_grid_tables(crop_bytes, raw_lines=crop_lines or None)
            if crop_tables:
                # Тип таблицы — по блокам, попавшим в область (числовая/прозаическая). Он нужен, чтобы
                # понимать, можно ли верить построчному разбору: прозаическую таблицу он дробит.
                verdict = {"kind": "?", "reason": "тип не определён"}
                try:
                    from src.indexing.table_type_route import lines_in_box, route

                    in_box = lines_in_box(raw_lines or [], best["bbox"])
                    if in_box:
                        verdict = route(in_box)
                        crop_tables[0].notes.append(
                            f"тип таблицы: {verdict['kind']} ({verdict['reason']})")
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"[tables] тип таблицы не определён: {type(e).__name__}: {e}")

                # Прозаическая таблица: построчный разбор рубит абзацы в строки (замер 05.10.2026:
                # конспект 38 строк при истинных 5). Если детектор ячеек включён и даёт МЕНЬШЕ строк —
                # значит он держит абзац в одной ячейке, и берём его результат.
                if verdict.get("kind") == "prose" and crop_lines:
                    try:
                        from src.indexing.table_cells import cells_available, extract_prose_table

                        if cells_available():
                            better = extract_prose_table(crop_bytes, crop_lines)
                            if better and len(better["rows"]) < crop_tables[0].n_rows:
                                cell_table = RecoveredTable(
                                    rows=better["rows"], source="rtdetr-cells",
                                    source_model="rt-detr-wireless-cell",
                                    notes=[f"собрано по ячейкам: {better['cells']}"])
                                cell_table.seconds = round(time.time() - started, 1)
                                logger.info(f"[tables] прозаическая таблица: построчно {crop_tables[0].n_rows} "
                                            f"строк → по ячейкам {len(better['rows'])}")
                                crop_tables = [cell_table]
                                verdict = {**verdict, "cells": better["cells"]}
                    except Exception as e:  # noqa: BLE001 — путь по ячейкам не должен ломать разбор
                        logger.debug(f"[tables] сборка по ячейкам не удалась: {type(e).__name__}: {e}")

                x0, y0, x1, y1 = best["bbox"]
                reason = (f"{grid_reason}; детектор области таблицы: скор {best['score']:.2f}, "
                          f"кроп {int(x1 - x0)}×{int(y1 - y0)} px → {crop_reason}; "
                          f"тип: {verdict['kind']}")
                return {"tables": crop_tables, "technique": "detector-grid", "reason": reason,
                        "seconds": round(time.time() - started, 1)}
            detector_note = f"детектор дал кроп, но {crop_reason}"
    if detector_note:
        grid_reason = f"{detector_note}; {grid_reason}"

    # Библиотечный разбор (TableRecognizer из Occular) — только пока движок прежний. Он сам ищет ЛИНИИ,
    # поэтому на страницах без сетки бесполезен (0 таблиц даже с полными моделями, проверено 27.09.2026),
    # а когда работает служба PP-OCRv5, держать его в конвейере незачем: линованные бланки разбирает своя
    # сетка, безлинейные — модель зрения (подключаемая опция).
    try:
        from src.indexing.ocr_client import service_enabled

        _service_on = service_enabled()
    except Exception:  # noqa: BLE001 — настройки недоступны: ведём себя как раньше
        _service_on = False

    if _service_on:
        tables, reason = [], f"{grid_reason}; библиотечный разбор пропущен (движок — служба PP-OCRv5)"
    else:
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

    # Предохранитель: модель зрения дорогая (десятки секунд на CPU), поэтому зовём её только если страница
    # ХОТЬ ЧЕМ-ТО похожа на таблицу. Пример цены ошибки: скриншот поисковой выдачи — модель искала таблицу
    # 72,3 с и вернула 5 строк мусора, который лёг в табличный слой документа (найдено 04.10.2026).
    probe_lines = [{"text": str(l.get("text") or "")} for l in (raw_lines or [])]
    if not probe_lines and ocr_lines:
        probe_lines = [{"text": getattr(l, "text", "")} for l in ocr_lines]
    if probe_lines:
        from src.indexing.vlm_tables import looks_like_table

        if not looks_like_table(probe_lines):
            return {"tables": [], "technique": "none",
                    "reason": f"{reason}; модель зрения не вызывалась — страница не похожа на таблицу "
                              f"(строк {len(probe_lines)}, чисел-значений мало)",
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


def render_document_pages(file_path: str, dpi: int = 200) -> List[bytes]:
    """Страницы документа как PNG-байты: картинка читается как есть, PDF рендерится.

    Зачем: скан бывает не только .png/.jpg, но и PDF без текстового слоя (сканы из МФУ, выгрузки
    из ЭДО). OCR и таблицы должны работать с ИЗОБРАЖЕНИЕМ страницы одинаково для обоих случаев.
    """
    from pathlib import Path

    path = Path(file_path)
    if path.suffix.lower() != ".pdf":
        return [path.read_bytes()]
    import fitz

    out: List[bytes] = []
    doc = fitz.open(str(path))
    for page in doc:
        out.append(page.get_pixmap(dpi=dpi).tobytes("png"))
    doc.close()
    return out


def has_text_layer(file_path: str, min_chars_per_page: int = 200) -> bool:
    """Есть ли у PDF текстовый слой. Для картинок — всегда False (слоя нет по определению)."""
    from pathlib import Path

    path = Path(file_path)
    if path.suffix.lower() != ".pdf":
        return False
    import fitz

    doc = fitz.open(str(path))
    try:
        pages = max(1, doc.page_count)
        chars = sum(len(page.get_text("text") or "") for page in doc)
    finally:
        doc.close()
    return chars >= min_chars_per_page * pages


def recover_tables_for_image_document(parsed: Any, file_path: str, config: Optional[Dict[str, Any]] = None,
                                      raw_lines: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Восстановить таблицы для документа-изображения (картинка ИЛИ скан-PDF) и вписать в разбор.

    Зачем именно так: дальше по конвейеру уже работает `_save_document_tables` — он берёт
    `parsed.pages[].tables` и сохраняет их в document_tables + строчный слой. Поэтому восстановленные
    таблицы достаточно положить в тот же разбор: не нужен отдельный путь записи, а провенанс
    (`extraction_method` = occular-table / vlm-<модель>) сохраняется штатно.
    """
    from pathlib import Path

    path = Path(file_path)
    suffix = path.suffix.lower()

    pages = getattr(parsed, "pages", None) or []
    if not pages:
        return {"applied": False, "reason": "в разборе документа нет страниц", "saved": 0}

    # Скан — это не только .png/.jpg: PDF без текстового слоя обязан идти тем же путём, иначе
    # смета-скан остаётся без таблиц (проверено 04.10.2026 на «Договор от 09.11.2020»).
    if suffix not in IMAGE_SUFFIXES and suffix != ".pdf":
        return {"applied": False, "reason": f"формат {suffix or '?'} не поддержан для таблиц", "saved": 0}
    if suffix == ".pdf" and has_text_layer(str(path)):
        return {"applied": False, "reason": "у PDF есть текстовый слой — таблицы берёт парсер PDF",
                "saved": 0}

    try:
        image = render_document_pages(str(path))[0]
    except Exception as e:  # noqa: BLE001
        return {"applied": False, "reason": f"страницу не удалось отрисовать: {e}", "saved": 0}

    report = recover_tables(image, page=getattr(pages[0], "page_num", 1) or 1, config=config,
                            raw_lines=raw_lines)
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
            # Честное качество разбора (было жёстко 0.6 — из-за этого мусорный разбор выглядел как
            # полноценный и попадал во фрагменты, поиск и страницу «Чанки»).
            "quality": round(float(getattr(table, "quality", 0.0) or 0.0), 3),
            "markdown": to_markdown(table.rows),
            "html": to_html(table.rows),
            "bbox": list(table.bbox or []),
            "extraction_method": (f"vlm-{table.source_model}" if table.source == "vlm" else "occular-table"),
        })
        # Арифметика — арбитр: структурные метрики (заполненность, TEDS) не замечают, что «1» стало «7» или
        # значение уехало на колонку. Вердикт пишем в разбор и в журнал: по нему видно, можно ли доверять числам.
        try:
            from src.indexing.table_validate import check_recovered_table

            _verdict = check_recovered_table(table)
            tables[-1]["arithmetic"] = _verdict.as_dict()
            logger.info(f"[tables] арифметика таблицы {len(tables)}: {_verdict.verdict}")
        except Exception as _ar_err:  # noqa: BLE001 — проверка не должна ломать восстановление
            logger.debug(f"арифметическая проверка таблицы не выполнена: {_ar_err}")

    logger.info(f"[tables] восстановлено таблиц для картинки: {len(report['tables'])} ({report['reason']})")
    return {"applied": True, "saved": len(report["tables"]), "technique": report["technique"],
            "reason": report["reason"], "seconds": report["seconds"]}
