# Гид: чанкинг документов

> Актуально на 2026-08-23. Настройки в админке: «Настройки чанкинга документов».

## Текущие настройки

- **chunk_size = 500** символов (ровно под лимит эмбеддинга ~500)
- **chunk_overlap = 75** символов (15%) — в админке вводится в ПРОЦЕНТАХ
- Хранится в config_store (`chunking/default`) как символы; UI конвертирует % → символы

## Почему 500

- Лимит входа GigaChat ~512 токенов ≈ 500 символов кириллицы.
- Если chunk_size > лимита — хвост чанка НЕ попадает в вектор (потеря информации).
- 500 символов ≈ 150-250 токенов реально (консервативно ~350) — запас ~30%.

## Почему overlap 15%

- 10-20% — оптимум индустрии; >25% = чанки-копии без роста точности.
- Перекрытие добавляет хвост предыдущего чанка в начало следующего — граница смыслового блока попадает в оба вектора.

## КАК применяется overlap (важно!)

RecursiveCharacterTextSplitter (langchain) **игнорирует** chunk_overlap при разбиении по разделителям
(`\n\n`, `\n`, `. `) — применяет только при посимвольной резке. Поэтому в
`src/indexing/chunking.py` (`chunk_segments`) после split_text мы добавляем overlap ВРУЧНУЮ:

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

## Применение настроек

- **Новые документы** — применяются сразу.
- **Существующие** — НЕ пересчитываются автоматически. Нужно «♻️ Переиндексировать все документы» (админка).
- Переиндексация: ~1.5 мин/документ (из них ~70 сек граф знаний с LLM); 150+ документов — несколько часов.
- При force-переобработке старые векторы Qdrant удаляются автоматически + граф Neo4j очищается.

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
        if len(t1) >= 100 and t1[-75:] in t2:
            ok += 1
    print('overlap пар:', ok)
asyncio.run(main())
```

## Если менять настройки

1. Поменяй в админке (размер в символах, overlap в процентах).
2. Нажми «Сохранить» — увидишь сколько символов получилось из %.
3. Проверь что chunk_size ≤ лимита текущей embedding-модели.
4. Переиндексируй все документы.
