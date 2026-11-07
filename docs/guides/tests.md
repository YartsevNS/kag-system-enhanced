# Прогон тестов

## Быстрый старт

    uv venv .venv-test --python 3.11
    uv pip install --python .venv-test/Scripts/python.exe -r requirements-dev.txt
    .venv-test/Scripts/python.exe -m pytest tests -q --timeout=45 --timeout-method=thread

На Linux/сервере пути те же, только бинарник `bin/python` вместо `Scripts/python.exe`.

## Что в наборе

    tests/                 1086 тестов в 99 файлах (коллекция 67 с)
    conftest.py            фикстура auth_headers: валидный локальный JWT
                           (секрет уникален на прогон, в БД не ходит)

## Важные ограничения

1. **`pytest` не входит в `requirements.txt`** — он в `requirements-dev.txt`, потому что
   в образе не нужен. Раньше его не было нигде: штатный прогон не запускался.
2. **Полный прогон идёт долго** (десятки минут): часть тестов ходит в сеть (провайдеры LLM,
   Docker-монитор) и ждёт таймаут. Поэтому `--timeout=45 --timeout-method=thread` обязателен —
   иначе прогон висит без вывода.
3. **Тесты не требуют поднятого стенда.** Всё, что обращается к Postgres/Qdrant/TypeDB,
   подменяется фикстурами или моками.
4. **`.venv311` на рабочей машине сломан** (`DLL load failed while importing _sqlite3`) —
   использовать `.venv-test`, он создаётся через `uv` с собственным Python 3.11.

## Флоу обработки — целевой набор

Эти файлы проверяют путь «документ → чанки → векторы → граф» и охраняют уже случавшиеся аварии:

    tests/test_processing_file_pick.py      выбор файла: бэкап *.v1.bak не подменяет рабочий
    tests/test_document_file_lookup.py      поиск файла: точное имя, legacy-имена, чужой префикс
    tests/test_resilience_guards.py         запись векторов, liveness-recovery, потеря задач
    tests/test_tasks_importable.py          модули конвейера импортируются
    tests/test_processing_block_guard.py    блокировка обработки из админки
    tests/test_upload_route_guards.py       права и защиты на роутах загрузки
    tests/test_document_cleanup_guard.py    уборка файлов удалённого документа
    tests/test_ingest_guard.py              защита от повторной загрузки
    tests/test_document_access.py           доступ к чужим документам

Прогон только этого набора (21,7 с, 79 проверок):

    .venv-test/Scripts/python.exe -m pytest tests/test_processing_file_pick.py \
        tests/test_document_file_lookup.py tests/test_resilience_guards.py \
        tests/test_tasks_importable.py tests/test_processing_block_guard.py \
        tests/test_upload_route_guards.py tests/test_document_cleanup_guard.py \
        tests/test_ingest_guard.py tests/test_document_access.py -q

## Чего тесты НЕ покрывают

- **TypeDB-контур**: ни один файл в `tests/` не упоминает `typedb`. Запись сущностей,
  провенанс `source-chunk-id`, ACL-фильтр фактов, правила `violated_documents` и
  `violated_certification` проверяются только живыми прогонами на стенде.
- **Политика ретраев обработки**: перевод в `failed` при постоянных ошибках проверен
  вручную (документ `8ef6b9c4`), автотеста нет.
- **Поиск документов** (какой фрагмент поднялся): измеряется набором
  `eval/golden_dataset_ib.jsonl` и скриптами `scripts/eval_*`, а не pytest.
