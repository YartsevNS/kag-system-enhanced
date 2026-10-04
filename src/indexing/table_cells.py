"""Детектор ЯЧЕЕК таблицы (RT-DETR, ONNX) и сборка матрицы для прозаических таблиц.

Зачем отдельный путь: построчный разбор (наша сетка по линиям и find_tables) на прозаической таблице
считает ВИЗУАЛЬНЫЕ строки и рубит абзацы. Замер 05.10.2026 на конспекте 13611481-3: истина 5×3
(проверено глазами), наша сетка дала 38×3, SLANet 17×3, а детектор ячеек + склейка строк внутри ячейки —
ровно 5×3 с осмысленным текстом в каждой ячейке. На 13611481-5 то же: 5×3.
На числовой накладной тот же детектор переизбыточен (300 запросов, 28×18 при истинных ~27×20) — там
выигрывает наша сетка по линиям. Поэтому путь не заменяет числовой, а дополняет его.

Модель: официальные веса PaddlePaddle/RT-DETR-L_wireless_table_cell_det (Apache-2.0), конвертация
paddle2onnx на сборочной машине (PaddlePaddle в рантайм-образ не попадает).

Контракт снят с файла:
  входы:  image [N,3,640,640] float32, im_shape [[640,640]], scale_factor [[1,1]]
          (так модель и ждёт: с «правильными» Paddle-входами боксы выходят за пределы кропа);
  выходы: fetch_name_0 [300·N, 6] — строки [cls_id, score, x0, y0, x1, y1] в системе 640×640,
          fetch_name_1 — счётчик запросов (всегда 300, для отбора бесполезен).
Отбор: score ≥ порога; отбрасываем боксы меньше 8 px и больше 80 % площади кропа; NMS по IoU 0,5.

Безопасность: если модели нет, настройка выключена или onnxruntime недоступен — функции возвращают
None/пусто, и конвейер работает как раньше (исключений модуль не бросает).
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.indexing.table_type_route import line_box, merge_cell_lines

logger = logging.getLogger(__name__)

INPUT_SIZE = 640
DEFAULT_CONF = 0.40
MIN_CELL_PX = 8
MAX_CELL_AREA_SHARE = 0.80
NMS_IOU = 0.50
DEFAULT_MODEL_PATH = "/app/models/rt_detr_wireless_cell.onnx"
ENV_MODEL_PATH = "KAG_TABLE_CELLS_DETECTOR"

#: Пороги признака «таблица прозаическая» для таблиц из текстового слоя (калибровка 05.10.2026:
#: конспект 13611481-3 медиана длины ячейки 53 и доля длинных 0,77; 13611481-4 — 67/0,77;
#: 13611481-5 — 42/0,73; при этом DAQN405 21/0,33, Квитанции-2 15/0,29, то есть числовые не задеваются).
PROSE_MEDIAN_LEN = 25
PROSE_LONG_SHARE = 0.5
PROSE_MIN_ROWS = 4
PROSE_MIN_COLS = 2

_lock = threading.Lock()
_sessions: Dict[str, Any] = {}


def cells_config() -> Dict[str, Any]:
    """Настройки детектора ячеек из админки (`ocr/settings`)."""
    cfg: Dict[str, Any] = {"enabled": False, "path": "", "conf": DEFAULT_CONF}
    try:
        from src.api.services.config_store import config_store

        raw = config_store.get("ocr", "settings") or {}
        if isinstance(raw, dict):
            cfg["enabled"] = bool(raw.get("cells_detector_enabled", False))
            cfg["path"] = str(raw.get("cells_detector_path") or "").strip()
            try:
                cfg["conf"] = min(0.95, max(0.05, float(raw.get("cells_detector_conf") or DEFAULT_CONF)))
            except (TypeError, ValueError):
                pass
    except Exception as e:  # noqa: BLE001 — без настроек считаем путь выключенным
        logger.debug(f"[tables] настройки детектора ячеек недоступны: {e}")
    return cfg


def model_path(cfg: Optional[Dict[str, Any]] = None) -> str:
    cfg = cfg if cfg is not None else cells_config()
    return str(cfg.get("path") or os.environ.get(ENV_MODEL_PATH) or DEFAULT_MODEL_PATH)


def cells_available(cfg: Optional[Dict[str, Any]] = None) -> bool:
    cfg = cfg if cfg is not None else cells_config()
    if not cfg.get("enabled"):
        return False
    path = model_path(cfg)
    if not path or not os.path.exists(path):
        logger.info(f"[tables] детектор ячеек включён, но модели нет по пути {path!r}")
        return False
    try:
        import onnxruntime  # noqa: F401
    except Exception as e:  # noqa: BLE001
        logger.info(f"[tables] детектор ячеек включён, но onnxruntime недоступен: {e}")
        return False
    return True


def _session(path: str) -> Any:
    with _lock:
        if path not in _sessions:
            import onnxruntime as ort

            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 4
            opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            _sessions[path] = ort.InferenceSession(path, opts, providers=["CPUExecutionProvider"])
        return _sessions[path]


def _as_array(image: Any) -> Any:
    import cv2
    import numpy as np

    if isinstance(image, (bytes, bytearray)):
        return cv2.imdecode(np.frombuffer(bytes(image), np.uint8), cv2.IMREAD_COLOR)
    if isinstance(image, (str, os.PathLike)):
        return cv2.imread(str(image))
    return image


def detect_cells(image: Any, cfg: Optional[Dict[str, Any]] = None
                 ) -> Optional[List[Tuple[float, float, float, float]]]:
    """Боксы ячеек в пикселях присланного изображения.

    None — путь недоступен (выключен/нет модели) или изображение не прочиталось; пустой список — «ячеек
    не нашлось». Это разные вещи: None означает «этапа нет», и вызывающий код обязан работать без него.
    """
    cfg = cfg if cfg is not None else cells_config()
    if not cells_available(cfg):
        return None
    array = _as_array(image)
    if array is None:
        return None
    try:
        import cv2
        import numpy as np

        h, w = array.shape[:2]
        small = cv2.resize(array, (INPUT_SIZE, INPUT_SIZE), interpolation=cv2.INTER_LINEAR)
        rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        feed = {
            "image": np.transpose(rgb, (2, 0, 1))[None, ...],
            "im_shape": np.array([[float(INPUT_SIZE), float(INPUT_SIZE)]], np.float32),
            "scale_factor": np.array([[1.0, 1.0]], np.float32),
        }
        feed = {k: v for k, v in feed.items()
                if k in [i.name for i in _session(model_path(cfg)).get_inputs()]}
        outs = _session(model_path(cfg)).run(None, feed)
        sx, sy = w / float(INPUT_SIZE), h / float(INPUT_SIZE)
        conf = float(cfg.get("conf") or DEFAULT_CONF)

        boxes: List[Tuple[float, float, float, float]] = []
        for out in outs:
            arr = np.asarray(out)
            if arr.dtype != np.float32 or arr.ndim < 2 or arr.shape[-1] != 6:
                continue
            for row in arr.reshape(-1, 6):
                cls, score, x0, y0, x1, y1 = (float(v) for v in row)
                if score < conf or int(cls) != 0 or x1 <= x0 or y1 <= y0:
                    continue
                box = (x0 * sx, y0 * sy, x1 * sx, y1 * sy)
                bw, bh = box[2] - box[0], box[3] - box[1]
                if bw < MIN_CELL_PX or bh < MIN_CELL_PX or bw * bh > MAX_CELL_AREA_SHARE * w * h:
                    continue
                boxes.append(box)
        kept: List[Tuple[float, float, float, float]] = []
        for box in sorted(boxes, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]), reverse=True):
            if all(_iou(box, k) < NMS_IOU for k in kept):
                kept.append(box)
        return kept
    except Exception as e:  # noqa: BLE001 — сбой детектора не должен ломать разбор
        logger.warning(f"[tables] детектор ячеек не отработал: {type(e).__name__}: {e}")
        return None


def _iou(a: Sequence[float], b: Sequence[float]) -> float:
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def group_rows(cells: Sequence[Sequence[float]], min_overlap: float = 0.5
               ) -> List[List[Tuple[float, float, float, float]]]:
    """Ячейки → логические строки: по перекрытию высот, внутри строки — слева вправо."""
    rows: List[List[Tuple[float, float, float, float]]] = []
    for cell in sorted((tuple(float(v) for v in c) for c in cells), key=lambda c: (c[1], c[0])):
        for row in rows:
            top = min(c[1] for c in row)
            bottom = max(c[3] for c in row)
            height = min(bottom - top, cell[3] - cell[1])
            shift = min(bottom, cell[3]) - max(top, cell[1])
            if height > 0 and shift >= min_overlap * height:
                row.append(cell)
                break
        else:
            rows.append([cell])
    return [sorted(row, key=lambda c: c[0]) for row in rows]


def rows_from_cells(cells: Sequence[Sequence[float]], lines: Sequence[Any]) -> List[List[str]]:
    """Матрица текста: строки ячеек, в каждой ячейке — склеенный текст всех её блоков OCR."""
    matrix: List[List[str]] = []
    for row in group_rows(cells):
        matrix.append([merge_cell_lines(cell, lines) for cell in row])
    return matrix


def is_prose_rows(rows: Sequence[Sequence[Any]]) -> bool:
    """Похожа ли таблица на прозаическую — только по тексту (для таблиц из текстового слоя).

    Признак: длинные ячейки (медиана > 25 символов и больше половины ячеек длиннее 25) при нормальном
    размере таблицы (≥ 4 строк и ≥ 2 колонок). Калибровка — в docstring модуля; на числовых таблицах
    (короткие ячейки) признак не срабатывает.
    """
    cells = [str(c or "").strip() for row in rows for c in row]
    cells = [c for c in cells if c]
    if not cells:
        return False
    n_rows = len(rows)
    n_cols = max((len(r) for r in rows), default=0)
    if n_rows < PROSE_MIN_ROWS or n_cols < PROSE_MIN_COLS:
        return False
    lens = sorted(len(c) for c in cells)
    median = lens[len(lens) // 2]
    long_share = sum(1 for c in cells if len(c) > PROSE_MEDIAN_LEN) / len(cells)
    return median > PROSE_MEDIAN_LEN and long_share >= PROSE_LONG_SHARE


def extract_prose_table(image: Any, lines: Sequence[Any]) -> Optional[Dict[str, Any]]:
    """Собрать таблицу детектором ячеек: матрица + геометрия. None — путь недоступен или не сработал."""
    boxes = detect_cells(image)
    if not boxes:
        return None
    matrix = rows_from_cells(boxes, lines)
    rows = [r for r in matrix if any(str(c).strip() for c in r)]
    if len(rows) < 2 or max((len(r) for r in rows), default=0) < 2:
        return None
    return {"rows": rows, "headers": rows[0] if rows else [],
            "cells": len(boxes), "source": "rtdetr-cells"}


def prose_table_from_text_page(page: Any, bbox: Sequence[float],
                               dpi: int = 200) -> Optional[Dict[str, Any]]:
    """Прозаическая таблица из ТЕКСТОВОЙ страницы: рендерим область и берём слова самой страницы.

    Зачем рендерить: детектору ячеек нужна картинка. Зачем брать слова страницы, а не OCR: у текстового
    PDF координаты и символы точнее любого распознавания — распознавать то, что уже есть в тексте, незачем.

    `bbox` — рамка таблицы в точках PDF (как отдаёт find_tables), координаты слов переводятся в пиксели
    отрендеренной области (масштаб dpi/72).
    """
    import cv2
    import numpy as np
    import fitz

    try:
        x0, y0, x1, y1 = (float(v) for v in bbox[:4])
        zoom = dpi / 72.0
        pix = page.get_pixmap(clip=fitz.Rect(x0, y0, x1, y1), dpi=dpi)
        img = cv2.imdecode(np.frombuffer(pix.tobytes("png"), np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return None
        lines = []
        for w in page.get_text("words"):
            wx0, wy0, wx1, wy1, text = float(w[0]), float(w[1]), float(w[2]), float(w[3]), str(w[4])
            if not (x0 - 2 <= wx0 and wx1 <= x1 + 2 and y0 - 2 <= wy0 and wy1 <= y1 + 2):
                continue
            lines.append({"text": text,
                          "bbox": [(wx0 - x0) * zoom, (wy0 - y0) * zoom,
                                   (wx1 - x0) * zoom, (wy1 - y0) * zoom]})
        if not lines:
            return None
        result = extract_prose_table(img, lines)
        return result
    except Exception as e:  # noqa: BLE001 — сбой этого пути не должен ломать разбор страницы
        logger.warning(f"[tables] таблица по ячейкам из текстовой страницы не собралась: "
                       f"{type(e).__name__}: {e}")
        return None


def status() -> Dict[str, Any]:
    """Статус для админки/диагностики."""
    cfg = cells_config()
    path = model_path(cfg)
    return {"enabled": bool(cfg.get("enabled")), "available": cells_available(cfg), "path": path,
            "model_exists": bool(path and os.path.exists(path)), "conf": cfg.get("conf"),
            "loaded": list(_sessions), "prose_thresholds": {
                "median_len": PROSE_MEDIAN_LEN, "long_share": PROSE_LONG_SHARE,
                "min_rows": PROSE_MIN_ROWS, "min_cols": PROSE_MIN_COLS}}
