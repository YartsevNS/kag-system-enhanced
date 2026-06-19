# ЗАДАЧА: Модернизация KAG-системы (kag-system-enhanced)

Ты — senior Python-разработчик, специализирующийся на RAG/KAG-системах. Твоя задача — интегрировать современные инструменты в существующий проект.

## КОНТЕКСТ ПРОЕКТА

Проект: https://github.com/YartsevNS/kag-system-enhanced
Стек: Python 3.11+, FastAPI, Celery, Neo4j, Qdrant, Ollama, Docker
Назначение: Интеллектуальный анализ гос.документов (ГОСТы, приказы ФСТЭК, pravo.gov.ru, fstec.ru)
Особенности: работа с русскоязычным контентом, государственными порталами, ГОСТ-таблицами, PDF-сканами.

## ЦЕЛЬ МОДЕРНИЗАЦИИ

Заменить базовые компоненты на передовые инструменты для повышения качества:
1. Краулинга (обход JS, парсинг таблиц, сбор URL)
2. Извлечения контента (чистый Markdown, структурированные данные)
3. Чанкинга (семантическое разбиение вместо рекурсивного)
4. Реранкинга (улучшение релевантности через конфигурируемый бэкенд: vLLM / Ollama / локальный FlashRank)
5. Графового поиска (LightRAG для dual-level retrieval: local + high-level)
6. OCR (обработка сканированных PDF через существующий Occular OCR)

---

## АРХИТЕКТУРА ИНТЕГРАЦИИ (5 СЛОЕВ)

### СЛОЙ 1: DISCOVERY (Сбор URL)

**Инструменты:** Crawlee (Python) + feedparser

**Задача:** Заменить текущий `web_monitor` на интеллектуальный сборщик URL.

**Требования:**
1. Создать новый модуль `app/services/discovery_service/`
2. Использовать Crawlee с PlaywrightCrawler для обхода JS-редиректов на гос.сайтах
3. Реализовать парсинг sitemap.xml для pravo.gov.ru, fstec.ru
4. Добавить feedparser для RSS-лент (если доступны)
5. Сохранять найденные URL в Neo4j с метаданными: `last_checked`, `status_code`, `content_type`, `discovered_at`
6. Реализовать дедупликацию URL через Neo4j
7. Интегрировать с Celery для периодического запуска

**Пример кода (Crawlee):**

    from crawlee.crawlers import PlaywrightCrawler, PlaywrightCrawlingContext

    async def discover_urls(start_urls: list[str]):
        crawler = PlaywrightCrawler(
            max_requests_per_crawl=100,
            request_handler=handle_request
        )
        await crawler.run(start_urls)

    async def handle_request(context: PlaywrightCrawlingContext):
        url = context.request.url
        await save_url_to_neo4j(url, status=200)
        links = await context.page.eval_on_selector_all(
            'a[href]',
            'elements => elements.map(e => e.href)'
        )
        for link in links:
            await context.add_requests([link])

**Пример кода (feedparser):**

    import feedparser

    async def discover_from_rss(feed_urls: list[str]) -> list[str]:
        urls = []
        for feed_url in feed_urls:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries:
                urls.append(entry.link)
        return urls

---

### СЛОЙ 2: EXTRACTION (Извлечение контента)

**Инструменты:** Trafilatura + Crawl4AI + Occular OCR (fallback для PDF)

**Задача:** Заменить `aiohttp + BeautifulSoup` на умные парсеры.

**Требования:**
1. Создать модуль `app/services/extraction_service/`
2. Реализовать стратегию извлечения:
   - **Шаг 1:** Trafilatura для статического HTML (быстро, чистый Markdown)
   - **Шаг 2:** Если Trafilatura вернул < 100 символов → Crawl4AI (рендерит JS)
   - **Шаг 3:** Для таблиц ГОСТ → Crawl4AI с JsonExtractionStrategy
3. Для PDF: PyMuPDF → если текст пустой → Occular OCR (существующий OCR проекта, https://github.com/Bodhi42/Occular-ocr)
4. Возвращать результат в формате Markdown для последующего чанкинга
5. Сохранять метаданные: `url`, `title`, `extracted_at`, `extraction_method`

**Пример кода (гибридный парсинг HTML):**

    import trafilatura
    from crawl4ai import AsyncWebCrawler, JsonExtractionStrategy

    async def extract_content(url: str) -> dict:
        html = await fetch_html(url)
        text = trafilatura.extract(html, output_format='markdown')
        
        if len(text or "") < 100:
            async with AsyncWebCrawler() as crawler:
                result = await crawler.arun(url=url)
                text = result.markdown
        
        return {"url": url, "content": text, "format": "markdown"}

**Пример кода (ГОСТ-таблицы через Crawl4AI):**

    async def extract_gost_table(url: str) -> dict:
        async with AsyncWebCrawler() as crawler:
            result = await crawler.arun(
                url=url,
                extraction_strategy=JsonExtractionStrategy(
                    schema={
                        "doc_number": "string",
                        "doc_title": "string",
                        "requirements": ["string"]
                    }
                )
            )
            return result.extracted_content

**Пример кода (PDF с fallback на Occular OCR):**

    import fitz  # PyMuPDF

    async def extract_pdf_text(pdf_path: str) -> str:
        doc = fitz.open(pdf_path)
        text = ""
        for page in doc:
            text += page.get_text()
        
        if len(text.strip()) > 100:
            return text
        
        # Fallback: Occular OCR (существующий OCR проекта)
        # Проект: https://github.com/Bodhi42/Occular-ocr
        text = await occular_ocr_process(pdf_path)
        return text

---

### СЛОЙ 3: PROCESSING (Чанкинг)

**Инструмент:** Chonkie

**Задача:** Заменить `RecursiveCharacterTextSplitter` на семантический чанкинг.

**Требования:**
1. Установить `chonkie` (pip install chonkie)
2. Использовать `SemanticChunker` с русской моделью: `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`
3. Настроить параметры:
   - `threshold=0.5` (порог схожести)
   - `chunk_size=512` (токенов)
   - `min_chunk_size=64`
4. Сохранять метаданные чанков: `start_idx`, `token_count`, `doc_id`, `chunk_id`
5. Реализовать кэширование в Redis для повторных документов
6. Интегрировать с Celery-задачами обработки документов

**Пример кода:**

    from chonkie import SemanticChunker
    from sentence_transformers import SentenceTransformer

    def chunk_documents(texts: list[str]) -> list[dict]:
        model = SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2')
        
        chunker = SemanticChunker(
            embedding_model=model,
            threshold=0.5,
            chunk_size=512,
            min_chunk_size=64
        )
        
        chunks = []
        for text in texts:
            doc_chunks = chunker.chunk(text)
            chunks.extend([
                {
                    "text": c.text,
                    "start_idx": c.start_idx,
                    "token_count": c.token_count
                }
                for c in doc_chunks
            ])
        
        return chunks

---

### СЛОЙ 4: INDEXING (Индексация в граф и векторное хранилище)

**Инструменты:** Neo4j + Qdrant + LightRAG (https://github.com/HKUDS/LightRAG)

**Задача:** Улучшить индексацию с учётом графовых связей через LightRAG и гибридный поиск в Qdrant.

**Почему LightRAG, а не Microsoft GraphRAG:**
- В 5-8 раз быстрее индексация
- В 3 раза меньше расход LLM-токенов
- Поддерживает инкрементальное обновление графа (критично для web_monitor)
- Dual-level retrieval (low-level + high-level) из коробки
- Работает с локальными LLM (Ollama) без внешних API

**Требования:**
1. Установить `lightrag-hku` (pip install lightrag-hku)
2. Инициализировать LightRAG с локальной Ollama-моделью и embeddings
3. Для каждого чанка выполнить:
   - Извлечение сущностей (ГОСТ, организация, дата, норматив) через LLM
   - Построение связей между сущностями
   - Сохранение в граф LightRAG
4. Настроить dual-level indexing:
   - **Low-level:** точные сущности (конкретный ГОСТ, приказ)
   - **High-level:** тематические кластеры ("требования к защите ПДн")
5. Реализовать инкрементальное обновление: при добавлении нового документа обновлять только новые сущности
6. Векторизовать чанки в Qdrant с метаданными: `doc_id`, `chunk_id`, `source_url`
7. Настроить гибридный поиск в Qdrant:
   - **Dense vectors** (семантический поиск) — через BGE-M3 или nomic-embed-text
   - **Sparse vectors** (BM25-поиск) — через FastEmbed BM25 encoder или Qdrant's built-in sparse index
   - Использовать Qdrant Prefetch API для объединения обоих поисков
8. Сохранять связь чанков в Neo4j: `(Chunk)-[:NEXT]->(Chunk)` для контекста

**Пример кода (инициализация LightRAG):**

    from lightrag import LightRAG, QueryParam
    from lightrag.llm.ollama import ollama_model_complete, ollama_embed
    from lightrag.kg.shared_storage import initialize_pipeline_status

    async def init_lightrag() -> LightRAG:
        rag = LightRAG(
            working_dir="./lightrag_data",
            llm_model_func=ollama_model_complete,
            llm_model_name="llama3.1:8b",
            embedding_func=ollama_embed,
            embedding_model="nomic-embed-text",
            chunk_token_size=512,
            addon_params={
                "language": "russian",
                "entity_types": ["ГОСТ", "Приказ", "Организация", "Дата", "Требование"],
            }
        )
        await initialize_pipeline_status()
        return rag

**Пример кода (инкрементальная индексация):**

    async def index_document(rag: LightRAG, text: str, doc_id: str, metadata: dict):
        await rag.ainsert(
            text,
            ids=[doc_id],
            metadata=metadata
        )

**Пример кода (dual-level retrieval):**

    from lightrag import QueryParam

    async def lightrag_query(rag: LightRAG, query: str, mode: str = "hybrid") -> list[dict]:
        result = await rag.aquery(
            query,
            param=QueryParam(mode=mode, language="russian")
        )
        return parse_lightrag_response(result)

**Пример кода (гибридный поиск в Qdrant — dense + sparse):**

    from qdrant_client import QdrantClient, models
    from fastembed import SparseTextEmbed, TextEmbed

    class QdrantHybridSearcher:
        def __init__(self, qdrant_url: str, collection_name: str):
            self.client = QdrantClient(url=qdrant_url)
            self.collection_name = collection_name
            # Dense embeddings (семантический поиск)
            self.dense_model = TextEmbed(model_name="BAAI/bge-m3")
            # Sparse embeddings (BM25-поиск)
            self.sparse_model = SparseTextEmbed(model_name="Qdrant/bm25")

        async def index_chunk(self, chunk_id: str, text: str, metadata: dict):
            dense_vector = list(self.dense_model.passages([text]))[0]
            sparse_vector = list(self.sparse_model.passages([text]))[0]
            
            self.client.upsert(
                collection_name=self.collection_name,
                points=[
                    models.PointStruct(
                        id=chunk_id,
                        vector={
                            "dense": dense_vector,
                            "sparse": {
                                "indices": sparse_vector.indices,
                                "values": sparse_vector.values
                            }
                        },
                        payload=metadata
                    )
                ]
            )

        async def hybrid_search(self, query: str, top_k: int = 50) -> list[dict]:
            dense_query = list(self.dense_model.query_embed([query]))[0]
            sparse_query = list(self.sparse_model.query_embed([query]))[0]
            
            # Qdrant Prefetch API: объединяет dense + sparse в одном запросе
            results = self.client.query_points(
                collection_name=self.collection_name,
                prefetch=[
                    models.Prefetch(
                        query=dense_query,
                        using="dense",
                        limit=top_k * 2
                    ),
                    models.Prefetch(
                        query=models.SparseVector(
                            indices=sparse_query.indices,
                            values=sparse_query.values
                        ),
                        using="sparse",
                        limit=top_k * 2
                    )
                ],
                query=models.FusionQuery(fusion=models.Fusion.RRF),  # Reciprocal Rank Fusion
                limit=top_k
            )
            return [
                {"id": p.id, "text": p.payload.get("text"), "score": p.score, **p.payload}
                for p in results.points
            ]

---

### СЛОЙ 5: RETRIEVAL & RERANKING (Поиск и реранкинг)

**Инструменты:** Конфигурируемый реранкер (vLLM / Ollama / локальный FlashRank) + LightRAG hybrid retrieval + Qdrant hybrid search

**Задача:** Улучшить релевантность выдачи через гибкий реранкер с выбором бэкенда через админ-панель.

#### 5.1. Архитектура реранкера (абстракция бэкенда)

Реализовать интерфейс `RerankerBackend` с тремя реализациями. Выбор бэкенда — через админ-страницу (по аналогии с настройками embedding). Активный бэкенд и его параметры сохраняются в БД (таблица `settings` или `system_config`).

**Требования к абстракции:**
1. Создать модуль `app/services/reranker/`
2. Определить интерфейс `RerankerBackend` с методом `async def rerank(query, passages) -> list[dict]`
3. Реализовать три бэкенда:
   - **`LocalRerankerBackend`** — FlashRank + `BAAI/bge-reranker-v2-m3` (работает в процессе приложения)
   - **`OllamaRerankerBackend`** — внешний Ollama-сервер с cross-encoder моделью
   - **`VLLMRerankerBackend`** — внешний vLLM-сервер с OpenAI-совместимым API
4. Создать `RerankerFactory.get_backend()` — читает активный бэкенд из БД и возвращает нужный класс
5. Кэшировать инстанс бэкенда в памяти (singleton) до смены настроек
6. При смене бэкенда через админку — инвалидировать кэш и пересоздать инстанс

**Пример кода (интерфейс и фабрика):**

    from typing import Protocol
    from enum import Enum

    class RerankerBackendType(str, Enum):
        LOCAL = "local"
        OLLAMA = "ollama"
        VLLM = "vllm"

    class RerankerBackend(Protocol):
        async def rerank(self, query: str, passages: list[dict]) -> list[dict]:
            ...

    class RerankerFactory:
        def __init__(self, settings_service):
            self.settings = settings_service
            self._cache: dict[str, RerankerBackend] = {}

        async def get_backend(self) -> RerankerBackend:
            config = await self.settings.get_reranker_config()
            backend_type = config["backend_type"]
            
            if backend_type not in self._cache:
                self._cache[backend_type] = self._create_backend(config)
            return self._cache[backend_type]

        def _create_backend(self, config: dict) -> RerankerBackend:
            if config["backend_type"] == RerankerBackendType.LOCAL:
                return LocalRerankerBackend(model_name=config["model_name"])
            elif config["backend_type"] == RerankerBackendType.OLLAMA:
                return OllamaRerankerBackend(
                    base_url=config["base_url"],
                    model_name=config["model_name"]
                )
            elif config["backend_type"] == RerankerBackendType.VLLM:
                return VLLMRerankerBackend(
                    base_url=config["base_url"],
                    model_name=config["model_name"],
                    api_key=config.get("api_key")
                )

        def invalidate_cache(self):
            self._cache.clear()

**Пример кода (LocalRerankerBackend — FlashRank):**

    from flashrank import Ranker, RerankRequest

    class LocalRerankerBackend:
        def __init__(self, model_name: str = "BAAI/bge-reranker-v2-m3"):
            self.ranker = Ranker(model_name=model_name)

        async def rerank(self, query: str, passages: list[dict]) -> list[dict]:
            request = RerankRequest(
                query=query,
                passages=[{"text": p["text"], "id": p["id"]} for p in passages]
            )
            return self.ranker.rerank(request)

**Пример кода (OllamaRerankerBackend):**

    import httpx

    class OllamaRerankerBackend:
        def __init__(self, base_url: str, model_name: str):
            self.base_url = base_url.rstrip("/")
            self.model_name = model_name

        async def rerank(self, query: str, passages: list[dict]) -> list[dict]:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    f"{self.base_url}/api/embed",
                    json={
                        "model": self.model_name,
                        "input": [[query, p["text"]] for p in passages]
                    }
                )
                response.raise_for_status()
                data = response.json()
                
                scored = [
                    {"id": p["id"], "text": p["text"], "score": score}
                    for p, score in zip(passages, data["embeddings"])
                ]
                return sorted(scored, key=lambda x: x["score"], reverse=True)

**Пример кода (VLLMRerankerBackend):**

    import httpx

    class VLLMRerankerBackend:
        def __init__(self, base_url: str, model_name: str, api_key: str | None = None):
            self.base_url = base_url.rstrip("/")
            self.model_name = model_name
            self.api_key = api_key

        async def rerank(self, query: str, passages: list[dict]) -> list[dict]:
            headers = {}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"

            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    f"{self.base_url}/v1/score",
                    headers=headers,
                    json={
                        "model": self.model_name,
                        "text_1": query,
                        "text_2": [p["text"] for p in passages]
                    }
                )
                response.raise_for_status()
                data = response.json()
                
                scored = [
                    {"id": p["id"], "text": p["text"], "score": score}
                    for p, score in zip(passages, data["data"])
                ]
                return sorted(scored, key=lambda x: x["score"], reverse=True)

#### 5.2. Админ-страница для управления реранкером

**Требования к UI (по аналогии с настройками embedding):**
1. Создать страницу `/admin/settings/reranker` в существующем админ-интерфейсе
2. Форма должна содержать:
   - **Селект бэкенда:** `local` / `ollama` / `vllm`
   - **Поле `base_url`** (активно для ollama и vllm, например: `http://ollama:11434` или `http://vllm:8000`)
   - **Поле `model_name`** (например: `BAAI/bge-reranker-v2-m3`)
   - **Поле `api_key`** (опционально, только для vllm)
   - **Кнопка "Проверить соединение"** — делает тестовый запрос к бэкенду и показывает статус
   - **Кнопка "Сохранить"** — сохраняет настройки в БД и инвалидирует кэш фабрики
3. При смене бэкенда — показать предупреждение о возможной недоступности сервиса
4. Отображать текущий активный бэкенд и его статус (online/offline)

**Пример кода (API эндпоинты для админки):**

    from fastapi import APIRouter, Depends
    from pydantic import BaseModel

    router = APIRouter(prefix="/admin/settings/reranker", tags=["admin"])

    class RerankerSettingsUpdate(BaseModel):
        backend_type: RerankerBackendType
        base_url: str | None = None
        model_name: str
        api_key: str | None = None

    @router.get("/")
    async def get_reranker_settings(settings_service=Depends(get_settings_service)):
        return await settings_service.get_reranker_config()

    @router.put("/")
    async def update_reranker_settings(
        payload: RerankerSettingsUpdate,
        settings_service=Depends(get_settings_service),
        reranker_factory=Depends(get_reranker_factory)
    ):
        await settings_service.update_reranker_config(payload.dict())
        reranker_factory.invalidate_cache()
        return {"status": "ok"}

    @router.post("/test-connection")
    async def test_connection(payload: RerankerSettingsUpdate):
        backend = create_temp_backend(payload)
        try:
            result = await backend.rerank(
                query="тест",
                passages=[{"id": "1", "text": "тестовый passage"}]
            )
            return {"status": "ok", "latency_ms": result.get("latency_ms")}
        except Exception as e:
            return {"status": "error", "error": str(e)}

#### 5.3. Финальный пайплайн поиска

**Требования:**
1. Реализовать пайплайн:
   - **Шаг 1:** Qdrant Hybrid Search (dense vectors + sparse vectors через Prefetch API с RRF fusion) → топ-50 кандидатов
   - **Шаг 2:** LightRAG hybrid retrieval (local + high-level) → топ-20 графовых результатов
   - **Шаг 3:** Объединение результатов (union + deduplication) → топ-40
   - **Шаг 4:** Реранкинг через активный бэкенд (из админки) → топ-10
   - **Шаг 5:** Финальная фильтрация → топ-5
2. Кэшировать результаты реранкинга в Redis (ключ = hash(query + backend_type + model_name))
3. При смене бэкенда в админке — инвалидировать кэш реранкинга
4. Логировать используемый бэкенд и latency каждого запроса (для метрик Prometheus)

**Пример кода (финальный пайплайн):**

    async def retrieve_and_rerank(
        rag: LightRAG,
        qdrant_searcher: QdrantHybridSearcher,
        reranker_factory: RerankerFactory,
        query: str,
        top_k: int = 5
    ) -> list[dict]:
        # Шаг 1: Qdrant Hybrid Search (dense + sparse через Prefetch API)
        qdrant_candidates = await qdrant_searcher.hybrid_search(query, top_k=50)
        
        # Шаг 2: LightRAG dual-level retrieval
        lightrag_results = await lightrag_query(rag=rag, query=query, mode="hybrid")
        
        # Шаг 3: Объединение результатов (union + deduplication)
        merged = merge_and_deduplicate(qdrant_candidates, lightrag_results)[:40]
        
        # Шаг 4: Реранкинг через активный бэкенд (из админки)
        backend = await reranker_factory.get_backend()
        reranked = await backend.rerank(query=query, passages=merged)
        
        # Шаг 5: Финальный топ-K
        return reranked[:top_k]

---

## ТРЕБОВАНИЯ К РЕАЛИЗАЦИИ

### Зависимости (обновить requirements.txt)

    crawlee[playwright]
    crawl4ai
    chonkie
    flashrank
    feedparser
    trafilatura
    lightrag-hku
    httpx
    qdrant-client
    fastembed

### Docker-конфигурация
- Добавить контейнер для Playwright (для Crawlee)
- Увеличить память для контейнеров с Chonkie (требует RAM для модели)
- Пробросить volume для кэша моделей (HuggingFace): `~/.cache/huggingface`
- Пробросить volume для LightRAG: `./lightrag_data`
- Интегрировать существующий контейнер Occular OCR (https://github.com/Bodhi42/Occular-ocr)
- Добавить опциональный сервис vLLM в `docker-compose.yml` (profile: reranker-vllm):
  - Образ: `vllm/vllm-openai:latest`
  - Модель: `BAAI/bge-reranker-v2-m3`
  - Порт: `8000`
  - Запускать только при выборе бэкенда `vllm` в админке
- При первом запуске FlashRank автоматически загрузит модель `bge-reranker-v2-m3` (~560MB)
- LightRAG при первом запуске загрузит embedding-модель `nomic-embed-text` (~270MB)
- FastEmbed загрузит модели `BAAI/bge-m3` (dense) и `Qdrant/bm25` (sparse) при первом запуске

### Интеграция с существующим стеком
- Все новые сервисы — асинхронные (asyncio)
- Интеграция с FastAPI через dependency injection
- Celery-задачи для фоновой обработки (краулинг, чанкинг, OCR, индексация в LightRAG и Qdrant)
- Логирование через существующую систему проекта
- Метрики через Prometheus (если есть): `reranker_latency_seconds`, `reranker_backend_type`, `reranker_errors_total`, `qdrant_search_latency_seconds`
- LightRAG работает в том же процессе, что и FastAPI (или отдельный worker через Celery)
- Настройки реранкера хранятся в БД (таблица `settings` или `system_config`) по аналогии с настройками embedding
- Qdrant уже используется в проекте — расширить коллекцию для поддержки sparse vectors (гибридный поиск)

### Тестирование
- Unit-тесты для каждого слоя
- Unit-тесты для каждого бэкенда реранкера (Local, Ollama, VLLM) с моками
- Unit-тесты для QdrantHybridSearcher (dense + sparse + fusion)
- Интеграционные тесты на реальных гос.сайтах (pravo.gov.ru, fstec.ru)
- A/B сравнение качества: старая версия vs новая (метрики: Precision@5, Recall@10)
- Нагрузочное тестирование Crawlee (не более 100 запросов в минуту на один домен)
- Тестирование BGE-Reranker на русских документах (сравнение с ms-marco)
- Тестирование LightRAG: сравнение local vs global vs hybrid retrieval на типовых запросах
- Тестирование инкрементального обновления LightRAG (добавление 10 документов → проверка, что граф обновился без перестройки)
- Тестирование Qdrant hybrid search: сравнение pure dense vs dense+sparse (RRF fusion) — ожидается улучшение Recall@10 на 10-15%
- Тестирование админки: смена бэкенда → проверка, что кэш инвалидировался и новый бэкенд используется
- Тестирование fallback: если внешний бэкенд (ollama/vllm) недоступен → возврат ошибки с понятным сообщением

---

## КРИТЕРИИ ГОТОВНОСТИ

- [ ] Discovery Service находит URL на pravo.gov.ru с обходом JS
- [ ] Extraction Service парсит ГОСТ-таблицы в JSON
- [ ] Chonkie чанкирует русские документы семантически
- [ ] Реализованы три бэкенда реранкера: Local (FlashRank), Ollama, VLLM
- [ ] Админ-страница `/admin/settings/reranker` работает: смена бэкенда, проверка соединения, сохранение
- [ ] Настройки реранкера сохраняются в БД и читаются при старте приложения
- [ ] Кэш фабрики реранкера инвалидируется при смене настроек
- [ ] BGE-Reranker-v2-m3 улучшает релевантность (сравнение топ-5 до/после, ожидается +15-25% качества)
- [ ] LightRAG построен граф сущностей для гос.документов
- [ ] LightRAG dual-level retrieval работает (local + high-level)
- [ ] Инкрементальное обновление LightRAG работает (новый документ не перестраивает весь граф)
- [ ] Qdrant hybrid search работает (dense + sparse vectors через Prefetch API с RRF fusion)
- [ ] Occular OCR работает как fallback для сканированных PDF
- [ ] Все сервисы работают в Docker
- [ ] Нет регрессии (старые функции работают)
- [ ] Покрытие тестами ≥ 70%

---

## ОГРАНИЧЕНИЯ И ЗАМЕЧАНИЯ

1. **Crawl4AI асинхронный** — интегрируй с FastAPI/Celery через `asyncio`.
2. **Chonkie может быть медленным** — кэшируй чанки в Redis.
3. **Реранкер — конфигурируемый.** Пользователь через админку выбирает, где лежит реранкер: локально (FlashRank), на внешнем Ollama-сервере или на внешнем vLLM-сервере. По аналогии с настройками embedding.
4. **Local бэкенд (FlashRank)** — модель `bge-reranker-v2-m3` загружается автоматически при первом запуске (~560MB). Работает быстро на CPU.
5. **Ollama бэкенд** — требует, чтобы на Ollama-сервере была загружена cross-encoder модель. Ollama должен быть доступен по сети из контейнера приложения.
6. **VLLM бэкенд** — требует отдельного контейнера/сервера с vLLM и моделью `BAAI/bge-reranker-v2-m3`. vLLM должен быть запущен с OpenAI-совместимым API.
7. **LightRAG** — использует локальную Ollama для извлечения сущностей. Первая индексация 1000 документов займёт 1-2 часа на GPU или 4-6 часов на CPU. Инкрементальные обновления — минуты.
8. **LightRAG vs Microsoft GraphRAG** — выбран LightRAG, так как он в 5-8 раз быстрее, в 3 раза экономнее по токенам и поддерживает инкрементальные обновления (критично для web_monitor).
9. **Qdrant hybrid search** — использовать Qdrant Prefetch API с RRF fusion для объединения dense (BGE-M3) и sparse (BM25) векторов. Это даёт улучшение Recall@10 на 10-15% по сравнению с pure dense search.
10. **Локальные модели** — все модели (FlashRank с BGE, Chonkie, LightRAG embeddings, Qdrant dense+sparse) должны работать локально без внешних API.
11. **Occular OCR** (https://github.com/Bodhi42/Occular-ocr) — использовать как fallback для PDF, когда PyMuPDF не может извлечь текст. Это существующий компонент проекта.
12. **Гос.сайты** — соблюдать robots.txt, не более 100 запросов в минуту на домен.
13. **Русский язык** — все модели должны поддерживать русский (BGE-Reranker-v2-m3, Chonkie с мультиязычной моделью, BGE-M3 для Qdrant, LightRAG с параметром language="russian" — проверено).
14. **Ресурсы LightRAG** — на 1000 документов: ~1-3 GB диск, 4-8 GB RAM для индексации, 2-4 GB RAM для поиска.
15. **Fallback при недоступности внешнего бэкенда** — если Ollama или vLLM недоступны, API должно возвращать понятную ошибку с рекомендацией переключить бэкенд в админке на `local`.
16. **Qdrant vs ChromaDB** — в проекте используется Qdrant (не ChromaDB). Qdrant поддерживает гибридный поиск (dense + sparse vectors) нативно через Prefetch API, что лучше для RAG.

---

## РЕСУРСЫ

- Crawlee Docs: https://crawlee.dev/python/
- Crawl4AI: https://github.com/unclecode/crawl4ai
- Chonkie: https://github.com/bhavnicksm/chonkie
- FlashRank: https://github.com/PrithivirajDamodaran/FlashRank
- BGE-Reranker-v2-m3: https://huggingface.co/BAAI/bge-reranker-v2-m3
- LightRAG: https://github.com/HKUDS/LightRAG
- LightRAG Docs: https://lightrag-hku.github.io/
- Trafilatura: https://github.com/adbar/trafilatura
- feedparser: https://feedparser.readthedocs.io/
- Occular OCR: https://github.com/Bodhi42/Occular-ocr
- vLLM Docs: https://docs.vllm.ai/
- Ollama API: https://github.com/ollama/ollama/blob/main/docs/api.md
- Qdrant Hybrid Search: https://qdrant.tech/documentation/concepts/hybrid-queries/
- FastEmbed (BM25 + dense): https://qdrant.github.io/fastembed/

Начни с обновления `requirements.txt` и создания структуры новых сервисов. Если что-то непонятно — спрашивай.