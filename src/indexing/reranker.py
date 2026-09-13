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

_FLASH_AVAILABLE = False
_BM25_AVAILABLE = False
_ranker_instance = None
_bm25_instance = None
_load_error: str = ""
_loaded_for: tuple = ()          # (model, cache_dir) последней удачной/неудачной попытки
_logged_error_for: tuple = ()    # чтобы не повторять одну и ту же ошибку в логе


def get_reranker_config() -> Dict[str, Any]:
    """Настройки реранкера из config_store (namespace reranker, ключ config)."""
    cfg: Dict[str, Any] = {
        "enabled": False,
        "model": DEFAULT_MODEL,
        "cache_dir": DEFAULT_CACHE_DIR,
        "top_k": DEFAULT_TOP_K,
    }
    try:
        from src.api.services.config_store import config_store
        stored = config_store.get("reranker", "config") or {}
        if isinstance(stored, dict):
            if "enabled" in stored:
                cfg["enabled"] = bool(stored["enabled"])
            if stored.get("model"):
                cfg["model"] = str(stored["model"]).strip()
            if stored.get("cache_dir"):
                cfg["cache_dir"] = str(stored["cache_dir"]).strip()
            if stored.get("top_k"):
                try:
                    cfg["top_k"] = max(1, min(50, int(stored["top_k"])))
                except (TypeError, ValueError):
                    pass
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
            return reranked[:top_k]
        return ranker.rerank(query, results, top_k)
    except Exception as e:
        # Ошибка на конкретном запросе: пишем ОДИН раз на тип ошибки, порядок не меняем.
        global _logged_error_for
        key = (type(e).__name__, str(e)[:80])
        if _logged_error_for != key:
            _logged_error_for = key
            logger.warning(f"[reranker] ошибка реранжирования ({type(e).__name__}: {e}) — исходный порядок")
        return results[:top_k]
