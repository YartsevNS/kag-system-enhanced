"""Распознавание таблиц моделью зрения (VL) через API — подключаемая опция админки.

Зачем отдельный модуль: для таблиц, нарисованных БЕЗ линий сетки (скриншоты из Excel, где рамки —
цветные заливки ячеек), наш Occular бессилен по устройству: детектор таблиц ищет линии. Замер
27.09.2026 на файле владельца: полный Occular (со всеми моделями) — 0 таблиц за 0,3 с, обычный OCR
даёт 1302 символа и теряет числовой столбец. Модель зрения читает такую страницу и возвращает таблицу
с числами (4, 28, 2, 43, 4, 12, 8, 1, 1, 4 — совпадает с изображением).

Решение владельца (27.09.2026): это ПОДКЛЮЧАЕМАЯ опция. Выключена — страница просто пропускается,
никаких сетевых вызовов. Включена — идём по API к сервису моделей (по умолчанию Ollama на 41).

Настройки читаются из табличного слоя (namespace tables, ключ config): `vlm_tables_*`.
Рабочие параметры вызова подобраны замером, а не догадкой:
  * `num_predict` ограничен (2048): при 4096 модель зацикливалась и повторяла строки (91 строка вместо 14);
  * штраф за повторы НЕ задаём: при 1,3 текст портился («в защитной одежде с колимират»), при 1,15
    сдвигались колонки и путались числа;
  * temperature 0 и top_k 1 — детерминированность;
  * стоп-последовательности обрывают ответ сразу после таблицы.
"""
from __future__ import annotations

import base64
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

from src.indexing.table_recovery import RecoveredTable, SOURCE_VLM, table_from_markdown

logger = logging.getLogger(__name__)

PROMPT = (
    "Преобразуй изображение в markdown-таблицу. Правила: только таблица, без вступлений и пояснений; "
    "одна строка таблицы на одну строку изображения; повторять строки запрещено; если данные кончились — "
    "заверши ответ. Числа сохраняй точно, ничего не додумывай."
)
STOP_SEQUENCES = ["\n\n", "```"]
DEFAULT_TIMEOUT_MS = 300_000


def _as_int(value: Any, low: int, high: int, fallback: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return fallback


def get_vlm_tables_config() -> Dict[str, Any]:
    """Настройки VL-распознавания таблиц (из общего конфига табличного слоя).

    Ошибка чтения настроек не должна ломать обработку: возвращаем выключенную опцию.
    """
    cfg: Dict[str, Any] = {
        "enabled": False,
        "endpoint": "http://192.168.50.41:11434",
        "model": "kag-qwen2vl:2b",
        "timeout_ms": DEFAULT_TIMEOUT_MS,
        "num_predict": 2048,
        "min_rows": 2,
        "api": "ollama",
    }
    try:
        from src.indexing.tables_settings import get_tables_config
        raw = get_tables_config()
        cfg["enabled"] = bool(raw.get("vlm_tables_enabled", False))
        endpoint = str(raw.get("vlm_tables_endpoint") or "").strip().rstrip("/")
        if endpoint:
            cfg["endpoint"] = endpoint
        model = str(raw.get("vlm_tables_model") or "").strip()
        if model:
            cfg["model"] = model
        cfg["timeout_ms"] = _as_int(raw.get("vlm_tables_timeout_ms"), 1_000, 1_800_000, DEFAULT_TIMEOUT_MS)
        cfg["num_predict"] = _as_int(raw.get("vlm_tables_num_predict"), 64, 8192, 2048)
        cfg["min_rows"] = _as_int(raw.get("vlm_tables_min_rows"), 2, 50, 2)
        api = str(raw.get("vlm_tables_api") or "").strip().lower()
        if api in ("ollama", "openai"):
            cfg["api"] = api
    except Exception as e:  # noqa: BLE001 — настройки недоступны, работаем выключенными
        logger.debug(f"[vlm-tables] настройки недоступны, опция выключена: {e}")
        cfg["enabled"] = False
    return cfg


def _drop_repeats(rows: list) -> Tuple[list, int]:
    """Убрать повторы строк: и подряд, и полностью совпадающие с уже встречавшимися.

    Нужно потому, что модель склонна зацикливаться: в замере она выдала 91 строку вместо 14, повторяя
    одни и те же. Шапку (первую строку) не считаем повтором даже если она встретилась ещё раз.
    """
    out: list = []
    seen: set = set()
    dropped = 0
    for i, row in enumerate(rows):
        key = tuple(row)
        if i > 0 and key in seen:
            dropped += 1
            continue
        if i > 0:
            seen.add(key)
        out.append(row)
    return out, dropped


def recognize_table(image: bytes, *, page: int = 0, config: Optional[Dict[str, Any]] = None
                    ) -> Tuple[Optional[RecoveredTable], str]:
    """Распознать таблицу моделью зрения.

    Возвращает (таблица, причина). Причина заполнена всегда — её видно в журнале и в админке, поэтому
    «пропущено, потому что выключено» никогда не выглядит как «не получилось распознать».
    Модуль никогда не бросает исключение: страница не должна теряться из-за недоступного сервиса.
    """
    cfg = config or get_vlm_tables_config()
    if not cfg["enabled"]:
        return None, "модель зрения выключена в настройках табличного слоя — страница пропущена"

    started = time.time()
    payload = {
        "model": cfg["model"],
        "prompt": PROMPT,
        "images": [base64.b64encode(image).decode()],
        "stream": False,
        "options": {
            "temperature": 0,
            "top_k": 1,
            "num_predict": cfg["num_predict"],
        },
        "stop": STOP_SEQUENCES,
    }
    if cfg.get("api") == "openai":
        # OpenAI-совместимый протокол (llama.cpp server, vLLM, внешние VL-API): картинка — в data-URL.
        payload = {
            "model": cfg["model"],
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {
                    "url": "data:image/png;base64," + base64.b64encode(image).decode()}},
            ]}],
            "temperature": 0,
            "max_tokens": cfg["num_predict"],
            "stream": False,
        }
        url = f"{cfg['endpoint']}/v1/chat/completions"
    else:
        url = f"{cfg['endpoint']}/api/generate"
    request = urllib.request.Request(url, json.dumps(payload).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=cfg["timeout_ms"] / 1000.0) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as e:
        return None, f"сервис моделей ответил ошибкой {e.code}"
    except urllib.error.URLError as e:
        return None, f"сервис моделей недоступен ({e.reason})"
    except Exception as e:  # noqa: BLE001
        return None, f"вызов сервиса моделей не удался: {type(e).__name__}: {str(e)[:120]}"

    if data.get("error"):
        return None, f"сервис моделей вернул ошибку: {str(data['error'])[:120]}"

    if cfg.get("api") == "openai":
        try:
            markdown = data["choices"][0]["message"]["content"] or ""
        except Exception:  # noqa: BLE001 — неожиданная форма ответа
            return None, "сервис вернул ответ неожиданного вида (нет choices[0].message.content)"
    else:
        markdown = data.get("response") or ""
    table = table_from_markdown(markdown, source_model=cfg["model"], page=page)
    if table is None:
        return None, "модель не вернула таблицу (в ответе нет markdown-таблицы)"

    rows, dropped = _drop_repeats(table.rows)
    table.rows = rows
    if dropped:
        table.notes.append(f"выброшено повторяющихся строк: {dropped}")
    table.seconds = round(time.time() - started, 1)
    if table.n_rows < cfg["min_rows"]:
        return None, f"таблица слишком короткая ({table.n_rows} строк(и)) — не считаем её таблицей"
    return table, f"распознано моделью {cfg['model']} за {table.seconds} с (строк {table.n_rows})"


def apply_settings_update(existing: Any, changes: Dict[str, Any]) -> Dict[str, Any]:
    """Слить изменения VL-опции с уже сохранённым конфигом табличного слоя.

    Важно: настройки таблиц лежат одним словарём (namespace tables, ключ config), и в нём же живут
    `enabled`, `row_vectors`, `sql_*` и прочее. Поэтому обновляем ТОЛЬКО свои ключи, остальные переносим
    как есть — иначе сохранение этой опции выключило бы весь табличный стек.
    """
    cfg = dict(existing) if isinstance(existing, dict) else {}
    for key in ("enabled", "endpoint", "model", "timeout_ms", "num_predict", "min_rows", "api"):
        value = changes.get(key)
        if value is None:
            continue
        if key == "enabled":
            cfg["vlm_tables_enabled"] = bool(value)
        elif key in ("endpoint", "model", "api"):
            cfg[f"vlm_tables_{key}"] = str(value).strip()
        else:
            try:
                cfg[f"vlm_tables_{key}"] = int(value)
            except (TypeError, ValueError):
                pass
    return cfg


def vlm_tables_status(check_service: bool = False) -> Dict[str, Any]:
    """Статус для админки: настройки + (по запросу) доступность сервиса и наличие модели."""
    cfg = get_vlm_tables_config()
    status: Dict[str, Any] = {
        "enabled": cfg["enabled"],
        "endpoint": cfg["endpoint"],
        "model": cfg["model"],
        "timeout_ms": cfg["timeout_ms"],
        "num_predict": cfg["num_predict"],
        "min_rows": cfg["min_rows"],
        "api": cfg.get("api", "ollama"),
        "reachable": None,
        "model_present": None,
        "detail": "",
    }
    if not check_service:
        status["detail"] = "опция выключена — страницы без линий пропускаются" if not cfg["enabled"] \
            else "опция включена"
        return status

    try:
        probe = f"{cfg['endpoint']}/v1/models" if cfg.get("api") == "openai" else f"{cfg['endpoint']}/api/tags"
        with urllib.request.urlopen(probe, timeout=15) as response:
            data = json.loads(response.read())
        names = [str(m.get("name") or m.get("id") or "") for m in (data.get("models") or data.get("data") or [])]
        status["reachable"] = True
        status["model_present"] = any(n == cfg["model"] or n.startswith(cfg["model"].split(":")[0]) for n in names)
        status["models"] = names[:20]
        status["detail"] = "сервис доступен, модель найдена" if status["model_present"] \
            else f"сервис доступен, но модели {cfg['model']} в списке нет"
    except Exception as e:  # noqa: BLE001
        status["reachable"] = False
        status["detail"] = f"сервис недоступен: {type(e).__name__}: {str(e)[:120]}"
    return status
