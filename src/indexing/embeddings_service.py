"""
Сервис эмбеддингов для KAG

Интеграция с Ollama для генерации embeddings и Qdrant для хранения.
Используется для векторизации документов и семантического поиска.
"""

from typing import List, Optional, Dict, Any
from datetime import datetime
import uuid
from loguru import logger
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    VectorParams,
    PointStruct,
    Filter,
    FieldCondition,
    MatchValue,
    MatchAny,
    Range,
    PayloadSchemaType
)

from src.llm.embeddings import EmbeddingClient
from src.config import get_settings


def _build_qdrant_filter_condition(condition: FieldCondition) -> dict:
    """
    Convert a Qdrant FieldCondition to a dict suitable for REST API.
    
    Handles MatchValue and MatchAny.
    """
    match = condition.match
    if isinstance(match, MatchValue):
        return {"key": condition.key, "match": {"value": match.value}}
    elif isinstance(match, MatchAny):
        return {"key": condition.key, "match": {"any": match.any}}
    else:
        raise ValueError(f"Unsupported match type: {type(match)}")


class EmbeddingsService:
    """
    Сервис для работы с эмбеддингами и Qdrant.

    Отвечает за:
    - Генерацию embeddings через Ollama
    - Сохранение в Qdrant с метаданными
    - Семантический поиск
    - Управление коллекциями
    """

    def __init__(
        self,
        qdrant_url: Optional[str] = None,
        collection_name: Optional[str] = None,
        embedding_client: Optional[EmbeddingClient] = None
    ):
        """
        Инициализация сервиса.

        Args:
            qdrant_url: URL Qdrant сервера
            collection_name: Название коллекции
            embedding_client: Клиент для генерации embeddings
        """
        settings = get_settings()

        self.qdrant_url = qdrant_url or f"http://{settings.QDRANT_HOST}:{settings.QDRANT_PORT}"
        self.collection_name = collection_name or settings.QDRANT_COLLECTION

        # Клиенты
        self._qdrant_client: Optional[QdrantClient] = None
        self._embedding_client = embedding_client

        # Настройки
        self._embedding_dimensions = settings.EMBEDDING_DIMENSIONS
        # Размер батча для /api/embed. 32 текста на CPU-Ollama (bge-m3) идут
        # дольше 60с и упирались в timeout (пустые векторы). Уменьшено до 8 —
        # батч ~12с, надёжно укладывается в EMBEDDING_TIMEOUT.
        self._batch_size = 8

        logger.info(
            f"EmbeddingsService инициализирован: "
            f"qdrant={self.qdrant_url}, collection={self.collection_name}"
        )

    async def initialize(self):
        """Инициализировать подключения и создать коллекцию при необходимости"""
        # Создаем embedding клиент если не передан
        if self._embedding_client is None:
            settings = get_settings()
            model = settings.EMBEDDING_MODEL
            base_url = settings.EMBEDDING_BASE_URL
            provider_type = "ollama"
            api_key = ""
            
            # Приоритет: function_map/embedding из админки (Provider Architecture)
            try:
                from src.api.services.config_store import config_store
                fm = config_store.get("function_map", "embedding") or {}
                if fm and fm.get("provider_id") and fm.get("model"):
                    from src.api.services.provider_service import provider_service
                    provider = provider_service.get_provider_with_key(fm["provider_id"])
                    if provider:
                        base_url = (provider.url or "").rstrip("/")
                        model = fm["model"]
                        provider_type = "ollama" if provider.type == "ollama" else "openai"
                        api_key = provider.api_key or ""
                        logger.info(f"Embedding из админки: provider={provider.id}, model={model}, url={base_url}, type={provider_type}")
            except Exception as e:
                logger.debug(f"function_map/embedding не найден, использую .env: {e}")
            
            self._embedding_client = EmbeddingClient(
                base_url=base_url,
                model=model,
                timeout=settings.EMBEDDING_TIMEOUT,
                provider_type=provider_type,
                api_key=api_key
            )
            logger.info(f"Embedding клиент инициализирован: {model}")

        # Создаем Qdrant клиент (с api-key, если задан)
        import os
        api_key = os.environ.get("QDRANT_API_KEY", "")
        self._api_key = api_key
        self._qdrant_client = QdrantClient(url=self.qdrant_url, api_key=api_key) if api_key else QdrantClient(url=self.qdrant_url)

        # Проверяем подключение
        try:
            collections = self._qdrant_client.get_collections()
            logger.info(f"Подключено к Qdrant: {len(collections.collections)} коллекций")
        except Exception as e:
            logger.error(f"Ошибка подключения к Qdrant: {e}")
            raise

        # Создаем коллекцию если не существует
        await self._ensure_collection()

        logger.info("EmbeddingsService инициализирован успешно")

    async def ensure_model(self):
        """Проверить актуальность embedding модели из настроек. Если модель/провайдер сменились — пересоздать клиент."""
        settings = get_settings()
        new_model = settings.EMBEDDING_MODEL
        new_base_url = settings.EMBEDDING_BASE_URL
        new_provider_type = "ollama"
        new_api_key = ""
        try:
            from src.api.services.config_store import config_store
            fm = config_store.get("function_map", "embedding") or {}
            if fm and fm.get("provider_id") and fm.get("model"):
                from src.api.services.provider_service import provider_service
                provider = provider_service.get_provider_with_key(fm["provider_id"])
                if provider:
                    new_base_url = (provider.url or "").rstrip("/")
                    new_model = fm["model"]
                    new_provider_type = "ollama" if provider.type == "ollama" else "openai"
                    new_api_key = provider.api_key or ""
        except Exception:
            pass
        if self._embedding_client and (
            self._embedding_client.model != new_model
            or self._embedding_client.base_url != new_base_url
            or self._embedding_client.provider_type != new_provider_type
        ):
            logger.info(f"♻️ Embedding модель изменилась: {self._embedding_client.model} → {new_model}")
            self._embedding_client = EmbeddingClient(
                base_url=new_base_url, model=new_model, timeout=settings.EMBEDDING_TIMEOUT,
                provider_type=new_provider_type, api_key=new_api_key
            )
            self._embedding_dimensions = int(settings.EMBEDDING_DIMENSIONS)
            logger.info(f"Embedding клиент пересоздан: {new_model}")

    async def _ensure_collection(self):
        """Создать коллекцию если не существует (dense + sparse)"""
        try:
            collections = self._qdrant_client.get_collections().collections
            exists = any(c.name == self.collection_name for c in collections)

            if not exists:
                logger.info(f"Создание гибридной коллекции: {self.collection_name}")

                from qdrant_client.http.models import SparseVectorParams, SparseIndexParams, VectorParams

                # Определяем размерность из фактической модели (generate один embedding)
                dim = self._embedding_dimensions
                try:
                    test_emb = await self._embedding_client.generate("test")
                    dim = len(test_emb)
                    logger.info(f"Фактическая размерность модели: {dim}")
                except Exception:
                    pass

                self._qdrant_client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config={
                        "dense": VectorParams(
                            size=dim,
                            distance=Distance.COSINE
                        ),
                    },
                    sparse_vectors_config={
                        "sparse": SparseVectorParams(
                            index=SparseIndexParams(
                                on_disk=False,
                            )
                        ),
                    },
                )

                # Создаем индексы для payload полей
                self._qdrant_client.create_payload_index(
                    collection_name=self.collection_name,
                    field_name="document_id",
                    field_schema=PayloadSchemaType.KEYWORD
                )

                self._qdrant_client.create_payload_index(
                    collection_name=self.collection_name,
                    field_name="chunk_id",
                    field_schema=PayloadSchemaType.KEYWORD
                )

                self._qdrant_client.create_payload_index(
                    collection_name=self.collection_name,
                    field_name="file_type",
                    field_schema=PayloadSchemaType.KEYWORD
                )

                self._qdrant_client.create_payload_index(
                    collection_name=self.collection_name,
                    field_name="filename",
                    field_schema=PayloadSchemaType.KEYWORD
                )

                self._qdrant_client.create_payload_index(
                    collection_name=self.collection_name,
                    field_name="group_ids",
                    field_schema=PayloadSchemaType.KEYWORD
                )

                logger.info(f"Коллекция создана: {self.collection_name}")
            else:
                # Коллекция есть — проверяем, совпадает ли размерность с текущей моделью.
                # Если модель сменилась (например 768→1024), пересоздаём коллекцию,
                # иначе вставка векторов падает с "dimension error".
                try:
                    _info = self._qdrant_client.get_collection(self.collection_name)
                    _existing_dim = None
                    _vc = _info.config.params.vectors
                    if isinstance(_vc, dict) and "dense" in _vc:
                        _vd = _vc["dense"]
                        _existing_dim = getattr(_vd, "size", None)
                    elif hasattr(_vc, "size"):
                        _existing_dim = _vc.size

                    _dim = self._embedding_dimensions
                    try:
                        _test = await self._embedding_client.generate("test")
                        _dim = len(_test)
                    except Exception:
                        pass

                    if _existing_dim is not None and _existing_dim != _dim:
                        logger.warning(
                            f"Размерность коллекции ({_existing_dim}) != модели ({_dim}) — пересоздаю коллекцию"
                        )
                        self._qdrant_client.delete_collection(self.collection_name)
                        return await self._ensure_collection()
                    logger.info(f"Коллекция существует: {self.collection_name} (dim={_existing_dim})")
                except Exception:
                    logger.info(f"Коллекция существует: {self.collection_name}")

        except Exception as e:
            logger.error(f"Ошибка создания коллекции: {e}")
            raise

    async def embed_and_store(
        self,
        document_id: str,
        chunks: List[Dict[str, Any]],
        metadata: Optional[Dict[str, Any]] = None,
        group_ids: Optional[List[str]] = None
    ) -> int:
        """
        Сгенерировать embeddings для чанков и сохранить в Qdrant.
        """
        # Инициализируем если нужно
        await self.initialize()

        # Проверяем актуальность модели (пользователь мог сменить её в админке)
        await self.ensure_model()

        if not self._embedding_client:
            raise RuntimeError("Embedding клиент не инициализирован")
        
        if not chunks:
            logger.warning("Пустой список чанков")
            return 0

        logger.info(f"Embed & Store: document={document_id}, chunks={len(chunks)}")

        # Извлекаем тексты
        texts = [chunk.get("content", "") for chunk in chunks]

        # Генерируем embeddings батчами
        embeddings = await self._embedding_client.generate_batch(texts, batch_size=self._batch_size)

        # Генерируем sparse векторы (BM25) — отключено для скорости, включить после обработки
        sparse_embeddings = [None] * len(texts)

        # Создаем точки для Qdrant
        points = []
        for i, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
            point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{document_id}-{i}"))

            # Формируем вектор: dense (основной) + sparse (BM25)
            vectors = {"dense": embedding}
            if sparse_embeddings[i] is not None:
                se = sparse_embeddings[i]
                vectors["sparse"] = {
                    "indices": se.indices.tolist(),
                    "values": se.values.tolist(),
                }

            # Извлекаем filename из metadata для прямого сохранения в payload
            filename = ""
            if metadata:
                filename = metadata.get("filename", "")
            
            payload = {
                "document_id": document_id,
                "chunk_id": chunk.get("chunk_id", f"{document_id}_chunk_{i}"),
                "content": chunk.get("content", ""),
                "file_type": metadata.get("file_type", "unknown") if metadata else "unknown",
                "filename": filename,  # Сохраняем filename напрямую для быстрого доступа
                "group_ids": group_ids or [],
                "metadata": {
                    **(metadata or {}),
                    **(chunk.get("metadata", {}))
                },
                "created_at": datetime.utcnow().isoformat()
            }

            points.append(
                PointStruct(
                    id=point_id,
                    vector=vectors,
                    payload=payload
                )
            )

        # Сохраняем в Qdrant батчами
        total_saved = 0
        for i in range(0, len(points), self._batch_size):
            batch = points[i:i + self._batch_size]
            self._qdrant_client.upsert(
                collection_name=self.collection_name,
                points=batch
            )
            total_saved += len(batch)

        logger.info(f"Сохранено {total_saved} векторов в Qdrant")
        return total_saved

    async def update_document_type_payload(self, document_id: str, document_type: str):
        """Обновить document_type в payload всех чанков документа в Qdrant."""
        try:
            # Находим все точки документа
            from qdrant_client.http import models as qmodels
            scroll_result = self._qdrant_client.scroll(
                collection_name=self.collection_name,
                scroll_filter=qmodels.Filter(
                    must=[
                        qmodels.FieldCondition(
                            key="document_id",
                            match=qmodels.MatchValue(value=document_id)
                        )
                    ]
                ),
                limit=1000,
                with_payload=True,
                with_vectors=False
            )
            points, _ = scroll_result
            if not points:
                logger.debug(f"Нет чанков для обновления типа {document_id}")
                return False

            # Обновляем payload каждой точки
            from qdrant_client.http import models as qmodels2
            updated_points = []
            for p in points:
                p.payload["document_type"] = document_type
                updated_points.append(
                    qmodels2.PointStruct(id=p.id, vector=p.vector, payload=p.payload)
                )

            self._qdrant_client.upsert(
                collection_name=self.collection_name,
                points=updated_points
            )
            logger.info(f"Обновлён document_type={document_type} для {len(updated_points)} чанков {document_id}")
            return True
        except Exception as e:
            logger.warning(f"Не удалось обновить document_type в Qdrant: {e}")
            return False

    async def search(
        self,
        query: str,
        limit: int = 10,
        filters: Optional[Dict[str, Any]] = None,
        group_ids: Optional[List[str]] = None,
        is_admin: bool = False
    ) -> List[Dict[str, Any]]:
        """
        Семантический поиск по embeddings.

        Args:
            query: Поисковый запрос
            limit: Количество результатов
            filters: Фильтры по метаданным
            group_ids: Список group_id для фильтрации (None = без фильтрации)
            is_admin: Если True, group_ids фильтр не применяется

        Returns:
            Список результатов с текстом и score
        """
        logger.debug(f"Поиск: query='{query[:100]}', limit={limit}")

        # Генерируем embedding для запроса
        query_embedding = await self._embedding_client.generate(query)

        # Создаем фильтр если есть
        conditions = []
        if filters:
            if "document_id" in filters:
                conditions.append(
                    FieldCondition(
                        key="document_id",
                        match=MatchValue(value=filters["document_id"])
                    )
                )

            if "file_type" in filters:
                conditions.append(
                    FieldCondition(
                        key="file_type",
                        match=MatchValue(value=filters["file_type"])
                    )
                )

        # Group-based access control: filter by group_ids unless admin
        if not is_admin and group_ids:
            conditions.append(
                FieldCondition(
                    key="group_ids",
                    match=MatchAny(any=group_ids)
                )
            )

        # Поиск через qdrant_client (прямой, надёжный). REST-запрос с
        # prefetch+rrf-fusion падал на этой версии Qdrant — заменён на клиентский.
        formatted_results = []
        try:
            from qdrant_client.models import Filter as QFilter

            query_filter = None
            if conditions:
                from qdrant_client.models import FieldCondition as _FC
                must = []
                for c in conditions:
                    # c — это наш FieldCondition (key, match). Пересобираем в qdrant-модель.
                    if hasattr(c, "key") and hasattr(c, "match"):
                        m = c.match
                        from qdrant_client.models import MatchValue, MatchAny
                        if hasattr(m, "value"):
                            must.append(_FC(key=c.key, match=MatchValue(value=m.value)))
                        elif hasattr(m, "any"):
                            must.append(_FC(key=c.key, match=MatchAny(any=m.any)))
                if must:
                    query_filter = QFilter(must=must)

            hits = self._qdrant_client.search(
                collection_name=self.collection_name,
                query_vector=("dense", query_embedding),
                limit=limit,
                query_filter=query_filter,
                with_payload=True,
            )

            for hit in hits:
                payload = hit.payload or {}
                formatted_results.append({
                    "id": hit.id,
                    "score": hit.score,
                    "content": payload.get("content", ""),
                    "document_id": payload.get("document_id"),
                    "chunk_id": payload.get("chunk_id"),
                    "file_type": payload.get("file_type"),
                    "filename": payload.get("filename", ""),
                    "metadata": payload.get("metadata", {})
                })
        except Exception as e:
            logger.warning(f"Поиск не выполнен: {e}")

        # Reranking: если результатов много и запрос осмысленный (>3 слов)
        if len(formatted_results) > 3 and len(query.split()) >= 3:
            try:
                from src.indexing.reranker import rerank_search_results
                formatted_results = await rerank_search_results(query, formatted_results, top_k=limit)
                logger.debug(f"После reranking: {len(formatted_results)} результатов")
            except Exception as e:
                logger.warning(f"Reranking failed (non-critical): {e}")

        logger.debug(f"Найдено {len(formatted_results)} результатов")
        return formatted_results

    async def delete_document(self, document_id: str) -> bool:
        """
        Удалить все чанки документа из Qdrant.

        Args:
            document_id: ID документа

        Returns:
            True если успешно
        """
        try:
            self._qdrant_client.delete(
                collection_name=self.collection_name,
                points_selector=Filter(
                    must=[
                        FieldCondition(
                            key="document_id",
                            match=MatchValue(value=document_id)
                        )
                    ]
                )
            )

            logger.info(f"Документ удален из Qdrant: {document_id}")
            return True
        except Exception as e:
            logger.error(f"Ошибка удаления документа: {e}")
            return False

    async def delete_all(self) -> bool:
        """Удалить все чанки из Qdrant"""
        try:
            self._qdrant_client.delete(
                collection_name=self.collection_name,
                points_selector=Filter(must=[])
            )
            logger.info("Все документы удалены из Qdrant")
            return True
        except Exception as e:
            logger.error(f"Ошибка очистки Qdrant: {e}")
            return False

    async def get_collection_stats(self) -> Dict[str, Any]:
        """
        Получить статистику коллекции.

        Returns:
            Словарь со статистикой
        """
        try:
            info = self._qdrant_client.get_collection(self.collection_name)

            return {
                "collection_name": self.collection_name,
                "vectors_count": info.vectors_count,
                "indexed_vectors_count": info.indexed_vectors_count,
                "points_count": info.points_count,
                "segments_count": info.segments_count,
                "config": {
                    "vector_size": self._embedding_dimensions,
                    "distance": "cosine"
                }
            }

        except Exception as e:
            logger.error(f"Ошибка получения статистики: {e}")
            return {"error": str(e)}

    async def get_document_chunks(self, document_id: str) -> List[Dict[str, Any]]:
        """
        Получить все чанки документа.

        Args:
            document_id: ID документа

        Returns:
            Список чанков
        """
        try:
            if self._qdrant_client is None:
                await self.initialize()
            results, _ = self._qdrant_client.scroll(
                collection_name=self.collection_name,
                scroll_filter=Filter(
                    must=[
                        FieldCondition(
                            key="document_id",
                            match=MatchValue(value=document_id)
                        )
                    ]
                ),
                limit=1000
            )

            chunks = []
            for point in results:
                chunks.append({
                    "id": point.id,
                    "content": point.payload.get("content", ""),
                    "chunk_id": point.payload.get("chunk_id"),
                    "metadata": point.payload.get("metadata", {})
                })

            # Сортируем по chunk_seq (из metadata), fallback на chunk_id
            chunks.sort(key=lambda x: x.get("metadata", {}).get("chunk_seq", 0) or 0)

            return chunks

        except Exception as e:
            logger.error(f"Ошибка получения чанков: {e}")
            return []

    @property
    def embedding_client(self) -> EmbeddingClient:
        """Получить embedding клиент"""
        return self._embedding_client

    async def close(self):
        """Закрыть подключения"""
        if self._embedding_client:
            await self._embedding_client.close()
        logger.info("EmbeddingsService закрыт")


# Глобальный экземпляр
embeddings_service = EmbeddingsService()
