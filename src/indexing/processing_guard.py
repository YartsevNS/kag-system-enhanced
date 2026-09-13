"""Административная блокировка обработки документов.

Настройка `system/processing = {blocked, message}` ставится в админке и
закрывает запуск обработки: API отдаёт 423 на «Обработать». Но документы,
поставленные в очередь ДО паузы, продолжали обрабатываться — галочка
действовала только на кнопку интерфейса.

Проверка вынесена отдельным модулем (как queue_guard), потому что нужна в двух
независимых местах: в самой задаче worker'а и в recovery, который переставляет
задачи для зависших документов.
"""

from __future__ import annotations

from loguru import logger

# Отсрочка повторной попытки, когда обработка остановлена администратором:
# задача не теряется (retry с countdown), но и не молотит впустую — одна
# проверка раз в две минуты на документ.
PROCESSING_BLOCK_RETRY_S = 120

DEFAULT_MESSAGE = "обработка временно недоступна (техническое обслуживание)"


def _load_processing_cfg():
    """Прочитать настройку system/processing из config_store.

    Отдельная функция — чтобы её можно было подменить в тестах: config_store
    тянет за собой БД, а проверка должна тестироваться без неё.
    """
    from src.api.services.config_store import config_store
    return config_store.get("system", "processing")


def processing_blocked() -> tuple[bool, str]:
    """Остановлена ли обработка администратором (system/processing).

    Возвращает (blocked, message).

    При сбое чтения настроек — (False, ""): недоступная БД не должна
    останавливать очередь (fail-open), факт сбоя виден в логе. Обратное
    решение (fail-closed) опаснее: одна ошибка чтения заморозила бы
    обработку всех документов без ведома администратора.
    """
    try:
        cfg = _load_processing_cfg() or {}
        if isinstance(cfg, dict) and cfg.get("blocked"):
            msg = str(cfg.get("message", "") or "").strip()
            return True, msg or DEFAULT_MESSAGE
    except Exception as e:  # pragma: no cover - зависит от внешней БД
        logger.warning(f"[processing_guard] настройку system/processing прочитать не удалось: {e}")
    return False, ""

def _load_graph_cfg():
    """Прочитать настройку system/graph из config_store.

    Отдельная функция — чтобы подменять её в тестах (config_store тянет БД).
    """
    from src.api.services.config_store import config_store
    return config_store.get("system", "graph")


def graph_skip_requested() -> tuple[bool, str]:
    """Нужно ли ПРОПУСТИТЬ построение графа при обработке (system/graph.skip).

    Граф — самая дорогая часть обработки: на замере 2026-09-13 документ в 13 чанков
    обрабатывался 147 с, из них 135.7 с ушло на граф (LLM-извлечение по чанкам,
    1585 векторов сущностей, дедупликация пар). Разбор и эмбеддинги документа — <12 с.
    Поэтому граф можно не строить сразу, а построить позже отдельным действием
    («Перестроить граф» на /kg или кнопкой у документа на странице «Документы»).

    Возвращает (skip, message). При сбое чтения — (False, ""): недоступная БД не
    должна менять поведение обработки (fail-open, граф строится как обычно).
    """
    try:
        cfg = _load_graph_cfg() or {}
        if isinstance(cfg, dict) and cfg.get("skip"):
            return True, str(cfg.get("message", "") or "").strip()
    except Exception as e:  # pragma: no cover - зависит от внешней БД
        logger.warning(f"[processing_guard] настройку system/graph прочитать не удалось: {e}")
    return False, ""
