# Устройство: ядро, модули, выравнивание

## Раскладка схемы

    src/ontology/
        core.tql              # абстрактные типы, роли, общие атрибуты
        modules/sec.tql       # домен информационной безопасности
        modules/it.tql        # домен информационных технологий
        build_schema.py       # конкатенация + проверка дублей имён + загрузка в базу
    config/
        alignment/sec_it.yaml # маппинги между доменами
        extractors/*.yaml     # регулярки и словари (следующий этап)

## Ядро (эскиз TypeQL)

    attribute key, value string;         # смысловой ключ (единое правило на домен)
    attribute text, value string;
    attribute ordinal, value integer;
    attribute valid-from, value date;
    attribute valid-to, value date;
    attribute source-path, value string; # structural_path

    entity entity-core @abstract, owns key @key;
    entity document sub entity-core, @abstract,
        owns title, owns document-kind, owns domain;
    entity version sub entity-core, @abstract,
        owns version-string;
    entity clause sub entity-core, @abstract,
        owns ordinal, owns text, owns source-path;
    entity requirement sub entity-core, @abstract,
        owns ordinal, owns text, owns source-path, owns obligation-level;
    entity control sub entity-core, @abstract,
        owns code, owns name, owns text;
    entity evidence sub entity-core, @abstract,
        owns text;

    relation alignment,
        relates source, relates target,
        owns alignment-kind, owns confidence, owns authored-by;
    relation requirement_definition,
        relates requirement, relates control,
        owns point-in-control;
    relation coverage,
        relates company-clause, relates requirement;

Домен в имени типа, ядро — без префикса.

## Модуль домена (эскиз)

    # modules/sec.tql
    entity sec_requirement sub requirement;
    entity sec_measure sub control;
    entity sec_regulator sub entity-core;
    entity sec_document sub document;
    # роли наследуются: sec_requirement играет requirement в requirement_definition

    # modules/it.tql
    entity it_requirement sub requirement;
    entity it_control sub control;
    entity it_service sub entity-core;
    entity it_document sub document;

Добавление домена = новый файл в `modules/`. Ядро не меняется.

## Правила ключей (единые для любого домена)

    document     домен:издатель|вид|номер            sec:fstek|приказ|17
    clause       документ#structural_path            sec:fstek|приказ|17#7.7.1.5
    requirement  документ#structural_path#порядок    sec:fstek|приказ|17#7.7.1.5#3
    control      домен:код                            sec:авз.1     it:ac-2
    version      документ#версия                      sec:fstek|приказ|17#ред-2
    regulator    домен:код органа                     sec:fstek

    ent-key = <тип>:sha1(<ключ>)   — домен внутри ключа, коллизий между доменами нет

Пока structural_path не заполнен (1b не сделан), требование получает ключ
`документ#clause#порядок` при наличии пункта, иначе `домен:kag:<uuid>#<h(текст)>` —
временное правило, помеченное в объекте (`key-source = "fallback"`), чтобы миграция не врала.

## Выравнивание (YAML)

    - source: {domain: sec, type: sec_measure, code: "авз.1"}
      target: {domain: it, type: it_control, code: "ac-2"}
      kind: close          # exact | close | related
      confidence: 0.8
      authored_by: "эксперт: ФИО"
      note: "антивирусная защита ↔ контроль вредоносного ПО"

Загрузчик: `scripts/load_alignment.py` — идемпотентный, пишет связь `alignment`
с атрибутами; повторный запуск не плодит дубли.

## Миграция (v23 → v24)

1. Собрать `core.tql` + `sec.tql`, загрузить в новую базу `cybersecurity_ontology_v24`.
2. Скрипт миграции: читает v23, пишет v24 с новыми типами и ключами; сохраняет
   `source-chunk-id`, `source-chunk-ids`, `ent-key`-провенанс.
3. Сверка: счётчики по категориям, множества смысловых ключей, провенанс.
4. Прогон одного документа в v24 и сравнение с v23 по числам (требования/меры/элементы/связи).
5. Только после сверки `.env` переключается на v24; v23 остаётся.

## Проверки, которые должны быть в тестах

- ядро без доменных имён (`grep` по core.tql);
- полиморфный запрос по `requirement` возвращает оба домена;
- ключи: по одному примеру на категорию;
- идемпотентность загрузчика выравнивания;
- миграция: расхождений 0;
- добавление «пустого» третьего домена не ломает прежние проверки.
