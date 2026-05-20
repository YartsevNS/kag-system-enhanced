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
        self._batch_size = 32  # Размер батча для Qdrant

        logger.info(
            f"EmbeddingsService инициализирован: "
            f"qdrant={self.qdrant_url}, collection={self.collection_name}"
        )

    async def initialize(self):
        """Инициализировать подключения и создать коллекцию при необходимости"""
        # Создаем embedding клиент если не передан
        if self._embedding_client is None:
            settings = get_settings()
            self._embedding_client = EmbeddingClient(
                base_url=settings.EMBEDDING_BASE_URL,
                model=settings.EMBEDDING_MODEL,
                timeout=settings.EMBEDDING_TIMEOUT
            )
            logger.info(f"Embedding клиент инициализирован: {settings.EMBEDDING_MODEL}")

        # Создаем Qdrant клиент
        self._qdrant_client = QdrantClient(url=self.qdrant_url)

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

    async def _ensure_collection(self):
        """Создать коллекцию если не существует"""
        try:
            # Проверяем существует ли коллекция
            collections = self._qdrant_client.get_collections().collections
            exists = any(c.name == self.collection_name for c in collections)

            if not exists:
                logger.info(f"Создание коллекции: {self.collection_name}")

                from qdrant_client.http.models import HnswConfigDiff, OptimizersConfigDiff, ScalarQuantization, ScalarType

                self._qdrant_client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=VectorParams(
                        size=self._embedding_dimensions,
                        distance=Distance.COSINE
                    ),
                    hnsw_config=HnswConfigDiff(
                        m=32,            # больше связей = точнее поиск
                        ef_construct=128  # качественнее построение графа
                    ),
                    optimizers_config=OptimizersConfigDiff(
                        indexing_threshold=5000  # индексация после 5000 точек
                    ),
                    quantization_config=ScalarQuantization(
                        scalar=ScalarType.INT8,  # int8 квантование для скорости
                        always_ram=True
                    )
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
                    field_name="group_ids",
                    field_schema=PayloadSchemaType.KEYWORD
                )

                logger.info(f"Коллекция создана: {self.collection_name}")
            else:
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

        # Создаем точки для Qdrant
        points = []
        for i, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
            point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{document_id}-{i}"))

            payload = {
                "document_id": document_id,
                "chunk_id": chunk.get("chunk_id", f"{document_id}_chunk_{i}"),
                "content": chunk.get("content", ""),
                "file_type": metadata.get("file_type", "unknown") if metadata else "unknown",
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
                    vector=embedding,
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

        # Ищем через REST API (обходим несовместимость клиента)
        formatted_results = []
        try:
            import requests
            search_payload = {
                "vector": query_embedding,
                "limit": limit,
                "with_payload": True
            }
            if conditions:
                search_payload["filter"] = {
                    "must": [
                        _build_qdrant_filter_condition(c) for c in conditions
                    ]
                }

            resp = requests.post(
                f"{self.qdrant_url}/collections/{self.collection_name}/points/search",
                json=search_payload,
                timeout=30
            )
            resp.raise_for_status()
            hits = resp.json().get("result", [])

            for hit in hits:
                payload = hit.get("payload", {})
                formatted_results.append({
                    "id": hit["id"],
                    "score": hit["score"],
                    "content": payload.get("content", ""),
                    "document_id": payload.get("document_id"),
                    "chunk_id": payload.get("chunk_id"),
                    "file_type": payload.get("file_type"),
                    "metadata": payload.get("metadata", {})
                })
        except Exception as e:
            logger.warning(f"REST search failed: {e}")

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
