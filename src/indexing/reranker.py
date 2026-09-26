"""Реранкер результатов поиска: настройка из админки, выключатель, честный статус.

Что было не так (найдено 2026-09-13 по логам стенда):
* модель была захардкожена как «BAAI/bge-reranker-v2-m3», которой в flashrank НЕТ: запрос уходил
  на `huggingface.co/prithivida/flashrank/resolve/main/BAAI/bge-reranker-v2-m3.zip` → 404 →
  ранкер не загружался НИКОГДА;
* вместо явного «выключено» молча включался BM25-fallback, который на пустом словаре падал
  с `division by zero` и возвращал исходный порядок — то есть реранкинг отсутствовал, а в логах
  это выглядело как «Reranker error … fallback to original order» на каждом запросе;
* кэш модели лежал в `/app/models/.flashrank` — это overlay-слой образа, файлы теряются при
  пересоздании контейнера.

Как теперь:
* настройки — в config_store (`reranker/config`): `enabled`, `model`, `cache_dir`, `top_k`;
  выключено = реранкинг не выполняется вообще (никакого скрытого fallback);
* по умолчанию включена МУЛЬТИЯЗЫЧНАЯ модель `ms-marco-MultiBERT-L-12` (единственная из списка
  flashrank, поддерживающая русский); полный список — `SUPPORTED_MODELS`;
* кэш модели — `/app/data/models/flashrank` (bind-каталог ./data, переживает пересоздание);
* ошибка загрузки запоминается и пишется в лог ОДИН раз, а не на каждый запрос; статус
  (enabled/loaded/error) доступен админке для отображения.
"""

import os
from typing import Any, Dict, List, Optional

from loguru import logger

# Модели flashrank 0.2.10 (список из flashrank/Config.py). Мультиязычная — только MultiBERT.
SUPPORTED_MODELS: List[str] = [
    "ms-marco-MultiBERT-L-12",      # мультиязычная, русский поддержан — по умолчанию
    "ms-marco-MiniLM-L-12-v2",
    "ms-marco-TinyBERT-L-2-v2",     # самая быстрая, но англоязычная
    "ce-esci-MiniLM-L12-v2",
    "rank-T5-flan",
]
DEFAULT_MODEL = "ms-marco-MultiBERT-L-12"
# Кэш в bind-каталоге ./data: /app/models теряется при пересоздании контейнера (overlay).
DEFAULT_CACHE_DIR = "/app/data/models/flashrank"
DEFAULT_TOP_K = 10
# Бэкенд «сервис»: cross-encoder живёт на сервере моделей (41), api ходит к нему по HTTP.
# Значения по умолчанию — из docs/guides/reranker-service.md (замер 26.09.2026: DiTy 1,46 с на
# 20 ядрах, у нас на 4 ядрах стенда будет медленнее, поэтому таймаут строгий).
DEFAULT_SERVICE_ENDPOINT = "http://192.168.50.41:8010"
DEFAULT_SERVICE_TIMEOUT_MS = 1500
DEFAULT_MIN_FRAGMENTS = 4
# «Защёлка»: после стольких подряд неудач сервиса не дёргаем его BREAKER_COOLDOWN_S секунд,
# иначе каждый вопрос будет ждать таймаут (чат замедлится, а пользы ноль).
BREAKER_FAILS = 3
BREAKER_COOLDOWN_S = 300

_service_fails = 0
_service_blocked_until = 0.0

_FLASH_AVAILABLE = False
_BM25_AVAILABLE = False
_ranker_instance = None
_bm25_instance = None
_load_error: str = ""
_loaded_for: tuple = ()          # (model, cache_dir) последней удачной/неудачной попытки
_logged_error_for: tuple = ()    # чтобы не повторять одну и ту же ошибку в логе


def get_reranker_config() -> Dict[str, Any]:
    """Настройки реранкера из config_store (namespace reranker, ключ config).

    Три состояния (решение владельца 26.09.2026 — реранкер подключаемый и отключаемый в админке):
      backend = "none"      — не ранжируем (значение по умолчанию);
      backend = "flashrank" — локальный cross-encoder внутри api (путь ONNX в cache_dir);
      backend = "service"   — HTTP-вызов cross-encoder'а на сервере моделей (endpoint).
    Совместимость: если в сохранённой конфигурации backend нет, а enabled=true — это flashrank
    (так работала прежняя версия, ломать её настройки нельзя).
    """
    cfg: Dict[str, Any] = {
        "enabled": False,
        "backend": "none",
        "model": DEFAULT_MODEL,
        "cache_dir": DEFAULT_CACHE_DIR,
        "top_k": DEFAULT_TOP_K,
        "endpoint": DEFAULT_SERVICE_ENDPOINT,
        "timeout_ms": DEFAULT_SERVICE_TIMEOUT_MS,
        "min_fragments": DEFAULT_MIN_FRAGMENTS,
        "keep_dense_on_error": True,
    }
    try:
        from src.api.services.config_store import config_store
        stored = config_store.get("reranker", "config") or {}
        if isinstance(stored, dict):
            if "enabled" in stored:
                cfg["enabled"] = bool(stored["enabled"])
            if stored.get("backend"):
                backend = str(stored["backend"]).strip().lower()
                cfg["backend"] = backend if backend in ("none", "flashrank", "service") else "none"
            elif cfg["enabled"]:
                cfg["backend"] = "flashrank"          # прежнее поведение
            if not cfg["enabled"]:
                cfg["backend"] = "none"               # тумблер выключен — никакого ранжирования
            if stored.get("model"):
                cfg["model"] = str(stored["model"]).strip()
            if stored.get("cache_dir"):
                cfg["cache_dir"] = str(stored["cache_dir"]).strip()
            if stored.get("endpoint"):
                cfg["endpoint"] = str(stored["endpoint"]).strip()
            for key, low, high in (("top_k", 1, 50), ("timeout_ms", 50, 30000),
                                   ("min_fragments", 1, 50)):
                if stored.get(key) not in (None, ""):
                    try:
                        cfg[key] = max(low, min(high, int(stored[key])))
                    except (TypeError, ValueError):
                        pass
            if "keep_dense_on_error" in stored:
                cfg["keep_dense_on_error"] = bool(stored["keep_dense_on_error"])
    except Exception as e:
        logger.debug(f"[reranker] настройки недоступны, значения по умолчанию: {e}")
    return cfg


def reranker_status() -> Dict[str, Any]:
    """Статус для админки: настройки + загружена ли модель + последняя ошибка."""
    cfg = get_reranker_config()
    cache_dir = cfg["cache_dir"]
    files: List[str] = []
    try:
        if os.path.isdir(cache_dir):
            files = sorted(os.listdir(cache_dir))[:10]
    except Exception:
        pass
    return {
        **cfg,
        "available_models": SUPPORTED_MODELS,
        "default_model": DEFAULT_MODEL,
        "loaded": _ranker_instance is not None,
        "load_error": _load_error,
        "cache_files": files,
        "cache_dir_exists": os.path.isdir(cache_dir),
    }


def warm_reranker(force: bool = False) -> Dict[str, Any]:
    """Загрузить модель реранкера (при первом вызове скачает ONNX в cache_dir).

    Нужно админке: скачать модель заранее и увидеть ошибку до включения, а не в бою.
    Работает независимо от `enabled`.
    """
    global _ranker_instance, _FLASH_AVAILABLE, _load_error, _loaded_for, _logged_error_for
    cfg = get_reranker_config()
    if _ranker_instance is not None and not force:
        return {"loaded": True, "model": cfg["model"], "error": ""}
    _loaded_for = ()
    try:
        from flashrank import Ranker as FlashRanker
        target_dir = cfg["cache_dir"]
        os.makedirs(target_dir, exist_ok=True)
        _ranker_instance = FlashRanker(model_name=cfg["model"], cache_dir=target_dir)
        _FLASH_AVAILABLE = True
        _load_error = ""
        _loaded_for = (cfg["model"], target_dir)
        logger.info(f"[reranker] модель загружена: {cfg['model']} (кэш {target_dir})")
        return {"loaded": True, "model": cfg["model"], "error": ""}
    except Exception as e:
        _ranker_instance = None
        _FLASH_AVAILABLE = False
        _load_error = f"{type(e).__name__}: {e}"
        logger.warning(
            f"[reranker] модель '{cfg['model']}' не загрузилась: {_load_error}. "
            f"Доступные модели: {', '.join(SUPPORTED_MODELS)}"
        )
        return {"loaded": False, "model": cfg["model"], "error": _load_error}


def get_default_reranker() -> Optional[Any]:
    """Экземпляр реранкера или None (выключено / не загрузилось).

    Приоритет: FlashRank (если включено и загрузилось) → BM25-фолбэк (только если включено)
    → None. Выключенный реранкер не подменяется скрытым BM25: «выключено» значит выключено.
    """
    global _bm25_instance, _BM25_AVAILABLE, _log_error

    cfg = get_reranker_config()
    if not cfg["enabled"]:
        return None

    # Модель/каталог переключили в админке — сбрасываем загруженный экземпляр
    if _ranker_instance is not None and _loaded_for != (cfg["model"], cfg["cache_dir"]):
        logger.info("[reranker] настройки изменились — перезагружаю модель")
        _reload()

    if _ranker_instance is None:
        warm_reranker()

    if _ranker_instance is not None:
        return _ranker_instance

    # BM25 — только как осознанный фолбэк, когда модель недоступна
    if _bm25_instance is None and not _BM25_AVAILABLE:
        try:
            from rank_bm25 import BM25Okapi  # noqa: F401
            _BM25_AVAILABLE = True
        except Exception as e:
            logger.warning(f"[reranker] BM25 недоступен: {e}")
            _BM25_AVAILABLE = False
    if _BM25_AVAILABLE:
        if _bm25_instance is None:
            _bm25_instance = _BM25_Reranker()
        return _bm25_instance
    return None


def _reload() -> None:
    """Сбросить загруженный экземпляр (после смены настроек)."""
    global _ranker_instance, _FLASH_AVAILABLE, _loaded_for
    _ranker_instance = None
    _FLASH_AVAILABLE = False
    _loaded_for = ()


class _BM25_Reranker:
    """BM25-фолбэк: без нейросетей, работает на любых языках, дёшево."""

    def rerank(self, query: str, passages: List[Dict[str, Any]], top_k: int = 5) -> List[Dict[str, Any]]:
        from rank_bm25 import BM25Okapi

        if not passages:
            return []
        if not (query or "").strip():
            return passages[:top_k]

        tokenized_corpus = [(p.get("text") or p.get("content") or "").split() for p in passages]
        try:
            bm25 = BM25Okapi(tokenized_corpus)
            scores = bm25.get_scores(query.split())
        except ZeroDivisionError:
            # Пустой словарь (нет общих токенов) — библиотека делит на ноль. Это не ошибка
            # системы, а случай «сравнивать нечего»: отдаём исходный порядок без шума в логе.
            logger.debug("[reranker] BM25: пустой словарь токенов — исходный порядок")
            return passages[:top_k]
        except Exception as e:
            logger.warning(f"[reranker] BM25 не сработал: {type(e).__name__}: {e}")
            return passages[:top_k]

        indexed = list(enumerate(passages))
        indexed.sort(key=lambda x: scores[x[0]], reverse=True)
        results = []
        for idx, passage in indexed[:top_k]:
            passage["rerank_score"] = float(scores[idx])
            passage["original_index"] = idx
            results.append(passage)
        return results


def _record_rerank_change(before: List[Dict[str, Any]], after: List[Dict[str, Any]]) -> None:
    """Метрика: поменял ли реранкер лучший фрагмент.

    Зачем: если реранкер никогда не меняет топ-1, он тратит время и ничего не даёт.
    Счётчики rag_reranker_runs_total / rag_reranker_changed_top1_total отвечают на
    это в Grafana (доля изменений).
    """
    try:
        if not before or not after:
            return

        def _key(item: Dict[str, Any]):
            return item.get("id") or item.get("chunk_id") or item.get("document_id")

        from src.monitoring.prometheus import record_rerank
        record_rerank(_key(before[0]) != _key(after[0]))
    except Exception:
        pass


def _rerank_via_service(query: str, results: List[Dict[str, Any]],
                        cfg: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """Ранжирование через внешний сервис (сервер моделей).

    Возвращает переупорядоченный список или None, если сервис не ответил/ответил неполно —
    решение, что делать дальше, принимает вызывающий (правило keep_dense_on_error: оставить
    векторный порядок, но НИКОГДА не ломать ответ пользователю).
    """
    global _service_fails, _service_blocked_until
    import json
    import time as _time
    import urllib.request

    if _time.time() < _service_blocked_until:
        return None                      # «защёлка» после серии неудач — не тратим таймаут

    payload = {"query": query,
               "candidates": [{"id": f"i{i}",
                               "text": (r.get("text") or r.get("content") or "")[:2000]}
                              for i, r in enumerate(results)]}
    url = cfg["endpoint"].rstrip("/") + "/rerank"
    timeout = max(0.05, cfg["timeout_ms"] / 1000.0)
    try:
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:                      # таймаут, отказ соединения, 5xx, мусор в ответе
        _note_service_failure(type(e).__name__, str(e)[:80])
        return None

    order: List[Dict[str, Any]] = []
    seen = set()
    for item in data.get("scores") or []:
        raw = str(item.get("id") or "")
        if not raw.startswith("i"):
            continue
        try:
            pos = int(raw[1:])
        except ValueError:
            continue
        if 0 <= pos < len(results) and pos not in seen:
            seen.add(pos)
            order.append(results[pos])
    if len(order) != len(results):
        _note_service_failure("неполный_ответ", f"{len(order)} из {len(results)}")
        return None

    _service_fails = 0
    return order


def _note_service_failure(reason: str, detail: Any = "") -> None:
    """Учесть неудачу сервиса: метрика + «защёлка» после BREAKER_FAILS неудач подряд."""
    global _service_fails, _service_blocked_until
    import time as _time

    _service_fails += 1
    if _service_fails >= BREAKER_FAILS:
        _service_blocked_until = _time.time() + BREAKER_COOLDOWN_S
        logger.warning(f"[reranker] сервис не ответил {_service_fails} раз подряд — пауза "
                       f"{BREAKER_COOLDOWN_S} с ({reason}: {detail})")
    else:
        logger.warning(f"[reranker] сервис не ответил ({reason}: {detail}) — векторный порядок")
    try:
        from src.monitoring.prometheus import record_reranker_error
        record_reranker_error(reason)
    except Exception:
        pass


async def rerank_search_results(
    query: str,
    results: List[Dict[str, Any]],
    top_k: int = 5,
) -> List[Dict[str, Any]]:
    """Переранжировать результаты поиска (или вернуть как есть, если реранкер выключен)."""
    if not results:
        return []

    cfg = get_reranker_config()
    if not cfg["enabled"]:
        return results[:top_k]

    # Бэкенд «сервис»: ранжирование на сервере моделей. Короткий список не ранжируем —
    # нечего менять местами, а вызов стоит времени.
    if cfg["backend"] == "service":
        if len(results) < cfg["min_fragments"]:
            return results[:top_k]
        import asyncio
        ordered = await asyncio.to_thread(_rerank_via_service, query, results, cfg)
        if not ordered:
            return results[:top_k]               # keep_dense_on_error
        _record_rerank_change(results, ordered)
        return ordered[:top_k]

    # Реранкер — УЛУЧШЕНИЕ, а не критический путь: любая его ошибка (загрузка модели,
    # инференс, приведение типов) не должна ломать ответ пользователю.
    try:
        ranker = get_default_reranker()
        if ranker is None:
            return results[:top_k]
        if hasattr(ranker, "rerank") and not isinstance(ranker, _BM25_Reranker):
            from flashrank import RerankRequest

            passages = [{
                "id": r.get("document_id", r.get("chunk_id", "")),
                "text": r.get("text", r.get("content", "")),
                "metadata": r,
            } for r in results]
            ranked = ranker.rerank(RerankRequest(query=query, passages=passages))
            reranked = []
            for r in ranked:
                item = r.get("metadata", {})
                # ВАЖНО: только float, а не numpy.float32. Живой случай 2026-09-13: реранкер
                # работал, клал numpy-оценку в результат, и ответ чата падал на сериализации
                # (PydanticSerializationError: Unable to serialize unknown type: numpy.float32) —
                # все ответы приходили пустыми, что выглядело как «качество ответа 0».
                try:
                    item["rerank_score"] = float(r.get("score", r.get("rerank_score", 0)) or 0.0)
                except (TypeError, ValueError):
                    item["rerank_score"] = 0.0
                reranked.append(item)
            _out = reranked[:top_k]
            _record_rerank_change(results, _out)
            return _out
        _out = ranker.rerank(query, results, top_k)
        _record_rerank_change(results, _out)
        return _out
    except Exception as e:
        # Ошибка на конкретном запросе: пишем ОДИН раз на тип ошибки, порядок не меняем.
        global _logged_error_for
        key = (type(e).__name__, str(e)[:80])
        if _logged_error_for != key:
            _logged_error_for = key
            logger.warning(f"[reranker] ошибка реранжирования ({type(e).__name__}: {e}) — исходный порядок")
        return results[:top_k]
