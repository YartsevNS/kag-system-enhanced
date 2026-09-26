# Наблюдаемость KAG: trace_id, стадии вопроса, метрики (2026-09-26)

Зачем этот документ: раньше, чтобы понять «почему ответ на вопрос был плохим», приходилось грепать
логи по времени и сопоставлять строки вручную. Метрики были объявлены в коде, но **никто их не
записывал**, и эндпоинта `/metrics` не существовало — Prometheus собирать было нечего. Теперь путь
запроса виден по одному идентификатору и в метриках по стадиям.

## 1. Trace ID: один идентификатор на весь запрос

Как это работает: `TraceMiddleware` (`src/api/trace.py`) — чистый ASGI-middleware (не
`BaseHTTPMiddleware`, чтобы не ломать стриминг ответов чата) — на входе:

1. берёт `X-Trace-ID` из запроса, если клиент его прислал, иначе рождает свой (12 hex-символов);
2. кладёт в `ContextVar` — оттуда loguru автоматически подставляет его в **каждую** строку журнала,
   включая строки, выполняемые в отдельных потоках (`asyncio.to_thread` копирует контекст);
3. возвращает тот же идентификатор в заголовке ответа `X-Trace-ID`;
4. логирует начало и итог запроса (метод, путь, статус, время в мс) и пишет метрики HTTP.

Правила идентификатора: ASCII-токен до 64 символов (`A-Za-z0-9._:-`). Кириллица, пробелы и переносы
строк заменяются своим идентификатором — заголовки HTTP кодируются latin-1 (кириллица ломает отдачу
ответа), а перенос строки в журнале — это подстановка.

Как пользоваться:

```bash
# 1) запрос с показом заголовков: в ответе будет X-Trace-ID
curl -i -X POST http://localhost:8000/api/v1/chat/ \
  -H "Authorization: Bearer <токен>" -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"какие требования к защите информации?"}],"stream":false}'

# 2) весь путь запроса по этому идентификатору
docker logs kag-api --since 10m | grep <X-Trace-ID>
```

Что видно в журнале по одному trace_id (пример пути):

```
2026-09-26 14:22:31.101 | INFO     | 8f3a91c2d4e5 | src.api.trace:__call__:104 - POST /api/v1/chat/ начат
2026-09-26 14:22:31.130 | INFO     | 8f3a91c2d4e5 | chat_service:generate_response:966 - [rag] поиск: 10 фрагментов за 210 мс (глубина 10, домен infosec)
2026-09-26 14:22:31.310 | INFO     | 8f3a91c2d4e5 | chat_service:generate_response:1152 - [rag] граф: 5 связей за 84 мс
2026-09-26 14:22:31.402 | INFO     | 8f3a91c2d4e5 | chat_service:generate_response:1196 - [tables] SQL по таблицам: статус skipped, строк 0, за 12 мс
2026-09-26 14:22:34.512 | INFO     | 8f3a91c2d4e5 | chat_service:generate_response:1418 - Ответ сгенерирован: model=deepseek-flash, tokens=4210, sources=10, elapsed=3.1s
2026-09-26 14:22:34.514 | INFO     | 8f3a91c2d4e5 | src.api.trace:__call__:119 - POST /api/v1/chat/ → 200 за 3413 мс
```

Формат журнала: `время | уровень | trace_id | модуль:функция:строка — сообщение`.

## 2. Метрики: /metrics

Эндпоинт: `GET /metrics` (формат Prometheus, без авторизации — как принято для внутреннего скрейпа;
наружу не публиковать, закрывать на уровне nginx).

Что записывается:

| Метрика | Метки | Смысл |
| :-- | :-- | :-- |
| `http_requests_total`, `http_request_duration_seconds` | method, endpoint | все HTTP-запросы (пишет TraceMiddleware) |
| `rag_stage_duration_seconds` | stage = qdrant / graph / tables / llm / access | постадийное время внутри вопроса |
| `llm_request_duration_seconds`, `llm_requests_total` | model, status | вызовы модели |
| `llm_tokens_total` | type = prompt / completion | расход токенов (контроль стоимости) |
| `qdrant_operations_total`, `qdrant_operation_duration_seconds` | operation | операции с векторной базой |
| `celery_*`, `cache_*` | — | воркер и кэш (объявлены, подключаются по мере использования) |

Пример скрейпа (prometheus.yml):

```yaml
scrape_configs:
  - job_name: kag-api
    metrics_path: /metrics
    static_configs:
      - targets: ["192.168.50.18:8000"]
```

Примеры запросов в Grafana (p95 по стадиям и полный путь):

```promql
histogram_quantile(0.95, sum(rate(rag_stage_duration_seconds_bucket[5m])) by (le, stage))
histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket{endpoint="/api/v1/chat/"}[5m])) by (le))
sum(rate(llm_tokens_total[1h])) by (type)
```

## 3. Что уже проверено живьём

- `X-Trace-ID` приходит в заголовке ответа; при запросе с клиентским заголовком он сохраняется;
- строки журнала несут тот же идентификатор, включая строки из рабочих потоков;
- `/metrics` отдаёт метрики в формате Prometheus, включая стадии RAG;
- невалидные идентификаторы (кириллица, перенос строки, слишком длинные) заменяются своим.

## 4. Что ещё не сделано (осознанно)

- **Сквозной trace через воркер**: задачи Celery пока не несут trace_id — для вопросов чата это не
  нужно, а для разбора обработки документа понадобится (передавать в headers задачи и логировать);
- **OpenTelemetry spans**: в проекте есть заготовки (`setup_opentelemetry`), полноценные трейсы со
  waterfall — отдельная задача; сейчас роль «waterfall» играют постадийные строки журнала по trace_id;
- **Central log store (Loki)**: логи сейчас локальные в контейнерах; для поиска по идентификатору
  без docker logs нужен Loki + Grafana (или аналог);
- **Метрики воркера**: `celery_*` объявлены, но в задачах ещё не записываются (документы/эмбеддинги);
- **Алерты**: не настроены (например, «p95 чата > 10 с 10 минут» или «доля ошибок LLM > 5%»).
