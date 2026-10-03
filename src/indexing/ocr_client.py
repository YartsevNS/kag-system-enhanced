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

_available: Optional[bool] = None
_checked_at: float = 0.0


def get_service_config() -> Dict[str, Any]:
    """Настройки OCR-службы. Ошибка чтения настроек = служба выключена (поведение как раньше)."""
    cfg: Dict[str, Any] = {"enabled": False, "url": DEFAULT_URL, "timeout_s": DEFAULT_TIMEOUT_S}
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
    data = _post("/ocr", image, "image/png", timeout_s or cfg["timeout_s"])
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
        payload = {"image_b64": image_b64, "quads": [_quad_points(q) for q in chunk]}
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


def ocr_service_status(check_service: bool = False) -> Dict[str, Any]:
    """Статус для админки: настройки + (по запросу) доступность службы и её движок."""
    cfg = get_service_config()
    status: Dict[str, Any] = {
        "enabled": cfg["enabled"],
        "url": cfg["url"],
        "timeout_s": cfg["timeout_s"],
        "reachable": None,
        "engine": None,
        "detail": "",
    }
    if not check_service:
        status["detail"] = "служба выключена — распознаёт прежний движок (Occular)" if not cfg["enabled"] \
            else "служба включена"
        return status
    try:
        with urllib.request.urlopen(f"{cfg['url']}/health", timeout=PROBE_TIMEOUT_S) as response:
            data = json.loads(response.read())
        status["reachable"] = str(data.get("status") or "") == "ok"
        status["engine"] = data.get("engine")
        status["lang"] = data.get("lang")
        status["warm"] = bool(data.get("ready"))
        status["detail"] = f"служба доступна ({status['engine']})" if status["reachable"] \
            else "служба ответила, но статус не ok"
    except Exception as e:  # noqa: BLE001
        status["reachable"] = False
        status["detail"] = f"служба недоступна: {type(e).__name__}: {str(e)[:120]}"
    return status
