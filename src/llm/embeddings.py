"""
Embedding клиент для Ollama

Генерирует векторные представления текста через Ollama API.
Используется для:
- Векторизации документов перед сохранением в Qdrant
- Векторизации поисковых запросов
- Семантического поиска
"""

from typing import List, Optional, Union
import httpx
import asyncio
from loguru import logger
from pydantic import BaseModel, Field


class EmbeddingResponse(BaseModel):
    """Ответ от embedding API"""
    embedding: List[float] = Field(..., description="Вектор эмбеддинга")
    model: str = Field(..., description="Использованная модель")
    total_tokens: int = Field(default=0, description="Количество токенов")


class EmbeddingClient:
    """
    Клиент для генерации эмбеддингов через Ollama.

    Ollama предоставляет endpoint /api/embeddings для генерации
    векторных представлений текста.

    Пример использования:
        client = EmbeddingClient(
            base_url="http://192.168.50.41:11434",
            model="nomic-embed-text"
        )
        vector = await client.generate("Текст документа")
    """

    def __init__(
        self,
        base_url: str = "http://192.168.50.41:11434",
        model: str = "nomic-embed-text:latest",
        timeout: float = 60.0,
        max_retries: int = 3,
        retry_delay: float = 1.0
    ):
        """
        Инициализация embedding клиента.

        Args:
            base_url: URL Ollama сервера
            model: Модель для эмбеддингов
            timeout: Таймаут запросов
            max_retries: Максимум повторных попыток
            retry_delay: Задержка между попытками
        """
        self.base_url = base_url.rstrip('/')
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay

        self._client: Optional[httpx.AsyncClient] = None
        self._dimensions: Optional[int] = None

        logger.info(
            f"EmbeddingClient инициализирован: "
            f"base_url={self.base_url}, model={self.model}"
        )

    async def _get_client(self) -> httpx.AsyncClient:
        """Получить HTTP клиент"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(
                    connect=10.0,
                    read=self.timeout,
                    write=self.timeout,
                    pool=self.timeout
                ),
                limits=httpx.Limits(
                    max_connections=50,
                    max_keepalive_connections=10
                )
            )

        return self._client

    async def close(self):
        """Закрыть HTTP клиент"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            logger.info("Embedding клиент закрыт")

    async def generate(self, text: str) -> List[float]:
        """
        Сгенерировать embedding для текста.

        Args:
            text: Текст для векторизации

        Returns:
            Вектор эмбеддинга (список float)

        Raises:
            Exception: Ошибка генерации
        """
        last_error = None

        for attempt in range(self.max_retries):
            try:
                client = await self._get_client()

                logger.debug(f"Embedding запрос: text_length={len(text)}, attempt={attempt+1}")

                response = await client.post(
                    "/api/embeddings",
                    json={
                        "model": self.model,
                        "prompt": text
                    }
                )

                if response.status_code == 404:
                    raise ValueError(
                        f"Модель не найдена: {self.model}. "
                        f"Выполните: ollama pull {self.model}"
                    )
                elif response.status_code != 200:
                    raise ValueError(
                        f"Ошибка embedding API (код: {response.status_code}): {response.text}"
                    )

                data = response.json()
                embedding = data.get("embedding", [])

                if not embedding:
                    raise ValueError("Пустой embedding в ответе")

                # Сохраняем размерность
                if self._dimensions is None:
                    self._dimensions = len(embedding)

                logger.debug(f"Embedding сгенерирован: dimensions={len(embedding)}")
                return embedding

            except httpx.TimeoutException:
                last_error = TimeoutError(f"Таймаут embedding запроса ({self.timeout}с)")
                logger.warning(last_error)
            except httpx.ConnectError as e:
                last_error = ConnectionError(f"Ошибка подключения к Ollama: {e}")
                logger.error(last_error)
                raise  # Не retry'им ошибки подключения
            except Exception as e:
                last_error = e
                logger.warning(f"Embedding ошибка (попытка {attempt+1}): {e}")

            # Пауза перед повторной попыткой
            if attempt < self.max_retries - 1:
                wait_time = self.retry_delay * (2 ** attempt)
                await asyncio.sleep(wait_time)

        raise last_error

    async def generate_batch(
        self,
        texts: List[str],
        batch_size: int = 10
    ) -> List[List[float]]:
        """
        Сгенерировать embeddings для списка текстов.

        Args:
            texts: Список текстов
            batch_size: Размер батча для параллельной обработки

        Returns:
            Список векторов (по одному на каждый текст)
        """
        logger.info(f"Batch embedding: {len(texts)} текстов, batch_size={batch_size}")

        embeddings = []
        total = len(texts)

        for i in range(0, total, batch_size):
            batch = texts[i:i + batch_size]
            batch_num = (i // batch_size) + 1
            total_batches = (total + batch_size - 1) // batch_size

            logger.debug(
                f"Обработка батча {batch_num}/{total_batches}: "
                f"{len(batch)} текстов"
            )

            # Параллельная обработка батча
            tasks = [self.generate(text) for text in batch]
            batch_embeddings = await asyncio.gather(*tasks, return_exceptions=True)

            # Обрабатываем результаты
            for j, emb in enumerate(batch_embeddings):
                if isinstance(emb, Exception):
                    logger.error(f"Ошибка в батче для текста {i+j}: {emb}")
                    # Возвращаем пустой вектор при ошибке
                    embeddings.append([0.0] * (self._dimensions or 768))
                else:
                    embeddings.append(emb)

        logger.info(f"Batch embedding завершен: {len(embeddings)} векторов")
        return embeddings

    async def generate_for_document(
        self,
        chunks: List[str],
        metadata: Optional[List[dict]] = None
    ) -> List[dict]:
        """
        Сгенерировать embeddings для чанков документа.

        Args:
            chunks: Список чанков текста
            metadata: Метаданные для каждого чанка

        Returns:
            Список словарей с embedding и метаданными
        """
        logger.info(f"Document embedding: {len(chunks)} чанков")

        embeddings = await self.generate_batch(chunks)

        results = []
        for i, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
            result = {
                "chunk_id": f"chunk_{i}",
                "content": chunk,
                "embedding": embedding,
                "metadata": metadata[i] if metadata and i < len(metadata) else {}
            }
            results.append(result)

        logger.info(f"Document embedding завершен: {len(results)} чанков")
        return results

    async def health_check(self) -> dict:
        """
        Проверить доступность Ollama embedding API.

        Returns:
            Словарь со статусом проверки
        """
        try:
            client = await self._get_client()

            # Пробуем сгенерировать embedding для простого текста
            start_time = asyncio.get_event_loop().time()

            response = await client.post(
                "/api/embeddings",
                json={
                    "model": self.model,
                    "prompt": "test"
                },
                timeout=10.0
            )

            response_time_ms = (asyncio.get_event_loop().time() - start_time) * 1000

            if response.status_code == 200:
                data = response.json()
                dimensions = len(data.get("embedding", []))

                return {
                    "healthy": True,
                    "model": self.model,
                    "dimensions": dimensions,
                    "response_time_ms": response_time_ms
                }
            else:
                return {
                    "healthy": False,
                    "error": f"HTTP {response.status_code}: {response.text}"
                }

        except Exception as e:
            return {
                "healthy": False,
                "error": str(e)
            }

    @property
    def dimensions(self) -> int:
        """Получить размерность embedding вектора"""
        if self._dimensions is None:
            raise ValueError(
                "Размерность еще не известна. "
                "Выполните хотя бы один запрос generate() сначала."
            )
        return self._dimensions

    async def __aenter__(self):
        """Async context manager entry"""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        await self.close()


# ===========================================
# Утилиты для работы с embeddings
# ===========================================

def cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
    """
    Вычислить косинусное сходство между двумя векторами.

    Args:
        vec1: Первый вектор
        vec2: Второй вектор

    Returns:
        Cosine similarity (0.0 - 1.0)
    """
    if len(vec1) != len(vec2):
        raise ValueError("Векторы должны иметь одинаковую размерность")

    dot_product = sum(a * b for a, b in zip(vec1, vec2))
    norm1 = sum(a * a for a in vec1) ** 0.5
    norm2 = sum(b * b for b in vec2) ** 0.5

    if norm1 == 0 or norm2 == 0:
        return 0.0

    return dot_product / (norm1 * norm2)


def normalize_vector(vec: List[float]) -> List[float]:
    """
    Нормализовать вектор (L2 норма).

    Args:
        vec: Вектор

    Returns:
        Нормализованный вектор
    """
    norm = sum(x * x for x in vec) ** 0.5
    if norm == 0:
        return vec
    return [x / norm for x in vec]
