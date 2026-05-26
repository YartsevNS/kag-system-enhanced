"""
Reranker сервис для улучшения качества поиска

Использует Cross-Encoder для пересортировки результатов поиска
и отбора наиболее релевантных документов для LLM.
"""

from typing import List, Dict, Any, Optional
from loguru import logger

try:
    from sentence_transformers import CrossEncoder
    CROSS_ENCODER_AVAILABLE = True
except ImportError:
    CROSS_ENCODER_AVAILABLE = False
    logger.warning("sentence-transformers не установлен. Reranking недоступен.")


class RerankerService:
    """
    Сервис для reranking'а поисковых результатов.
    
    Flow:
    1. Получить топ-20 результатов из векторного поиска
    2. Оценить релевантность каждого документа запросу через Cross-Encoder
    3. Отсортировать по score
    4. Вернуть топ-5 самых релевантных для LLM
    """
    
    def __init__(self, model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"):
        """
        Инициализация сервиса.
        
        Args:
            model_name: Название модели для reranking
                - cross-encoder/ms-marco-MiniLM-L-6-v2 (англ/мультиязычный)
                - deepvk/rubert-base-cased-reranker (русский язык)
                - cointegrated/rubert-tiny2-reranker (русский, быстрый)
        """
        self.model_name = model_name
        self.model: Optional[CrossEncoder] = None
        
        if CROSS_ENCODER_AVAILABLE:
            try:
                # Пробуем русскоязычную модель, если не найдена - fallback на английскую
                russian_models = [
                    "deepvk/rubert-base-cased-reranker",
                    "cointegrated/rubert-tiny2-reranker",
                    "ai-forever/rubert-base-cased-reranker"
                ]
                
                for ru_model in russian_models:
                    try:
                        logger.info(f"Попытка загрузки русской модели: {ru_model}")
                        self.model = CrossEncoder(ru_model, max_length=512)
                        self.model_name = ru_model
                        logger.info(f"Загружена русская модель для reranking: {ru_model}")
                        break
                    except Exception:
                        continue
                
                # Если русская не загрузилась, пробуем английскую
                if self.model is None:
                    logger.info(f"Загрузка модели: {model_name}")
                    self.model = CrossEncoder(model_name, max_length=512)
                    logger.info(f"Загружена модель для reranking: {model_name}")
                    
            except Exception as e:
                logger.error(f"Ошибка загрузки модели reranking: {e}")
                self.model = None
        else:
            logger.warning("CrossEncoder недоступен - reranking отключен")
    
    def rerank(
        self,
        query: str,
        documents: List[Dict[str, Any]],
        top_k: int = 5
    ) -> List[Dict[str, Any]]:
        """
        Пересортировать документы по релевантности запросу.
        
        Args:
            query: Поисковый запрос
            documents: Список документов (топ-20 из векторного поиска)
            top_k: Количество документов для возврата (обычно 3-5)
        
        Returns:
            Отсортированный список топ-k документов
        """
        if not CROSS_ENCODER_AVAILABLE or self.model is None:
            logger.warning("Reranking недоступен, возвращаем исходные результаты")
            return documents[:top_k]
        
        if not documents:
            return []
        
        # Берем не более top_k * 4 документов для reranking (оптимизация)
        docs_to_rerank = documents[: min(len(documents), top_k * 4)]
        
        try:
            # Извлекаем тексты документов
            texts = [doc.get("content", "") for doc in docs_to_rerank]
            
            # Создаем пары (query, document) для Cross-Encoder
            pairs = [[query, text] for text in texts]
            
            # Получаем scores релевантности
            scores = self.model.predict(pairs, show_progress_bar=False, batch_size=32)
            
            # Собираем результаты с scores
            ranked_docs = []
            for doc, score in zip(docs_to_rerank, scores):
                doc_copy = doc.copy()
                doc_copy["rerank_score"] = float(score)
                ranked_docs.append(doc_copy)
            
            # Сортируем по убыванию релевантности
            ranked_docs.sort(key=lambda x: x["rerank_score"], reverse=True)
            
            # Возвращаем топ-k
            result = ranked_docs[:top_k]
            
            logger.info(
                f"Reranking выполнен: {len(docs_to_rerank)} -> {len(result)} документов, "
                f"top_score={result[0]['rerank_score']:.3f if result else 0:.3f}"
            )
            
            return result
            
        except Exception as e:
            logger.error(f"Ошибка при reranking: {e}")
            # Fallback: возвращаем исходные результаты
            return documents[:top_k]
    
    def rerank_with_threshold(
        self,
        query: str,
        documents: List[Dict[str, Any]],
        threshold: float = 0.5,
        max_results: int = 5
    ) -> List[Dict[str, Any]]:
        """
        Reranking с порогом отсечения.
        
        Args:
            query: Поисковый запрос
            documents: Список документов
            threshold: Минимальный score для включения в результат
            max_results: Максимальное количество результатов
        
        Returns:
            Документы с score >= threshold
        """
        ranked = self.rerank(query, documents, top_k=max_results)
        
        # Фильтруем по порогу
        filtered = [doc for doc in ranked if doc.get("rerank_score", 0) >= threshold]
        
        if len(filtered) < len(ranked):
            logger.info(
                f"Отсечено {len(ranked) - len(filtered)} документов ниже порога {threshold}"
            )
        
        return filtered


# Глобальный экземпляр сервиса
reranker_service = RerankerService()
