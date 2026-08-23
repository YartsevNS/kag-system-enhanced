# Граф знаний: архитектура максимальной точности (исследование 2026-08-23)

> Синтез лучших практик: GraphRAG (Microsoft), CORE-KG, LINK-KG, KGGen, Deg-Rag,
> nodecanon, Neo4j LLMGraphTransformer + наш стек (Neo4j Community, Qdrant, DeepSeek,
> будущий кластер мелких моделей для embedding/извлечения).
> Цель: максимальная точность извлечения сущностей и связей из русских нормативных документов.

## Статус реализации (2026-08-23, коммит 00a0528)

- [x] thinking disabled для deepseek (reasoning-баг, 4dcdc4d)
- [x] адаптивный граф: ВСЕ чанки + параллельные LLM-вызовы (Semaphore 3)
- [x] двуэтапное извлечение (entities → relations, KGGen) + description
- [x] entity resolution: lexical + embedding (0.95) + подстрока + aliases
- [x] версионирование стандартов (ISO: -N = часть, :YYYY = версия; ГОСТ: год после тире)
- [x] схема Neo4j: description, aliases
- [ ] LLM-верификация сомнительных пар (кореференция «ЦБ»=«Банк России»)
- [ ] переиндексация всех 158 документов

Результат на gost-r-34.pdf: 190 сущностей (legal_term 153, document_ref 16,
date 8, organization 6, person 4, location 3), 72/72 чанков в графе,
entity resolution слил 44 дубля.

## Часть 1. Почему наш текущий граф НЕ точен (диагноз)

| Проблема | Причина | Цена |
|---|---|---|
| 158/159 документов без сущностей | deepseek-v4-flash — reasoning, съедал max_tokens | граф пуст |
| Покрытие 6% текста | chunks[:10] при среднем 161 чанке | 94% сущностей теряется |
| Дубликаты узлов | LLM извлекает surface forms: «Банк России»/«ЦБ»/«регулятор»/«Банк РФ» | 4 узла вместо 1, связи рвутся |
| Шум | нет guided extraction, нет кореференции | мусорные узлы |
| Нет связи между документами | каждая сущность локальна для чанка | граф-«лес», не сеть |

Исследования 2025-2026: у типичного LLM-графа **34% узлов — дубли** (10K финансовый корпус),
**847 сущностей реально 312** (Duk Lee). Entity resolution уменьшает граф на ~40% и
**улучшает QA по всем метрикам**.

## Часть 2. Что говорят лучшие практики

### 2.1. GraphRAG (Microsoft, arXiv 2404.16130)
- TextUnit = **1200 токенов** (чанк для извлечения, крупнее embedding-чанка)
- Извлечение: LLM на каждый text unit → entities (name, type, description) + relations
- **Leiden community detection** → иерархия сообществ → community summaries → глобальные вопросы
- Local search (сущности+чанки) + Global search (сообщества)
- Двухпроходность: extraction + gleaning (повторный проход для пропущенного)

### 2.2. CORE-KG / LINK-KG (юридические тексты! arXiv 2506.21607)
- **Type-aware coreference resolution** — отдельный промпт на каждый тип сущности:
  «A.Y.» = «the defendant» = «A.Y. Petrov»
- Снижает дубли узлов на **33%**, шум на **38%** vs GraphRAG baseline
- Structured domain prompts: типы, роли, фильтрация legal boilerplate
- LINK-KG: type-specific Prompt Cache для отслеживания ссылок через чанки

### 2.3. Entity Resolution (5 сигналов, Duk Lee / modernData101)
1. **Lexical** — нормализация, edit distance (30% дублей)
2. **Embedding similarity** — cosine >0.88-0.92 (ещё 20-30%)
3. **Attribute matching** — одинаковые свойства (тип, дата, реквизиты)
4. **Graph topology** — общие соседи = тот же узел (самый мощный, все пропускают)
5. **Blocking → scoring → clustering** — три слоя resolution

### 2.4. KGGen (arXiv 2502.09956)
- Двухэтапное извлечение: **сначала entities, потом relations** (а не всё сразу)
- Консистентность: связи строятся только между уже найденными сущностями

### 2.5. Deg-Rag (arXiv 2510.14271)
- Entity resolution + **triple reflection** (LLM фильтрует ошибочные связи)

### 2.6. nodecanon
- Пост-обработка без LLM: merge по строке + эмбеддингу + топологии графа

### 2.7. Чанкинг (Firecrawl 2026, NVIDIA, Chroma)
- RecursiveCharacter 400-512 токенов + 10-20% overlap — лучший дефолт (у нас 500/15 ✓)
- Page-level выигрывает для юридических (0.648 accuracy)
- **Semantic chunking +9% recall** — будущий кластер позволяет

## Часть 3. Наш дизайн: «KAG-Graph Precision Pipeline»

### 3.1. Принцип: 3 стадии извлечения (не 1)

```
Документ → [A] Кореференция (очистка ссылок)
         → [B] Guided extraction (сущности, потом связи)
         → [C] Entity Resolution (дедупликация, канонизация)
         → Neo4j
```

### 3.2. Стадия A — Type-aware coreference (по CORE-KG)

Для нормативных документов ключевые alias-группы:
- Организации: «Банк России» = «ЦБ» = «ЦБ РФ» = «регулятор» (в контексте банков)
- Документы: «Указание № 5342-У» = «настоящее указание» = «документ»
- Должности: «Председатель Банка России» = «Председатель»
- Даты: «10.06.2024» = «10 июня 2024 г.» = «дата подписания»

Реализация: отдельный компактный LLM-проход по документу (или по группам чанков),
который строит «словарь канонических имён» → передаётся в стадию B как контекст.
**На кластере: мелкая модель (7-14B) делает это параллельно по секциям.**

### 3.3. Стадия B — Guided extraction (по KGGen + GraphRAG)

- Двухэтапно: (1) entities → (2) relations (не одним промптом!)
- Schema-constrained: типы из нашей онтологии (person/organization/date/money/...)
- **description для каждой сущности** — нужен для resolution и community detection
- Покрытие: **все чанки документа**, не 10 (кластер делает параллельно)
- Chunk для извлечения: объединять 2-3 embedding-чанка в один text unit (~1200 токенов)

### 3.4. Стадия C — Entity Resolution (по 5 сигналам)

Порядок (дешёвое → дорогое):
1. **Lexical blocking**: нормализация (нижний регистр, «Банк россии»), exact, edit distance < 2
2. **Embedding**: cosine по эмбеддингам имён (у нас уже есть embedding-инфраструктура!)
   - порог 0.90 для русского
3. **Graph topology**: если у двух узлов ≥2 общих соседей — merge
4. **LLM-verification** (только для сомнительных пар): «Это одна организация?»
5. **Clustering**: union-find → канонический узел, остальные — alias (свойство `aliases: []`)

Результат: `(:Entity {name: "Банк России", aliases: ["ЦБ","ЦБ РФ","Банк РФ"], canonical: true})`

### 3.5. Community detection (GraphRAG-стиль, для глобальных вопросов)

- Leiden-кластеризация на кластере (или NetworkX локально)
- Community summaries для ответов «о чём в целом говорят документы»
- В Neo4j Community: Leiden доступен в библиотеках (python-louvain/igraph), не в GDS

### 3.6. Параллельность на будущем кластере

```
N документов × M чанков → пул воркеров (vLLM/Ollama, 2-4 одинаковые модели)
  - sharding по чанкам: чанк i → модель i % num_workers
  - каждая модель: кореференция + извлечение для своей порции
  - результат: raw triples → стадия C (resolution) централизованно
```

- Embedding: локальная модель (MiniLM-RU / e5-mistral / bge-m3) на кластере — бесплатно, быстро
- Extraction: Qwen2.5-14B / Qwen3-8B (сильны в русском и JSON) — замена deepseek для graph
- **Параллельный запуск нескольких одинаковых моделей = sharding, не дублирование**:
  каждая обрабатывает свою долю чанков

### 3.7. Инкрементальность

- При добавлении документа: resolution-стадия проверяет НОВЫЕ сущности против СУЩЕСТВУЮЩИХ
  в Neo4j (не перестраивает всё)
- Streaming resolution (по modernData101): evaluate incoming → merge → update canonicals
- Lineage: `source_docs` на каждой сущности (у нас уже есть!)

## Часть 4. Что делаем СЕЙЧАС (без кластера, на DeepSeek)

1. **Покрытие: адаптивный граф** — не 10, а все чанки (параллельно asyncio.gather, лимит 3-4).
   Сейчас 25 сек на 10 чанков → 161 чанк ~6-7 мин с параллельностью. Для переиндексации приемлемо.
2. **Двухэтапное извлечение** (entities → relations) — меньше битых связей.
3. **Entity resolution после индексации**: скрипт по Neo4j (lexical + embedding по именам + топология).
4. **description в сущностях** — для resolution и будущих communities.
5. **Кореференция** — лёгкий вариант: в промпте извлечения добавить «используй канонические имена из списка» + пост-проход alias-групп.

## Часть 5. Показатели успеха

- % документов с ≥1 сущностью: 6% → 95%+
- Дубли узлов: снижение на 30-40% (меряется: count(Entity) vs уникальных canonical)
- Покрытие документа: 6% → 100% чанков
- QA: вопросы «какие организации в ГОСТе X», «какие документы регулируют Y» — полные ответы
- Скорость: параллельно на кластере — 100 документов/час вместо 4 ч/100

## Источники

- Microsoft GraphRAG: arxiv.org/abs/2404.16130; microsoft.github.io/graphrag
- CORE-KG: arxiv.org/abs/2506.21607 (юридические тексты, coreference)
- LINK-KG: arxiv.org/abs/2510.26486
- KGGen: arxiv.org/abs/2502.09956
- Deg-Rag: arxiv.org/abs/2510.14271
- nodecanon: github.com/rasinmuhammed/node-canon
- Duk Lee «GraphRAG Is Not a Database Feature — It's an Entity Resolution Problem»
- modernData101 «Entity Resolution at Scale»
- Firecrawl «Best Chunking Strategies 2026», NVIDIA chunking benchmarks
- openspg/kag сравнение: docs/COMPARISON_openspg_kag.md
