# KAG — Knowledge Augmentation Generation

> Интеллектуальная система управления знаниями: **RAG + граф знаний** для работы с документами (ГОСТы, НПА, нормативка) на русском языке.

KAG строит из загруженных документов семантическую базу (векторы в Qdrant) **и** граф знаний (Neo4j): извлекает сущности и связи, разрешает синонимы, связывает версии документов. Чат отвечает на вопросы с учётом домена запроса (legal / medical / technical / infosec / …), сложные вопросы раскладывает на подзапросы.

---

## Возможности

### 💬 Чат с AI
- RAG по документам + обогащение из графа знаний (Neo4j)
- **Query Routing**: мелкая модель (qwen2.5:1.5b) определяет домен вопроса ДО основного LLM → фильтр поиска по домену
- **Query Decomposition**: сложные вопросы («сравни А и Б») разбиваются на подзапросы, результаты объединяются
- **Table RAG**: таблицы из документов извлекаются структурно (PyMuPDF find_tables) и возвращаются в чат как HTML
- Потоковая генерация (streaming), экспорт диалогов в PDF/DOCX

### 🕸️ Граф знаний (Neo4j + DozerDB)
- Извлечение сущностей и связей LLM (KGGen-подход: entities → relations)
- **Entity Resolution**: лексика + эмбеддинги + топология + LLM-верификация + словарь алиасов
- **Словарь алиасов** (entity_aliases): модерация прямо в админке (клик по паре → inline-редактирование), автопары для промпта роутинга
- Версии документов (SUPERSEDED_BY + is_current)
- **DozerDB**: multi-database, NODE KEY constraints, **OpenGDS** (community detection gds.louvain без Enterprise-лицензии), telemetry off
- Батч-запись (UNWIND), кэш LLM-ответов, режимы извлечения (two_pass / single_pass)

### 🔍 Поиск
- **Hybrid Search**: dense (embeddings) + sparse (BM25) через RRF — включается в админке
- Reranker bge-reranker-v2-m3 (мультиязычный)
- Фильтры по домену, группам доступа, типу документа

### 📄 Документы
- PDF (текстовый слой PyMuPDF + OCR Occular для сканов), DOCX, TXT, CSV
- Таблицы → Qdrant (markdown) + document_tables (Postgres, точный поиск по ячейкам)
- Переиндексация (в т.ч. по отдельным документам), статус обработки, миниатюры

### 🌐 Web Monitor
- Мониторинг внешних источников: RSS / скрапинг (aiohttp+BS4+Trafilatura) / SPA (Playwright)
- Автоматическая загрузка документов и новостей, дедупликация по SHA-256
- Наборы источников: ФСТЭК, ФСБ, ЦБ РФ, ГОСТ Р, Securelist и др.

### ⚙️ Админка
- Провайдеры LLM: **любой OpenAI-совместимый** (DeepSeek, OpenRouter, GigaChat), Ollama, llama.cpp — типовая схема «функция → модель»
- Настройки: чанкинг, OCR, поиск (Hybrid), Neo4j (батч/таймауты), worker-ресурсы, масштабирование, внешний адрес
- **Бэкап одним ZIP**: документы + метаданные + словарь алиасов + настройки + история чатов
- Сворачиваемые секции, типы документов, доменная схема сущностей

### 🔐 Безопасность
- **Keycloak SSO** (OIDC) для всех пользователей + локальный admin fallback
- Внешний адрес настраивается в админке (продукт для сторонних сетей)
- Группы доступа к документам, телеметрия отключена (DozerDB)

---

## Технологический стек

| Компонент | Технология |
|-----------|------------|
| Backend | Python 3.11, FastAPI, Celery (2+ worker'а) |
| Граф знаний | Neo4j Community + **DozerDB** (multi-db, OpenGDS) |
| Векторная БД | Qdrant (dense 1024 + sparse BM25) |
| Реляционная БД | PostgreSQL (документы, чаты, алиасы, настройки) |
| Кэш/очереди | Redis (Celery broker/results) |
| LLM | DeepSeek / OpenRouter / GigaChat / Ollama / llama.cpp (любой OpenAI-совместимый) |
| Embeddings | GigaChat (1024 dim) или любой OpenAI-совместимый |
| Query routing | qwen2.5:1.5b (Ollama) |
| Reranker | bge-reranker-v2-m3 |
| SSO | Keycloak |
| Прокси | nginx |
| Мониторинг | Prometheus + Grafana + Loki |

---

## Развёртывание

### Требования
- Linux-сервер, Docker + docker-compose (v1), ~8+ CPU / 16+ GB RAM
- Доступ к LLM API (DeepSeek/OpenRouter) или локальным Ollama/llama.cpp

### Шаги
```bash
git clone https://github.com/YartsevNS/kag-system-enhanced.git
cd kag-system-enhanced

# 1. Создать .env (пароли генерируются при развёртывании)
cp .env.example .env   # заполнить

# 2. Запуск всех сервисов
docker-compose up -d

# 3. Первоначальная настройка
#    http://<host>:8000/setup — Setup Wizard (БД, LLM, embedding, SSH)
```

### Важно (из опыта)
- На сервере используется **docker-compose v1** (`docker-compose`, не `docker compose`)
- После пересоздания контейнеров (`up -d`) перезапускать nginx (кэш IP upstream → 502)
- Масштабирование worker: `docker-compose up -d --scale worker=2`
- Neo4j — образ `graphstack/dozerdb` (DozerDB); данные в volume `neo4j_data`
- Свежий код: scp по одному файлу + `docker restart kag-api` (и worker'ов)

---

## Архитектура

```
                    ┌──────────────┐
                    │    nginx     │  :80/:443, SSO (Keycloak)
                    └──────┬───────┘
                           ▼
                    ┌──────────────┐        ┌──────────────────┐
                    │   FastAPI    │───────▶│   Qdrant (dense  │
                    │   (api)      │        │   + sparse BM25) │
                    └──────┬───────┘        └──────────────────┘
                           │
             ┌─────────────┼──────────────┐
             ▼             ▼              ▼
      ┌────────────┐ ┌───────────┐ ┌──────────────────┐
      │ PostgreSQL │ │  Redis    │ │ Neo4j + DozerDB  │
      │ (docs,     │ │ (celery,  │ │ (граф знаний,    │
      │  чаты,     │ │  кэш)     │ │  OpenGDS)        │
      │  алиасы)   │ └───────────┘ └──────────────────┘
      └────────────┘
             │
             ▼
      ┌────────────────────────────────────────────┐
      │ Celery workers (×2): парсинг → чанкинг →    │
      │ embeddings → граф (entity extraction,       │
      │ resolution) → таблицы                        │
      └────────────────────────────────────────────┘
             │
             ▼
      ┌────────────────────────────────────────────┐
      │ LLM: DeepSeek (chat/graph) · qwen (routing) │
      │ · GigaChat (embeddings) · Ollama/llama.cpp  │
      └────────────────────────────────────────────┘
```

**Пайплайн обработки документа:** загрузка → PyMuPDF/OCR → сегменты → чанки (1000 симв., overlap 150) → embeddings (dense + sparse) → Qdrant → таблицы → DocumentRepository → граф Neo4j (сущности/связи/версии).

**Пайплайн вопроса:** маршрутизация интента → domain (query_analysis) → RAG-поиск (Qdrant, фильтр по домену, hybrid, rerank) → декомпозиция сложных вопросов → обогащение графом/таблицами → LLM (streaming).

---

## Ветки

- `main` — актуальная стабильная (переписана на stable_PyMuPDF)
- `stable` — стабильная линия
- `stable_PyMuPDF` — рабочая ветка (PyMuPDF-парсер, всё актуальное)

## Документация

- `docs/guides/` — гиды: graph-precision-architecture, embedding, chunking, chat, deploy, web-monitor
- Obsidian-база знаний проекта: `C:\VSCODE_PROJECT\obsidian` (решения, статусы, компоненты, сессии)

---

*KAG — продукт для сторонних сетей: разворачивается за reverse proxy, SSO настраивается через админку, имена LLM-провайдеров в UI не хардкодятся.*
