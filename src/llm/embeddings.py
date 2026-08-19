"""
Embedding клиент для KAG.

Поддерживает два типа провайдеров:
- ollama (локальный): POST /api/embeddings (одиночный) и /api/embed (батч)
- OpenAI-совместимые (openai/deepseek/openrouter/custom): POST /v1/embeddings (input=[...])

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
    Клиент для генерации эмбеддингов.

    Поддерживает Ollama (/api/embed) и OpenAI-совместимые провайдеры
    (/v1/embeddings). Тип провайдера задаётся через provider_type.
    """

    def __init__(
        self,
        base_url: str = "http://192.168.50.41:11434",
        model: str = "nomic-embed-text:latest",
        timeout: float = 60.0,
        max_retries: int = 3,
        retry_delay: float = 1.0,
        provider_type: str = "ollama",
        api_key: str = ""
    ):
        """
        Инициализация embedding клиента.

        Args:
            base_url: URL провайдера (без endpoint)
            model: Модель для эмбеддингов
            timeout: Таймаут запросов
            max_retries: Максимум повторных попыток
            retry_delay: Задержка между попытками
            provider_type: "ollama" или OpenAI-совместимый (openai/deepseek/openrouter/custom)
            api_key: API-ключ для OpenAI-совместимых провайдеров
        """
        self.base_url = base_url.rstrip('/')
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.provider_type = provider_type  # "ollama" | "openai"-совместимый
        self.api_key = api_key

        self._client: Optional[httpx.AsyncClient] = None
        self._dimensions: Optional[int] = None

        logger.info(
            f"EmbeddingClient инициализирован: "
            f"base_url={self.base_url}, model={self.model}, type={self.provider_type}"
        )

    async def _get_client(self) -> httpx.AsyncClient:
        """Получить HTTP клиент (пересоздаём при смене event loop).

        Celery обрабатывает каждый документ в отдельном asyncio.run() — новый
        event loop. httpx.AsyncClient привязывается к loop при первом запросе,
        поэтому кэшированный клиент после закрытия loop даёт "Event loop is closed".
        """
        import asyncio
        current_loop = asyncio.get_running_loop()
        if (
            self._client is None
            or self._client.is_closed
            or getattr(self, "_client_loop", None) is not current_loop
        ):
            if self._client is not None and not self._client.is_closed:
                try:
                    await self._client.aclose()
                except Exception:
                    pass
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
            self._client_loop = current_loop

        return self._client

    async def close(self):
        """Закрыть HTTP клиент"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            logger.info("Embedding клиент закрыт")

    def _headers(self) -> dict:
        """Заголовки для OpenAI-совместимых провайдеров."""
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _embed_endpoint(self, batch: bool) -> str:
        """Правильный endpoint в зависимости от типа провайдера.

        Для Ollama: /api/embed (батч) или /api/embeddings (одиночный).
        Для OpenAI-совместимых: /embeddings (если base_url уже оканчивается на
        /v1) или /v1/embeddings. Это исключает двойной /v1.
        """
        if self.provider_type == "ollama":
            return "/api/embed" if batch else "/api/embeddings"
        # OpenAI-совместимый
        if self.base_url.endswith("/v1"):
            return "/embeddings"
        return "/v1/embeddings"

    def _embed_payload(self, texts: List[str]) -> dict:
        """Тело запроса в зависимости от типа провайдера."""
        if self.provider_type == "ollama":
            if len(texts) == 1:
                return {"model": self.model, "prompt": texts[0]}
            return {"model": self.model, "input": texts}
        # OpenAI-совместимый
        return {"model": self.model, "input": texts}

    def _parse_embeddings(self, data: dict, count: int) -> List[List[float]]:
        """Распарсить ответ в список векторов (по числу запрошенных текстов)."""
        if self.provider_type == "ollama":
            # /api/embeddings (одиночный) → {"embedding": [...]}
            if "embedding" in data:
                return [data["embedding"]]
            # /api/embed (батч) → {"embeddings": [[...], ...]}
            return data.get("embeddings", [])
        # OpenAI-совместимый /v1/embeddings → {"data": [{"embedding": [...]}, ...]}
        return [item.get("embedding", []) for item in data.get("data", [])]

    async def _embed_request(self, client: httpx.AsyncClient, texts: List[str]) -> List[List[float]]:
        """Один HTTP-запрос для списка текстов. Возвращает список векторов."""
        endpoint = self._embed_endpoint(batch=(len(texts) > 1))
        payload = self._embed_payload(texts)

        if self.provider_type == "ollama":
            response = await client.post(endpoint, json=payload)
        else:
            response = await client.post(endpoint, json=payload, headers=self._headers())

        if response.status_code == 404:
            raise ValueError(f"Модель не найдена: {self.model}. Проверьте имя модели у провайдера.")
        if response.status_code != 200:
            raise ValueError(
                f"Ошибка embedding API (код {response.status_code}): {response.text[:200]}"
            )

        data = response.json()
        embeddings = self._parse_embeddings(data, len(texts))
        if not embeddings:
            raise ValueError("Пустой ответ embedding API")

        if self._dimensions is None and embeddings and embeddings[0]:
            self._dimensions = len(embeddings[0])
        return embeddings

    async def generate(self, text: str) -> List[float]:
        """
        Сгенерировать embedding для одного текста.

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

                embeddings = await self._embed_request(client, [text])
                embedding = embeddings[0]

                if not embedding:
                    raise ValueError("Пустой embedding в ответе")

                logger.debug(f"Embedding сгенерирован: dimensions={len(embedding)}")
                return embedding

            except httpx.TimeoutException:
                last_error = TimeoutError(f"Таймаут embedding запроса ({self.timeout}с)")
                logger.warning(last_error)
            except httpx.ConnectError as e:
                last_error = ConnectionError(f"Ошибка подключения к embedding API: {e}")
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
        batch_size: int = 8
    ) -> List[List[float]]:
        """
        Сгенерировать embeddings для списка текстов НАСТОЯЩИМ батчем.

        Один HTTP-запрос на батч из batch_size текстов (для Ollama /api/embed
        или OpenAI /v1/embeddings). Раньше каждый текст шёл отдельным запросом
        (~1.5 сек × N чанков) — это было узким местом векторизации.

        Args:
            texts: Список текстов
            batch_size: Размер батча (текстов на один запрос)

        Returns:
            Список векторов (по одному на каждый текст)
        """
        logger.info(f"Batch embedding: {len(texts)} текстов, batch_size={batch_size}")

        embeddings: List[List[float]] = []
        total = len(texts)

        for i in range(0, total, batch_size):
            batch = texts[i:i + batch_size]
            try:
                batch_emb = await self._embed_request(client=await self._get_client(), texts=batch)
                embeddings.extend(batch_emb)
            except Exception as e:
                logger.error(f"Ошибка embedding батча {i}: {e}")
                for _ in batch:
                    embeddings.append([0.0] * (self._dimensions or 768))

        logger.info(f"Batch embedding завершён: {len(embeddings)} векторов")
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
                "chunk_id": f"chunk_{i:05d}",
                "content": chunk,
                "embedding": embedding,
                "metadata": metadata[i] if metadata and i < len(metadata) else {}
            }
            results.append(result)

        logger.info(f"Document embedding завершен: {len(results)} чанков")
        return results

    async def health_check(self) -> dict:
        """
        Проверить доступность embedding API.

        Returns:
            Словарь со статусом проверки
        """
        try:
            client = await self._get_client()

            start_time = asyncio.get_event_loop().time()

            embeddings = await self._embed_request(client, ["test"])

            response_time_ms = (asyncio.get_event_loop().time() - start_time) * 1000

            if embeddings and embeddings[0]:
                return {
                    "healthy": True,
                    "model": self.model,
                    "dimensions": len(embeddings[0]),
                    "response_time_ms": response_time_ms
                }
            return {
                "healthy": False,
                "error": "Пустой ответ embedding API"
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
