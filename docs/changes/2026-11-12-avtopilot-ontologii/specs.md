# Спецификация: автопилот онтологии

## 1. Правила автопилота (список в entity_aliases)

| Правило | Условие | Уверенность |
|---|---|---|
| same_letters | вариант и канон совпадают по буквам (регистр, пробелы, знаки, ё/е) | 0.99 |
| registry_entry | канона нет, в варианте ≥ 3 буквы (реестровая запись, а не синоним) | 0.90 |
| requisites | в любом из названий есть ИНН/ОГРН/КПП с числом ≥ 9 цифр | 0.95 |
| abbreviation_from_full | короткое имя выводится из первых букв значимых слов полного (служебные слова пропускаются) | 0.92 |
| иначе | остаётся человеку; подсказка abbreviation_mismatch, если короткая строка (2–8 символов) похожа на аббревиатуру полного названия из ≥ 2 слов | — |

Отклонение автоматически НЕ ставится: верный синоним без выводимой аббревиатуры
(ЦБ → Банк России) правилом не отличим от мусора. Машина сортирует, человек решает.

## 2. Журнал авто-решений (ontology_auto_decisions)

Поля: kind (alias|duplicate|gap), subject, decision, rule, confidence, payload,
status (active|revoked), created_by, created_at, revoked_by/at, note.
Отзыв возвращает записи в очередь: пары словаря → verdict='', reviewed=false,
source='autopilot:revoked'; дубли → status='pending'; пробелы → status='proposed'.

## 3. Покрытие вопроса (answerability)

- Токенизация и стемминг — src/indexing/lexical.py (тот же, что в BM25).
- Словарь: подтверждённые entity_aliases (5000) + типы/связи доменной схемы.
- coverage = известные стемы / все значимые стемы вопроса; кеш 300 с.
- verdict: covered ≥ 0.6, partial ≥ 0.3, иначе uncovered.
- Чатовый путь: результат в metadata.answerability; пробелы пишутся в
  ontology_schema_gaps (hits+1, до 5 примеров), общие слова не пишутся.
- Отказ генерации не меняется: гейт информирует и копит пробелы, а не блокирует
  ответ (порог покрытия калибруется на реальных вопросах).

## 4. Learn

- /terms: любое решение/правка/удаление пары сбрасывает кеш глоссария.
- Глоссарий (src/api/services/glossary.py): 60 подтверждённых пар «вариант → канон»
  и 20 отклонённых «вариант ≠ канон», потолок 2200 символов, кеш 600 с.
- Глоссарий подставляется в задание извлечения отдельным блоком контекста
  (typedb_writer._render_prompt), текст промпта не меняется.
- /ontology/schema-gaps/{id}/review accepted: термин попадает в entity_aliases
  (source='gap_accepted', approved). Канон берётся из примечания «канон: X».

## 5. API

| Метод | Путь | Что делает |
|---|---|---|
| GET | /api/v1/ontology/automation/decisions | журнал авто-решений + сводка |
| POST | /api/v1/ontology/automation/run | прогон правил по scope=aliases\|duplicates\|all, dry_run |
| POST | /api/v1/ontology/automation/decisions/{id}/revoke | отзыв решения |
| GET | /api/v1/ontology/schema-gaps/draft | каркас define из пробелов |
| GET | /api/v1/ontology/answerability?q= | покрытие вопроса без чата |
| GET | /api/v1/ontology/glossary | блок глоссария, уходящий в извлечение |
