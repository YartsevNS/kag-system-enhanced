"""
Лёгкий reranker для переранжирования результатов поиска.

Использует FlashRank (ONNX-based, без torch).
Fallback: rank_bm25 если FlashRank не загрузился.

Установка:
    pip install flashrank rank-bm25
"""

from typing import List, Dict, Any, Optional
from loguru import logger

# FlashRank — лёгкий ONNX-ранкер (без torch)
_FLASH_AVAILABLE = False
_BM25_AVAILABLE = False
_ranker_instance = None
_bm25_instance = None


def get_default_reranker() -> Optional[Any]:
    """
    Получить экземпляр ранкера.
    
    Приоритет:
    1. FlashRank (ONNX, быстрый, до 1M параметров)
    2. BM25 (ранжирование по совпадению слов, без нейросетей)
    3. None (ранжирование не доступно)
    
    Returns:
        Ранкер с методом rerank(query, passages, top_k) или None
    """
    global _ranker_instance, _bm25_instance, _FLASH_AVAILABLE, _BM25_AVAILABLE

    # Пробуем FlashRank
    if _ranker_instance is None and not _FLASH_AVAILABLE:
        try:
            from flashrank import Ranker as FlashRanker, RerankRequest

            # Небольшая модель: ~50MB, работает на CPU через ONNX
            _ranker_instance = FlashRanker(
                model_name="ms-marco-MiniLM-L-12-v2",
                cache_dir="/app/models/.flashrank",
            )
            _FLASH_AVAILABLE = True
            logger.info("✅ FlashRank загружен (ONNX, без torch)")
        except Exception as e:
            logger.warning(f"FlashRank не загружен: {e}")
            _FLASH_AVAILABLE = False

    if _FLASH_AVAILABLE and _ranker_instance:
        return _ranker_instance

    # Пробуем BM25 как fallback
    if _bm25_instance is None and not _BM25_AVAILABLE:
        try:
            from rank_bm25 import BM25Okapi
            _BM25_AVAILABLE = True
            logger.info("✅ BM25 доступен (fallback reranker)")
        except Exception as e:
            logger.warning(f"BM25 не загружен: {e}")
            _BM25_AVAILABLE = False

    if _BM25_AVAILABLE:
        return _BM25_Reranker()

    return None


class _BM25_Reranker:
    """
    BM25 reranker — дёшево, без нейросетей, работает на любых языках.
    Используется как fallback когда FlashRank недоступен.
    """

    def rerank(
        self,
        query: str,
        passages: List[Dict[str, Any]],
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        """
        Переранжировать пассажи по BM25.

        Args:
            query: Поисковый запрос
            passages: Список пассажей с полем 'text'
            top_k: Сколько результатов вернуть

        Returns:
            Отсортированный список пассажей (от лучшего к худшему)
        """
        from rank_bm25 import BM25Okapi

        if not passages:
            return []

        # Токенизация (простое разбиение по пробелам)
        tokenized_corpus = [p.get("text", "").split() for p in passages]
        bm25 = BM25Okapi(tokenized_corpus)
        tokenized_query = query.split()

        scores = bm25.get_scores(tokenized_query)

        # Сортируем по убыванию score
        indexed = list(enumerate(passages))
        indexed.sort(key=lambda x: scores[x[0]], reverse=True)

        results = []
        for idx, passage in indexed[:top_k]:
            passage["rerank_score"] = float(scores[idx])
            passage["original_index"] = idx
            results.append(passage)

        return results


# ============================================================
# Удобная функция для использования в chat_service / search
# ============================================================

async def rerank_search_results(
    query: str,
    results: List[Dict[str, Any]],
    top_k: int = 5,
) -> List[Dict[str, Any]]:
    """
    Переранжировать результаты поиска.

    Args:
        query: Текст запроса
        results: Результаты из Qdrant (список словарей с полем 'text')
        top_k: Сколько лучших вернуть

    Returns:
        Переранжированный список (top_k элементов)
    """
    if not results:
        return []

    ranker = get_default_reranker()
    if ranker is None:
        # Ранкер недоступен — возвращаем как есть, обрезая до top_k
        return results[:top_k]

    try:
        # FlashRank: использует метод rerank
        if hasattr(ranker, "rerank") and not isinstance(ranker, _BM25_Reranker):
            from flashrank import RerankRequest

            # Преобразуем результаты в формат FlashRank
            passages = []
            for r in results:
                passages.append({
                    "id": r.get("document_id", r.get("chunk_id", "")),
                    "text": r.get("text", r.get("content", "")),
                    "metadata": r,
                })

            req = RerankRequest(query=query, passages=passages)
            ranked = ranker.rerank(req)

            # FlashRank возвращает отсортированные результаты
            reranked = []
            for r in ranked:
                item = r.get("metadata", {})
                item["rerank_score"] = r.get("score", r.get("rerank_score", 0))
                reranked.append(item)

            return reranked[:top_k]

        else:
            # BM25 fallback
            return ranker.rerank(query, results, top_k)

    except Exception as e:
        logger.warning(f"Reranker error: {e}, fallback to original order")
        return results[:top_k]
