# KAG System — Документация проекта

> Knowledge Augmentation Generation — система управления документами с ИИ: OCR, векторный поиск, граф знаний, чат.
> **Дата последнего обновления:** 2026-08-23
> **Ветка:** stable_PyMuPDF
> **Сервер:** 192.168.50.18 (внутренний), qd.gostsecret.ru (внешний)

## Разделы

| Файл | Содержание |
|---|---|
| [architecture-current.md](architecture-current.md) | Архитектура (актуальная): сервисы, потоки данных, базы, модули |
| [ARCHITECTURE-LEGACY.md](ARCHITECTURE-LEGACY.md) | Архитектура (историческая версия из проекта) |
| [decisions.md](decisions.md) | ADR — ключевые решения и почему |
| [troubleshooting.md](troubleshooting.md) | Узкие места и проблемы: симптом → причина → решение |
| [sessions-summary.md](sessions-summary.md) | Сводка по сессиям (детально, без сжатия) |
| [guides/](guides/) | Инструкции: embedding, чанкинг, граф, деплой, чат, веб-монитор |

## Ключевые факты (актуально на 2026-08-23)

- **Embedding:** GigaChat `Embeddings`, размерность **1024**, лимит входа ~500 символов (512 токенов)
- **Чанкинг:** размер **500** символов, overlap **15% (75)** — применяется вручную (RecursiveCharacterTextSplitter игнорирует overlap)
- **LLM (чат/граф/анализ):** `deepseek-v4-flash` (api.deepseek.com) — ⚠️ граф знаний возвращает ПУСТЫЕ ответы
- **Worker:** 4 CPU / 12G (лимиты через `${WORKER_CPUS:-4.0}` / `${WORKER_MEMORY:-12G}`)
- **Очередь:** Celery, redis db=1, QueueGuard (`qguard:{doc_id}`, SET NX, TTL 6ч)
- **Qdrant:** коллекция `kag_documents`, dense 1024 COSINE (+ sparse, отключён для скорости)
- **Neo4j:** Community — NODE KEY недоступны → MERGE + индекс

## Статусы документов

`pending → processing → completed | failed`

- completed: 158+ (на момент паузы 2026-08-22; очередь 0)
- recovery: сбрасывает зависшие >60 мин (снимает замок QueueGuard перед перезапуском)

## Полезные команды

```bash
# Очередь
docker exec kag-redis redis-cli -n 1 LLEN documents
# Замки QueueGuard
docker exec kag-redis redis-cli -n 1 KEYS 'qguard:*'
# Статусы
docker exec kag-kag-db psql -U kag -d kag -t -c "SELECT status, count(*) FROM documents GROUP BY status;"
# Логи worker
docker logs -f kag-worker
```
