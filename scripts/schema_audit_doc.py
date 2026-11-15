"""Собрать документ для внешнего аудита схемы онтологии.

Источник — сами файлы схемы (`src/ontology/core.tql`, `modules/*.tql`), а не пересказ:
документ должен показывать аудитору ровно то, что загружается в TypeDB.

Запуск:  python scripts/schema_audit_doc.py [--out docs/ontology/SCHEMA-AUDIT.md]
"""
from __future__ import annotations

import importlib.util
import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ONTO = ROOT / "src" / "ontology"

# schema_view тянет за собой пакет src.api.services (тяжёлые зависимости),
# поэтому грузим модуль по пути — нам нужен только разбор .tql.
_spec = importlib.util.spec_from_file_location(
    "schema_view", ROOT / "src" / "api" / "services" / "schema_view.py")
schema_view = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(schema_view)


def card(c: str) -> str:
    return f"@card({c})" if c else ""


def entity_table(m) -> str:
    rows = ["| тип | по-русски | родитель | @abstract | атрибуты (owns) | роли (plays) |",
            "|---|---|---|---|---|---|"]
    for e in m["entities"]:
        owns = ", ".join(
            f"`{o['attr']}`" + (" **@key**" if o["key"] else "") + (" @unique" if o["unique"] else "")
            + (" " + card(o["card"]) if o["card"] else "")
            for o in e["owns"]) or "—"
        plays = ", ".join(f"`{p}`" for p in e.get("plays") or []) or "—"
        rows.append(f"| `{e['label']}` | {e.get('ru') or '—'} | "
                    f"{('`' + e['super'] + '`') if e['super'] else '—'} | "
                    f"{'да' if e['abstract'] else '—'} | {owns} | {plays} |")
    return "\n".join(rows)


def relation_table(m) -> str:
    rows = ["| связь | по-русски | родитель | роли (relates) | атрибуты связи |",
            "|---|---|---|---|---|"]
    for r in m["relations"]:
        roles = ", ".join(f"`{x['role']}`" + (" " + card(x["card"]) if x["card"] else "")
                          for x in r.get("relates") or []) or "—"
        owns = ", ".join(f"`{o['attr']}`" for o in r["owns"]) or "—"
        rows.append(f"| `{r['label']}` | {r.get('ru') or '—'} | "
                    f"{('`' + r['super'] + '`') if r['super'] else '—'} | {roles} | {owns} |")
    return "\n".join(rows)


def attribute_table(m) -> str:
    rows = ["| атрибут | тип значения |"]
    rows.append("|---|---|")
    for a in m["attributes"]:
        rows.append(f"| `{a['label']}` | {a['value']} |")
    return "\n".join(rows)


def main() -> int:
    modules = [schema_view.parse_module("core", ONTO / "core.tql")]
    for p in sorted((ONTO / "modules").glob("*.tql")):
        modules.append(schema_view.parse_module(p.stem, p))

    totals = {
        "entities": sum(len(m["entities"]) for m in modules),
        "relations": sum(len(m["relations"]) for m in modules),
        "attributes": sum(len(m["attributes"]) for m in modules),
        "roles": sum(len(r.get("relates") or []) for m in modules for r in m["relations"]),
        "plays": sum(len(e.get("plays") or []) for m in modules for e in m["entities"]),
    }

    parts = []
    parts.append(f"""# Схема онтологии KAG — документ для аудита

Сформирован: {datetime.now().strftime('%d.%m.%Y %H:%M')}
Источник: `src/ontology/core.tql` (ядро) и `src/ontology/modules/*.tql` (доменные модули).
Это ровно те файлы, из которых собирается и загружается схема в TypeDB.

## 1. Что это за онтология

Онтология нормативных требований и мер защиты: документы регуляторов (ФСТЭК, ФСБ, Банк России, ГОСТ),
их структура (разделы, пункты, приложения), требования, меры (контроли), доказательства, редакции
документов, а также родовые понятия — субъекты, активы, данные, угрозы, уязвимости, риски,
инциденты, нарушения, термины, функции и процессы.

Контур продукта: документ компании → его пункты → покрытие требований регуляторов → пробелы →
новая редакция документа со ссылками на требования.

Архитектурное решение (ADR-010): **ядро доменно-независимо, домены подключаются модулями**.
Связи и роли объявлены один раз в ядре; доменные типы (ИБ, ИТ, следующий домен) —
подтипы родовых и играют те же роли. Поэтому требование любого домена участвует
в одних и тех же связях, а новый домен не требует правки ядра.

## 2. Состав

| модуль | файл | сущностей | связей | атрибутов |
|---|---|---|---|---|""")
    for m in modules:
        parts.append(f"| {m['name']} | `{m['file']}` | {len(m['entities'])} | "
                     f"{len(m['relations'])} | {len(m['attributes'])} |")
    players = [(m['name'], list((m.get('role_players') or {}).keys())) for m in modules]
    players = [(n, ks) for n, ks in players if ks]
    if players:
        parts.append(
            "\nПодписки ролей для типов, объявленных в другом модуле: "
            + "; ".join(f"модуль {n}: " + ", ".join(f"`{k}`" for k in ks) for n, ks in players)
            + ". Это не новые типы модуля — тип объявлен в ядре, модуль только подписывает его на роль.")
    parts.append(f"""
Итого: **{totals['entities']} сущностей**, **{totals['relations']} связей**,
**{totals['attributes']} атрибутов**, **{totals['roles']} ролей в связях**,
**{totals['plays']} подписок plays**.

## 3. Соглашения (важно для аудита)

1. **Идентичность.** У каждой сущности обязательный атрибут `ent-key` вида `<тип>:<sha1(ключ)>`.
   Смысловой ключ собирается из данных документа: документ — «издатель|вид|номер»,
   редакция — документ + версия, пункт — документ + `struct_index`,
   требование — документ + пункт + порядок (или хэш дословного текста),
   мера — нормализованный код (`АВЗ-1` = `АВЗ.1` = `авз 1`), регулятор — каноническое имя.
   Домен входит в ключ, поэтому коллизий между доменами нет.
2. **Провенанс.** Любая сущность помнит источник: `source-chunk-id` (первый фрагмент),
   `source-chunk-ids` (все фрагменты), `source-clause` (пункт). Это позволяет показать
   цитату для каждого факта и применить права доступа по источнику.
3. **Обязательность.** В TypeDB 3 `@abstract` не навязывает контракт подтипам, поэтому
   обязательные атрибуты (`@key`, `@card(1..1)`) дополнительно проверяются конвейером записи:
   сущность без обязательного атрибута не пишется и попадает в отчёт «отброшено».
4. **Роли.** Каждая роль связи объявлена с кардинальностью; роли `@card(0..)` необязательны.
   Исполнитель роли указывается явно (`entity X plays relation:role`) — если роль без
   исполнителя, TypeDB отвергает вставку всей связи ([INF11]), поэтому такие ошибки проверяются.
5. **Единицы домена.** Доменные признаки добавляются подтипами и владением, но не дублируют
   родовые типы: `cii_object` и `security_software` — подтипы `asset`, `certificate` — подтип
   `evidence`, `threat_source` — подтип `threat`, `vulnerability` — родовой тип ядра с
   доменными атрибутами БДУ.

## 4. Ядро: родовые типы

Родовые понятия доменно-независимы: их используют и ИБ, и ИТ, и любой следующий домен.
""")
    core = modules[0]
    parts.append("### 4.1 Сущности ядра\n")
    parts.append(entity_table(core))
    parts.append("\n### 4.2 Связи ядра\n")
    parts.append(relation_table(core))
    parts.append("\n### 4.3 Атрибуты ядра\n")
    parts.append(attribute_table(core))

    for m in modules[1:]:
        parts.append(f"\n## 5.{modules.index(m)} Домен `{m['name']}`\n")
        parts.append("### Сущности\n")
        parts.append(entity_table(m))
        if m["relations"]:
            parts.append("\n### Связи\n")
            parts.append(relation_table(m))
        if m["attributes"]:
            parts.append("\n### Атрибуты\n")
            parts.append(attribute_table(m))

    parts.append("""
## 6. Как схема собирается и загружается

    python -m src.ontology.build_schema --db <база> --modules sec,it

Сборщик конкатенирует ядро и выбранные модули, проверяет:
- в ядре нет доменных имён (`sec_`, `it_`, `security`, `cii_`, `bdu`);
- нет двойных объявлений одного типа;
- каждая роль связи имеет исполнителя (проверка отдельно, см. п. 3.4).
Затем загружает схему одной транзакцией и печатает счётчики типов.

Доступные базы: `cybersecurity_ontology_v26` (актуальная схема, 128 типов сущностей,
35 связей, 62 атрибута, 77 ролей), `v23` (рабочие данные предыдущей схемы), `v25`, `v24` (отладка).

## 7. Что известно про качество заполнения (на 15.11.2026)

Документ `fstek-2014-mery-zashchity-gis.pdf`, 321 фрагмент, прогон на схеме v23:

| показатель | значение |
|---|---|
| пунктов документа | 195 |
| требований | 473 |
| мер | 114 |
| требований со связью «выведено из пункта» | 466 (98,5 %) |
| требований со связью «мера определяет требование» | 351 (74 %) |
| связей «мера описана в пункте» | 130 |
| всего связей | 1174 |

Ограничение: обработан один документ из корпуса; документы компании не обрабатывались,
поэтому ветка покрытия (`requirement_coverage`, `document_compliance`) не заполнена.

## 8. Что заведомо не покрыто (кандидаты на доработку после аудита)

1. Таблицы как объекты (строка, ячейка) — таблица мер извлекается как текст, а не как структура.
2. Приложения и формы (документ → приложение → таблица).
3. Перекрёстные ссылки «пункт → пункт» внутри документа (есть только документ → документ).
4. Связь «термин → пункт, где он определён».
5. Версии требования между редакциями документа.
6. Матрица «требование × роль исполнителя» (типы есть, связи нет).
7. Правила TypeDB (`when → then`) — сейчас онтология является графом фактов без вывода.
8. Второй домен (ИТ) — только схема, данных нет.

## 9. Вопросы, на которые нужен ответ аудита

1. Достаточна ли модель «ядро + доменные модули» для нескольких предметных областей,
   или нужна другая декомпозиция (например, отдельные базы с федеративным слоем)?
2. Правильно ли выбрана идентичность сущностей (структурный ключ + хеш текста для требований)?
   Где она сломается на перефразировании, OCR-ошибках, переиздании документа?
3. Достаточно ли связей, чтобы отвечать на вопросы вида «какие требования предъявляются
   к объекту», «чем закрывается требование», «что не покрыто документом компании»?
4. Нужны ли правила вывода (TypeDB rules) и какие именно — или вывод должен остаться в коде?
5. Как правильно моделировать таблицы и приложения, не потеряв дословность?
6. Что в схеме лишнее или преждевременное?

## 9.1. Реакция на внешний аудит

Аудит схемы выполнен внешней моделью; решения по каждому замечанию (принято / отклонено / отложено)
зафиксированы в `docs/ontology/AUDIT-RESPONSE.md`. Кратко: принято 13 замечаний, из них закрыты
в схеме — ИТ-домен как подтипы `asset`, `regulator sub party`, `bdu-id @unique`, удаление дубля
`threat_measure`, расширение ролей применимости, новые связи `violation_of`, `term_source`,
`requirement_succession`, `clause_reference`, роль `duty_role` в `responsibility`.

## 10. Полный исходный текст схемы

Ниже — файлы в том виде, в котором они загружаются.
""")
    for m, path in zip(modules, [ONTO / "core.tql"] + sorted((ONTO / "modules").glob("*.tql"))):
        parts.append(f"\n### `{path.relative_to(ROOT).as_posix()}`\n\n```tql\n"
                     + path.read_text(encoding="utf-8").rstrip() + "\n```\n")

    out = ROOT / "docs" / "ontology" / "SCHEMA-AUDIT.md"
    for i, a in enumerate(__import__("sys").argv):
        if a == "--out" and i + 1 < len(__import__("sys").argv):
            out = Path(__import__("sys").argv[i + 1])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(parts), encoding="utf-8")
    print(f"документ готов: {out} ({out.stat().st_size} байт)")
    print(f"сущностей {totals['entities']}, связей {totals['relations']}, "
          f"атрибутов {totals['attributes']}, ролей {totals['roles']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
