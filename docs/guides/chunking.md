# Гид: чанкинг документов

> Актуально на 2026-09-12. Настройки в админке: «Настройки чанкинга документов».
> Будущие варианты (семантический сплиттер, чанкинг по пунктам) — в
> `docs/plans/rag-chunking-and-lexical-search.md`.

## Текущие настройки

- **chunk_size = 1500** символов
- **chunk_overlap = 225** символов (15%) — в админке вводится в ПРОЦЕНТАХ
- Значения берутся из `src/config.py` (`CHUNK_SIZE` / `CHUNK_OVERLAP`).
  В `config_store` ключа `chunking/default` сейчас НЕТ: он пропал вместе с томом
  postgres 2026-09-12 (пересоздание тома унесло и конфиг), поэтому действуют
  дефолты кода. Если админ сохранит настройки в UI — они появятся в config_store
  и начнут перекрывать дефолты.

## Почему 1500 и overlap 15%

- Действующая модель эмбеддингов — `EmbeddingsGigaR` (2560 измерений, лимит
  входа 4096 токенов). 1500 символов кириллицы ≈ 750-1000 токенов, то есть
  запас более чем двукратный: хвост чанка в вектор не теряется.
  (Прежняя причина «500 символов под лимит 512 токенов» относится к другой
  модели и больше не действует.)
- Чанкинг сегментный: буфер накапливает сегменты (абзац/таблица/страница) до
  `chunk_size`, поэтому таблицы и абзацы не рвутся посередине.
- Overlap 15% — оптимум индустрии; >25% даёт чанки-копии без роста точности.
  Перекрытие добавляет хвост предыдущего чанка в начало следующего, чтобы
  граница смыслового блока попала в оба вектора.

## КАК применяется overlap (важно!)

RecursiveCharacterTextSplitter (langchain) **игнорирует** chunk_overlap при разбиении по разделителям
(`\n\n`, `\n`, `. `) — применяет только при посимвольной резке (сегменты-гиганты
длиннее `chunk_size`). Поэтому в `src/indexing/chunking.py` (`chunk_segments`)
после split_text мы добавляем overlap ВРУЧНУЮ:

```python
overlap = self.chunk_overlap or 0
prev_text = ""
for i, text in enumerate(split_texts):
    if prev_text and overlap > 0:
        prev_tail = prev_text[-overlap:]
        text = prev_tail + text
    if len(text) > self.chunk_size + overlap:
        text = text[:self.chunk_size + overlap]
    prev_text = text
    ...
```

Ключевые моменты:
- Хвост берём из **prev_text** (уже сформированного чанка с overlap), а НЕ из split_texts[i-1] — иначе граница «съезжает», дыры.
- Обрезаем до `chunk_size + overlap`, НЕ до chunk_size — иначе теряем конец и создаём дыры.
- В metadata чанка пишется `overlap_applied` (True для i>0) — можно проверить в Qdrant.

## Порядок чанков в интерфейсе (2026-09-12)

Чанки должны идти по возрастанию номера: 1, 2, 3 … N — и на странице «Чанки», и в
API. Раньше показывались вперемешку, две причины:

1. **Номер чанка лежит в payload внутри `metadata`** (`metadata.chunk_seq`), а не на
   верхнем уровне. Код читал `payload.get("chunk_seq")` → всегда 0.
2. Запасной разбор номера из `chunk_id` был рассчитан на старый формат
   (`chunk_00001`) и падал на текущем `<document_id>_chunk_00045` (в начале UUID →
   `int()` бросал исключение). В итоге у всех чанков ключ сортировки был 0, и
   сортировка ничего не делала: порядок оставался таким, каким его вернул
   Qdrant scroll, то есть произвольным.

Плюс общая страница без выбранного документа запрашивала `/api/v1/chunks`, которого
не существовало: получала 404 и уходила в fallback на семантический поиск с пустым
запросом — порядок по релевантности, то есть тоже почти случайный.

Сейчас:

- разбор номера и ключ сортировки — в `src/api/services/chunk_order.py`
  (top-level → `metadata` → разбор из `chunk_id`, включая формат с префиксом
  документа); чанки без номера идут в конец, но в детерминированном порядке;
- `GET /api/v1/upload/{document_id}/chunks` — номер берётся из `metadata`,
  пагинация применяется ПОСЛЕ сортировки;
- `GET /api/v1/chunks` (offset/limit/document_id) — общий список, порядок
  «документ → номер чанка → id»;
- сам `chunk_seq` пишется в `metadata` чанка при чанкинге
  (`src/indexing/chunking.py`) и в верхний уровень payload НЕ поднимается.

## Применение настроек

- **Новые документы** — применяются сразу.
- **Существующие** — НЕ пересчитываются автоматически. Нужно «♻️ Переиндексировать все документы» (админка) либо `POST /api/v1/upload/{id}/process` по каждому документу.
- Замеренный темп (2026-09-12): 0.83 с на чанк, эмбеддинги ~12% времени, остальное — извлечение сущностей для графа; 17 документов / 2049 чанков ≈ 29 минут.
- При force-переобработке старые векторы Qdrant удаляются автоматически + граф Neo4j очищается.
- После векторзации сервис сверяет exact count точек с числом чанков: 0 точек → документ `failed` (шаг `verify_points` в process-логе).

## Проверка overlap в Qdrant

```python
# в контейнере kag-api
from src.indexing.embeddings_service import embeddings_service
import asyncio
async def main():
    await embeddings_service.initialize()
    q = embeddings_service._qdrant_client
    col = embeddings_service.collection_name
    r = q.scroll(collection_name=col, scroll_filter={'must':[{'key':'document_id','match':{'value':'<doc_id>'}}]}, limit=100, with_payload=True, with_vectors=False)
    pts = sorted(r[0], key=lambda p: (p.payload.get('metadata',{}) or {}).get('chunk_seq', 0))
    ok = 0
    for i in range(len(pts)-1):
        t1 = pts[i].payload.get('content') or ''
        t2 = pts[i+1].payload.get('content') or ''
        if len(t1) >= 100 and t1[-225:] in t2:
            ok += 1
    print('overlap пар:', ok)
asyncio.run(main())
```

## Если менять настройки

1. Поменяй в админке (размер в символах, overlap в процентах).
2. Нажми «Сохранить» — увидишь сколько символов получилось из %.
3. Проверь, что `chunk_size` укладывается в лимит текущей embedding-модели
   (для EmbeddingsGigaR — 4096 токенов, то есть с запасом до ~6000 символов).
4. Переиндексируй документы (полный прогон или подмножество).
5. Замеряй эффект на наборе вопросов: `python scripts/evaluate_retrieval.py`
   (см. `docs/guides/retrieval-eval.md`).
