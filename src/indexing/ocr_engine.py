"""Движок инференса распознавания: OpenVINO Execution Provider вместо ванильного CPU.

Зачем: замер 04.10.2026 на нашем железе (Intel Xeon E5-2682 v4, Broadwell — AVX2 без AVX-512/VNNI)
показал на ПОЛНОМ пайплайне страницы: накладная 4,6 → 2,0 с (2,35x), смета 2,4 → 1,2 с (1,94x),
при этом число строк и чисел совпало, подстановок цифр — ноль. Это FP32: квантование не применяется,
риск «поехавших» цифр не затронут (в отличие от INT8, который у нас отклонён замером).

Как работает: подменяется список провайдеров в самом rapidocr
(`rapidocr.inference_engine.onnxruntime.provider_config.ProviderConfig.get_ep_list`) — препроцессинг,
алфавит и CTC-декодер остаются родные, меняется только движок инференса. Так сравнение честное.

Правила:
* включается настройкой `ocr/settings.engine` = `openvino` (или `auto`);
* если пакета `onnxruntime-openvino` нет или провайдер недоступен — тихо остаёмся на CPU (fail-open),
  распознавание работает как раньше;
* подмена делается один раз на процесс, потокобезопасно.

ВНИМАНИЕ (профиль по умолчанию): пока настройка не выставлена, поведение прежнее — CPU.
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

_patched = False
_lock = threading.Lock()
_OV_PROVIDER = "OpenVINOExecutionProvider"


def openvino_available() -> bool:
    """Есть ли провайдер OpenVINO в текущем onnxruntime."""
    try:
        import onnxruntime as ort

        return _OV_PROVIDER in ort.get_available_providers()
    except Exception:  # noqa: BLE001 — нет onnxruntime/пакета: считаем, что нет
        return False


def engine_setting() -> str:
    """Значение настройки `ocr/settings.engine` (по умолчанию `cpu`)."""
    try:
        from src.api.services.config_store import config_store

        cfg = config_store.get("ocr", "settings") or {}
        return str(cfg.get("engine") or "cpu").strip().lower()
    except Exception:  # noqa: BLE001 — настройки недоступны: ведём себя как раньше
        return "cpu"


def is_active() -> bool:
    """Включён ли OpenVINO фактически (подмена сделана)."""
    return _patched


def enable_if_configured() -> bool:
    """Включить OpenVINO, если он есть и разрешён настройкой. True — движок активен.

    Вызывать перед созданием движков rapidocr (страница и ячейки). Повторный вызов безвреден.
    """
    global _patched
    with _lock:
        if _patched:
            return True
        setting = engine_setting()
        if setting not in ("openvino", "ov", "auto"):
            return False
        if not openvino_available():
            logger.info("[ocr] настройка просит OpenVINO, но провайдера нет — работаем на CPU")
            return False
        try:
            from rapidocr.inference_engine.onnxruntime import provider_config as pc

            original = pc.ProviderConfig.get_ep_list

            def patched(self):  # type: ignore[no-untyped-def]
                return [(_OV_PROVIDER, {"device_type": "CPU"})] + list(original(self))

            pc.ProviderConfig.get_ep_list = patched  # type: ignore[method-assign]
            _patched = True
            logger.info("[ocr] движок инференса: OpenVINO (CPU) — провайдер rapidocr подменён")
            return True
        except Exception as e:  # noqa: BLE001 — подмена не удалась: работаем как раньше
            logger.warning(f"[ocr] OpenVINO включить не удалось ({type(e).__name__}: {e}) — остаёмся на CPU")
            return False


def status() -> dict:
    """Краткий статус для админки/диагностики."""
    return {
        "setting": engine_setting(),
        "provider_available": openvino_available(),
        "active": _patched,
    }
