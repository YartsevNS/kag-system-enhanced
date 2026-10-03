"""Клиент OCR-службы (PP-OCRv5, RapidOCR/ONNX): строки страницы и текст вырезанных ячеек.

Зачем служба, а не библиотека в контейнере: движок распознавания должен меняться без пересборки
конвейера. Служба живёт на сервере моделей (41), отвечает по HTTP и умеет то же, что Occular:

  * `POST /ocr`   — строки страницы с рамками (замена `_raw_lines_from_occular`);
  * `POST /cells` — текст вырезанных ячеек пачкой (замена распознавателя ячеек Occular).

Почему PP-OCRv5 и кириллица: PP-OCRv6 кириллицу не поддерживает вовсе (проверено ошибками самой
библиотеки: `Unsupported rec.lang_type='eslav'` для v6 small и medium), а PP-OCRv5 cyrillic даёт
верный русский текст. Замер 03.10.2026 на одном хосте (прод, контейнер api): накладная —
PP-OCRv5 28,0 с и 4 контрольных числа против Occular 43,9 с и 3 чисел.

Настройки — namespace `ocr`, ключ `settings` (там же, где force_ocr/dpi):
`service_enabled`, `service_url`, `service_timeout_s`. По умолчанию ВЫКЛЮЧЕНО: пока выключено,
конвейер работает как раньше (Occular), поэтому включение — отдельное осознанное действие.

Модуль никогда не бросает исключение: недоступная служба — это причина в отчёте, а не потерянная
страница. Недоступность кэшируется на минуту, чтобы недоступный сервер не тормозил каждую страницу.
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

DEFAULT_URL = "http://192.168.50.41:8020"
DEFAULT_TIMEOUT_S = 120
PROBE_TIMEOUT_S = 3.0
PROBE_TTL_S = 60.0            # как часто перепроверять недоступную службу
MAX_CELLS_PER_CALL = 80       # предел вырезок в одном запросе: страховка от гигантских таблиц

# Языки PP-OCRv5 для русского. Оба — про кириллицу, но с разными сильными сторонами (замер 03.10.2026):
#   cyrillic — чище на обычной прозе («Счет-фактура»);
#   eslav    — читает числа и названия позиций («Балка», 4 контрольных числа из 6 против 1).
LANGUAGES = ("cyrillic", "eslav")
DEFAULT_TEXT_LANG = "cyrillic"
DEFAULT_CELLS_LANG = "eslav"

_available: Optional[bool] = None
_checked_at: float = 0.0


def get_service_config() -> Dict[str, Any]:
    """Настройки OCR-службы. Ошибка чтения настроек = служба выключена (поведение как раньше).

    Языков два, и это не прихоть: замер 03.10.2026 на накладной дал «cyrillic» 1 контрольное число из 6
    (и не нашёл «Балка»), а «eslav» — 4 из 6 (но прозу портит: «Счет-фатура»). Поэтому текст страницы и
    вырезки ячеек распознаются разными моделями.
    """
    cfg: Dict[str, Any] = {"enabled": False, "url": DEFAULT_URL, "timeout_s": DEFAULT_TIMEOUT_S,
                           "text_lang": DEFAULT_TEXT_LANG, "cells_lang": DEFAULT_CELLS_LANG}
    try:
        from src.api.services.config_store import config_store

        raw = config_store.get("ocr", "settings") or {}
        if isinstance(raw, dict):
            cfg["enabled"] = bool(raw.get("service_enabled", False))
            url = str(raw.get("service_url") or "").strip().rstrip("/")
            if url:
                cfg["url"] = url
            try:
                cfg["timeout_s"] = max(5, min(1800, int(raw.get("service_timeout_s") or DEFAULT_TIMEOUT_S)))
            except (TypeError, ValueError):
                pass
            text_lang = str(raw.get("service_text_lang") or "").strip().lower()
            if text_lang in LANGUAGES:
                cfg["text_lang"] = text_lang
            cells_lang = str(raw.get("service_cells_lang") or "").strip().lower()
            if cells_lang in LANGUAGES:
                cfg["cells_lang"] = cells_lang
    except Exception as e:  # noqa: BLE001 — без настроек работаем как до службы
        logger.debug(f"[ocr-service] настройки недоступны, служба выключена: {e}")
        cfg["enabled"] = False
    return cfg


def service_enabled() -> bool:
    """Включена ли служба в настройках (без сетевой проверки)."""
    return bool(get_service_config().get("enabled"))


def reset_probe() -> None:
    """Сбросить кэш доступности (после поднятия службы — чтобы конвейер увидел её сразу)."""
    global _available, _checked_at
    _available = None
    _checked_at = 0.0


def service_available(force: bool = False) -> bool:
    """Доступна ли служба. Результат кэшируется: недоступный сервер не тормозит каждую страницу."""
    global _available, _checked_at
    now = time.time()
    if not force and _available is not None and now - _checked_at < PROBE_TTL_S:
        return _available
    cfg = get_service_config()
    ok = False
    try:
        with urllib.request.urlopen(f"{cfg['url']}/health", timeout=PROBE_TIMEOUT_S) as response:
            data = json.loads(response.read())
        ok = str(data.get("status") or "") == "ok"
    except Exception as e:  # noqa: BLE001 — недоступность службы не ошибка конвейера
        logger.debug(f"[ocr-service] служба недоступна ({cfg['url']}): {type(e).__name__}: {str(e)[:80]}")
        ok = False
    _available, _checked_at = ok, now
    return ok


def _post(path: str, body: bytes, content_type: str, timeout_s: float) -> Optional[Dict[str, Any]]:
    """Общий вызов службы: возвращает разобранный JSON или None (с диагностикой в журнале)."""
    cfg = get_service_config()
    request = urllib.request.Request(f"{cfg['url']}{path}", body, {"Content-Type": content_type})
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as e:
        logger.debug(f"[ocr-service] {path}: служба ответила ошибкой {e.code}")
    except urllib.error.URLError as e:
        logger.debug(f"[ocr-service] {path}: служба недоступна ({e.reason})")
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[ocr-service] {path}: вызов не удался: {type(e).__name__}: {str(e)[:100]}")
    return None


def lines_from_service(image: bytes, *, timeout_s: Optional[float] = None) -> List[Dict[str, Any]]:
    """Строки OCR страницы: формат совпадает с Occular (`text`, `quad`, `confidence`).

    Пустой список — служба недоступна или ничего не распознала: вызывающий код решает, что делать
    (у нас это откат на прежний движок и честная причина в отчёте).
    """
    cfg = get_service_config()
    if not cfg["enabled"]:
        return []
    data = _post(f"/ocr?lang={cfg['text_lang']}", image, "image/png", timeout_s or cfg["timeout_s"])
    if not data:
        return []
    out: List[Dict[str, Any]] = []
    for item in data.get("lines") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        quad = item.get("quad")
        if not text or quad is None:
            continue
        try:
            confidence = float(item.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        line: Dict[str, Any] = {"text": text, "quad": quad, "confidence": confidence}
        # Признак «координаты в системе присланного изображения» — чтобы потребитель не пересчитывал их
        # по масштабу сетки (служба распознаёт ровно то изображение, которое ей передали).
        if item.get("raw_scale") is not None:
            line["raw_scale"] = item["raw_scale"]
        out.append(line)
    return out


def cells_from_service(image: Any, quads: Sequence[Any], *, timeout_s: Optional[float] = None
                       ) -> List[Tuple[str, float]]:
    """Текст вырезанных ячеек: на вход изображение страницы и рамки, на выходе [(текст, уверенность)].

    Одним запросом на пачку: каждая вырезка — отдельное обращение к службе было бы в разы медленнее
    (сетевой круг на ячейку), а распознаватель всё равно однопоточный.
    """
    cfg = get_service_config()
    if not cfg["enabled"] or not len(quads):
        return []
    try:
        import cv2

        ok, buf = cv2.imencode(".png", image)
        if not ok:
            return []
        image_b64 = base64.b64encode(buf.tobytes()).decode()
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[ocr-service] изображение не закодировано: {e}")
        return []

    results: List[Tuple[str, float]] = []
    items = list(quads)
    for start in range(0, len(items), MAX_CELLS_PER_CALL):
        chunk = items[start:start + MAX_CELLS_PER_CALL]
        payload = {"image_b64": image_b64, "quads": [_quad_points(q) for q in chunk],
                   "lang": cfg["cells_lang"]}   # ячейки читает та модель, что лучше берёт числа (eslav)
        data = _post("/cells", json.dumps(payload).encode(), "application/json",
                     timeout_s or cfg["timeout_s"])
        if not data:
            # Не добиваем службу: на остаток пачки вернём пустые значения — разбор не теряется.
            results.extend([("", 0.0)] * len(chunk))
            continue
        for item in (data.get("texts") or []):
            if isinstance(item, dict):
                try:
                    results.append((str(item.get("text") or ""), float(item.get("confidence") or 0.0)))
                except (TypeError, ValueError):
                    results.append((str(item.get("text") or ""), 0.0))
            else:
                results.append((str(item or ""), 0.0))
    return results


def _quad_points(quad: Any) -> List[List[float]]:
    """Рамка в вид [[x, y] × 4] — в этом виде её ждёт служба."""
    try:
        import numpy as np

        arr = np.asarray(quad, dtype=float).reshape(-1, 2)
        return [[float(p[0]), float(p[1])] for p in arr[:4]]
    except Exception:  # noqa: BLE001
        return []


# ── Локальный движок: запасной путь, когда службы нет ───────────────────────────────────────────
# Зачем: служба живёт на другом сервере. Если она недоступна, а Occular из образа убран (веса ~700 МБ
# и torch ~0,9 ГБ), скан остался бы без распознавания вовсе. Поэтому в образ ставится тот же PP-OCRv5
# (RapidOCR), но локально: лицензионно чисто, работает без сети, медленнее службы, но страницу не теряет.
_local_engines: Dict[str, Any] = {}          # язык -> движок страницы
_local_cells_engines: Dict[str, Any] = {}    # язык -> движок вырезок
_local_failed = False
_local_lock = threading.Lock()

CELLS_TARGET_H = 40
CELLS_MAX_SCALE = 4.0


def local_available() -> bool:
    """Есть ли в контейнере локальный движок (проверка импорта, без сборки модели)."""
    try:
        import rapidocr  # noqa: F401

        return True
    except Exception:  # noqa: BLE001 — локального движка нет: работаем службой или прежним путём
        return False


def _build_local(cells: bool, lang: str):
    """Собрать локальный движок PP-OCRv5 на нужном языке.

    Для вырезок — без детектора: на вырезке одной строки детектор не находит текст вовсе (проверено
    03.10.2026). Язык берётся из настроек: текст — cyrillic, ячейки — eslav.
    """
    from rapidocr import RapidOCR
    from rapidocr.utils.typings import LangRec, ModelType, OCRVersion

    lang_enum = {"cyrillic": LangRec.CYRILLIC, "eslav": LangRec.ESLAV}[lang]
    params: Dict[str, Any] = {
        "Rec.ocr_version": OCRVersion.PPOCRV5,
        "Rec.lang_type": lang_enum,
        "Rec.model_type": ModelType.MOBILE,
    }
    if cells:
        params["Global.use_det"] = False
    return RapidOCR(params=params)


def _local_engine_for(cells: bool, lang: str):
    """Движок локального распознавания для языка: собирается один раз на процесс."""
    cache = _local_cells_engines if cells else _local_engines
    if lang not in cache:
        with _local_lock:
            if lang not in cache:
                cache[lang] = _build_local(cells=cells, lang=lang)
    return cache[lang]


def _as_array(image: bytes):
    import io

    import numpy as np
    from PIL import Image

    return np.array(Image.open(io.BytesIO(image)).convert("RGB"))


def lines_local(image: bytes) -> List[Dict[str, Any]]:
    """Строки страницы локальным PP-OCRv5. Пустой список — движка нет или распознать не удалось."""
    global _local_failed
    if _local_failed:
        return []
    try:
        engine = _local_engine_for(cells=False, lang=get_service_config()["text_lang"])
        array = _as_array(image)
        with _local_lock:
            out = engine(array)
    except Exception as e:  # noqa: BLE001 — без локального движка остаётся прежний путь
        _local_failed = True
        logger.debug(f"[ocr-service] локальное распознавание недоступно: {type(e).__name__}: {str(e)[:100]}")
        return []

    texts = [str(t) for t in (getattr(out, "txts", None) or [])]
    boxes = getattr(out, "boxes", None)
    scores = getattr(out, "scores", None)
    lines: List[Dict[str, Any]] = []
    for i, text in enumerate(texts):
        if not text.strip():
            continue
        line: Dict[str, Any] = {"text": text, "confidence": 0.0, "raw_scale": 1.0}
        try:
            if boxes is not None and len(boxes) > i:
                import numpy as np

                arr = np.asarray(boxes[i], dtype=float).reshape(-1, 2)
                line["quad"] = [[float(p[0]), float(p[1])] for p in arr]
        except Exception:  # noqa: BLE001 — рамка не критична для текста
            pass
        try:
            if scores is not None and len(scores) > i:
                line["confidence"] = float(scores[i])
        except Exception:  # noqa: BLE001
            pass
        lines.append(line)
    return lines


def cells_local(image: Any, quads: Sequence[Any]) -> List[Tuple[str, float]]:
    """Текст вырезанных ячеек локальным распознавателем (без детектора, с увеличением мелких вырезок)."""
    global _local_failed
    if _local_failed or not len(quads):
        return []
    try:
        import cv2
        import numpy as np

        engine = _local_engine_for(cells=True, lang=get_service_config()["cells_lang"])
        height, width = image.shape[:2]
        out: List[Tuple[str, float]] = []
        with _local_lock:
            for quad in quads:
                try:
                    points = np.asarray(quad, dtype=float).reshape(-1, 2)
                    x0 = max(0, int(points[:, 0].min()))
                    x1 = min(width, int(np.ceil(points[:, 0].max())))
                    y0 = max(0, int(points[:, 1].min()))
                    y1 = min(height, int(np.ceil(points[:, 1].max())))
                    crop = image[y0:y1, x0:x1]
                    if crop.size == 0 or crop.shape[0] < 3 or crop.shape[1] < 3:
                        out.append(("", 0.0))
                        continue
                    if crop.shape[0] < CELLS_TARGET_H:
                        factor = min(CELLS_MAX_SCALE, CELLS_TARGET_H / max(1, crop.shape[0]))
                        crop = cv2.resize(crop, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC)
                    result = engine(crop)
                    text = result.txts[0] if getattr(result, "txts", None) else ""
                    score = 0.0
                    scores = getattr(result, "scores", None)
                    if scores is not None and len(scores):
                        score = float(scores[0])
                    out.append((str(text or ""), score))
                except Exception:  # noqa: BLE001 — одна плохая вырезка не роняет пачку
                    out.append(("", 0.0))
        return out
    except Exception as e:  # noqa: BLE001
        _local_failed = True
        logger.debug(f"[ocr-service] локальные вырезки недоступны: {type(e).__name__}: {str(e)[:100]}")
        return []


def ocr_service_status(check_service: bool = False) -> Dict[str, Any]:
    """Статус для админки: настройки + (по запросу) доступность службы и её движок."""
    cfg = get_service_config()
    status: Dict[str, Any] = {
        "enabled": cfg["enabled"],
        "url": cfg["url"],
        "timeout_s": cfg["timeout_s"],
        "text_lang": cfg["text_lang"],
        "cells_lang": cfg["cells_lang"],
        "languages": list(LANGUAGES),
        "reachable": None,
        "engine": None,
        "detail": "",
    }
    if not check_service:
        status["detail"] = "служба выключена — распознаёт локальный движок в контейнере" if not cfg["enabled"] \
            else "служба включена"
        return status
    try:
        with urllib.request.urlopen(f"{cfg['url']}/health", timeout=PROBE_TIMEOUT_S) as response:
            data = json.loads(response.read())
        status["reachable"] = str(data.get("status") or "") == "ok"
        status["engine"] = data.get("engine")
        status["lang"] = data.get("lang")
        status["service_loaded_page"] = data.get("loaded_page")
        status["service_loaded_cells"] = data.get("loaded_cells")
        status["warm"] = bool(data.get("ready"))
        status["detail"] = f"служба доступна ({status['engine']}; языки: {', '.join(data.get('languages') or [])})" \
            if status["reachable"] else "служба ответила, но статус не ok"
    except Exception as e:  # noqa: BLE001
        status["reachable"] = False
        status["detail"] = f"служба недоступна: {type(e).__name__}: {str(e)[:120]}"
    return status
