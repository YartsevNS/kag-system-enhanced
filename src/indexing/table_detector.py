"""Детектор ОБЛАСТИ таблицы на странице (PP-DocLayoutV3, ONNX, CPU).

Зачем отдельный этап: наша сетка и модели структуры работают лучше по САМОЙ таблице, чем по всей
странице. Замер 04.10.2026 (раздел «SLANet и селектор» в docs/guides/table-structure-bakeoff.md):
SLANet-plus на кропе от этого детектора дал на смете 9×3 (истина 8×3), а на полной странице 14×3.

Подпись модели (снята с файла, не из документации — спека извне расходилась):
  вход  pixel_values [1, 3, 800, 800] float32, RGB, значения /255;
  выходы logits [1, 300, 25] (после sigmoid — вероятности классов) и pred_boxes [1, 300, 4]
         (нормализованные cxcywh);
  класс таблицы — 21 (не 2);
  координаты пересчитываются в пиксели присланного изображения здесь же.

Поведение: если модели нет, включена настройка выключена или onnxruntime недоступен — модуль честно
говорит «недоступен» (`detect_tables` → None), и конвейер работает как раньше. Исключений не бросает.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

TABLE_CLASS_ID = 21
DEFAULT_INPUT = 800
DEFAULT_CONF = 0.50
#: Путь к модели по умолчанию: сперва настройка, затем переменная окружения.
DEFAULT_MODEL_PATH = "/app/models/pp_doclayout_v3.onnx"
ENV_MODEL_PATH = "KAG_TABLE_DETECTOR"

_session_lock = threading.Lock()
_sessions: Dict[str, Any] = {}


def detector_config() -> Dict[str, Any]:
    """Настройки детектора из админки (`ocr/settings`) с безопасными значениями по умолчанию."""
    cfg: Dict[str, Any] = {"enabled": False, "path": "", "conf": DEFAULT_CONF, "pad": 8}
    try:
        from src.api.services.config_store import config_store

        raw = config_store.get("ocr", "settings") or {}
        if isinstance(raw, dict):
            cfg["enabled"] = bool(raw.get("detector_enabled", False))
            cfg["path"] = str(raw.get("detector_path") or "").strip()
            try:
                cfg["conf"] = min(0.95, max(0.05, float(raw.get("detector_conf") or DEFAULT_CONF)))
            except (TypeError, ValueError):
                pass
            try:
                cfg["pad"] = min(64, max(0, int(raw.get("detector_pad") or 8)))
            except (TypeError, ValueError):
                pass
    except Exception as e:  # noqa: BLE001 — без настроек детектор считаем выключенным
        logger.debug(f"[tables] настройки детектора недоступны: {e}")
    return cfg


def model_path(cfg: Optional[Dict[str, Any]] = None) -> str:
    """Путь к модели: настройка → переменная окружения → путь по умолчанию."""
    cfg = cfg if cfg is not None else detector_config()
    return str(cfg.get("path") or os.environ.get(ENV_MODEL_PATH) or DEFAULT_MODEL_PATH)


def detector_available(cfg: Optional[Dict[str, Any]] = None) -> bool:
    """Включён ли детектор и есть ли всё нужное для работы (модель, onnxruntime)."""
    cfg = cfg if cfg is not None else detector_config()
    if not cfg.get("enabled"):
        return False
    path = model_path(cfg)
    if not path or not os.path.exists(path):
        logger.info(f"[tables] детектор включён, но модели нет по пути {path!r} — работаем без него")
        return False
    try:
        import onnxruntime  # noqa: F401
    except Exception as e:  # noqa: BLE001
        logger.info(f"[tables] детектор включён, но onnxruntime недоступен: {e}")
        return False
    return True


def _session(path: str) -> Any:
    """Сессия onnxruntime на путь (собирается один раз на процесс)."""
    with _session_lock:
        if path not in _sessions:
            import onnxruntime as ort

            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 4
            opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            _sessions[path] = ort.InferenceSession(path, opts, providers=["CPUExecutionProvider"])
        return _sessions[path]


def _as_array(image: Any) -> Any:
    """bytes/путь/массив → BGR-массив cv2."""
    import cv2
    import numpy as np

    if isinstance(image, (bytes, bytearray)):
        return cv2.imdecode(np.frombuffer(bytes(image), np.uint8), cv2.IMREAD_COLOR)
    if isinstance(image, (str, os.PathLike)):
        return cv2.imread(str(image))
    return image


def detect_tables(image: Any, cfg: Optional[Dict[str, Any]] = None
                  ) -> Optional[List[Dict[str, Any]]]:
    """Найти области таблиц на странице.

    Возвращает список `{"score", "bbox": (x0, y0, x1, y1)}` в пикселях ПРИСЛАННОГО изображения,
    отсортированный по уверенности, либо `None`, если детектор недоступен (это не ошибка разбора,
    а «этапа нет» — вызывающий код обязан работать и без него). Пустой список — «таблиц не нашёл».
    """
    cfg = cfg if cfg is not None else detector_config()
    if not detector_available(cfg):
        return None
    array = _as_array(image)
    if array is None:
        return None
    try:
        import cv2
        import numpy as np

        h, w = array.shape[:2]
        small = cv2.resize(array, (DEFAULT_INPUT, DEFAULT_INPUT), interpolation=cv2.INTER_CUBIC)
        rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        pixel = np.transpose(rgb, (2, 0, 1))[None, ...]
        session = _session(model_path(cfg))
        logits, boxes = session.run(None, {"pixel_values": pixel})[:2]
        prob = 1.0 / (1.0 + np.exp(-logits[0]))
        cls, score = prob.argmax(axis=1), prob.max(axis=1)
        found: List[Dict[str, Any]] = []
        conf = float(cfg.get("conf") or DEFAULT_CONF)
        for i in np.where(score >= conf)[0]:
            if int(cls[i]) != TABLE_CLASS_ID:
                continue
            cx, cy, bw, bh = (float(v) for v in boxes[0][i])
            box = (max(0.0, (cx - bw / 2) * w), max(0.0, (cy - bh / 2) * h),
                   min(float(w), (cx + bw / 2) * w), min(float(h), (cy + bh / 2) * h))
            if box[2] - box[0] < 20 or box[3] - box[1] < 20:
                continue
            found.append({"score": float(score[i]), "bbox": box})
        found.sort(key=lambda item: item["score"], reverse=True)
        return found
    except Exception as e:  # noqa: BLE001 — сбой детектора не должен ломать разбор страницы
        logger.warning(f"[tables] детектор области таблицы не отработал: {type(e).__name__}: {e}")
        return None


def crop_table(image: Any, box: Sequence[float], pad: int = 8) -> Optional[bytes]:
    """Вырезать область таблицы как PNG-байты (с небольшим запасом по краям)."""
    array = _as_array(image)
    if array is None:
        return None
    try:
        import cv2

        h, w = array.shape[:2]
        x0 = max(0, int(box[0]) - pad)
        y0 = max(0, int(box[1]) - pad)
        x1 = min(w, int(box[2]) + pad)
        y1 = min(h, int(box[3]) + pad)
        if x1 - x0 < 20 or y1 - y0 < 20:
            return None
        ok, buf = cv2.imencode(".png", array[y0:y1, x0:x1])
        return buf.tobytes() if ok else None
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[tables] кроп таблицы не сделан: {e}")
        return None


def status() -> Dict[str, Any]:
    """Статус для диагностики/админки."""
    cfg = detector_config()
    path = model_path(cfg)
    return {
        "enabled": bool(cfg.get("enabled")),
        "available": detector_available(cfg),
        "path": path,
        "model_exists": bool(path and os.path.exists(path)),
        "conf": cfg.get("conf"),
        "class_id": TABLE_CLASS_ID,
        "loaded": list(_sessions),
    }
