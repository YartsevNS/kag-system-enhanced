"""Онтология связей графа: закрытый список типов с допустимыми парами сущностей.

Зачем: замер 10.10.2026 показал, что смысловой слой графа непригоден — по схеме проходили 27%
связей, 16,6% были формально невозможны («место находится в месте», «человек подписан человеком»),
а 56% свалены в мусорку BELONGS_TO, у которой семантики вообще нет. Причина в задании: промпт
ограничивал ТИПЫ СУЩНОСТЕЙ, но типы связей не ограничивал вовсе, поэтому модель придумывала их на ходу.

Правильный порядок (и то, что делает этот модуль):
  1. Объявить типы связей с ОСМЫСЛЕННЫМИ именами и допустимыми парами «источник → цель».
  2. Отдать этот список модели в задании (тогда она отвечает в рамках схемы, а не изобретает).
  3. Проверить каждую тройку перед записью в граф — детерминированно, без модели.
  4. Привести свободные названия от модели к каноническому типу (иначе всё сваливается в мусорку).

Модуль без внешних зависимостей: работает и в контейнере, и на ноутбуке, и в тестах.
"""

from __future__ import annotations

from dataclasses import dataclass

# ── Типы сущностей, между которыми бывают связи ────────────────────────────────
# Совпадают с доменной схемой (DEFAULT_DOMAIN_SCHEMA) плюс два типа, которых не хватало:
# «стандарт» (ГОСТ/СП) и «пункт» — их извлекают правила, и именно по ним чаще всего спрашивают.
ENTITY_TYPES = {
    "document_ref": "документ или его номер",
    "legal_term": "термин, понятие, требование",
    "organization": "организация, орган власти",
    "person": "человек (подписант, должностное лицо)",
    "date": "дата",
    "money": "сумма, стоимость",
    "location": "место, адрес",
    "standard": "стандарт, свод правил (ГОСТ, СП, СНиП)",
    "clause": "пункт, раздел, таблица, приложение",
    "event": "событие",
}


@dataclass(frozen=True)
class RelationSpec:
    """Тип связи: человеческое имя, смысл и допустимые пары типов сущностей."""

    code: str                  # канонический код (в данных — латиницей)
    title: str                 # как показать человеку
    meaning: str               # что означает связь
    pairs: tuple[tuple[str, str], ...]  # допустимые пары (источник → цель)


# ── Закрытый список смысловых связей ───────────────────────────────────────────
# Только то, что реально встречается в нормативных и деловых документах.
# RELATED_TO оставлен как крайняя мера: если модель не смогла выбрать тип, факт не теряем,
# но помечаем — и по этой доле видно, что задание надо уточнять.
RELATIONS: tuple[RelationSpec, ...] = (
    RelationSpec("SIGNED_BY", "подписано", "документ подписан лицом или организацией",
                 (("document_ref", "person"), ("document_ref", "organization"))),
    RelationSpec("ISSUED_BY", "издано", "документ издан или утверждён органом",
                 (("document_ref", "organization"), ("standard", "organization"))),
    RelationSpec("DATED", "датировано", "документ или событие имеет дату",
                 (("document_ref", "date"), ("organization", "date"),
                  ("person", "date"), ("standard", "date"), ("event", "date"))),
    RelationSpec("AMOUNT", "на сумму", "документ или организация связаны с суммой",
                 (("document_ref", "money"), ("organization", "money"), ("money", "money"))),
    RelationSpec("LOCATED_AT", "расположено", "организация или лицо находится по адресу",
                 (("organization", "location"), ("person", "location"))),
    RelationSpec("SUPERSEDES", "отменяет", "документ отменяет или заменяет другой документ",
                 (("document_ref", "document_ref"), ("standard", "standard"))),
    RelationSpec("AMENDS", "изменяет", "документ вносит изменения в другой документ",
                 (("document_ref", "document_ref"),)),
    RelationSpec("REFERENCES", "ссылается на", "документ ссылается на другой документ или стандарт",
                 (("document_ref", "document_ref"), ("document_ref", "standard"),
                  ("standard", "standard"), ("standard", "document_ref"))),
    RelationSpec("HAS_CLAUSE", "содержит пункт", "документ содержит пункт, раздел или приложение",
                 (("document_ref", "clause"), ("standard", "clause"))),
    RelationSpec("REQUIRES", "устанавливает требование",
                 "документ, стандарт или пункт устанавливает требование к чему-либо",
                 (("document_ref", "legal_term"), ("standard", "legal_term"),
                  ("clause", "legal_term"), ("standard", "organization"))),
    RelationSpec("APPLIES_TO", "распространяется на", "документ распространяется на кого/что-либо",
                 (("document_ref", "organization"), ("document_ref", "legal_term"),
                  ("document_ref", "person"), ("standard", "organization"))),
    RelationSpec("PART_OF", "входит в состав", "одно входит в состав другого",
                 (("document_ref", "document_ref"), ("legal_term", "legal_term"),
                  ("document_ref", "standard"), ("clause", "document_ref"),
                  ("standard", "document_ref"))),
    RelationSpec("DEFINES", "определяет", "документ или стандарт определяет термин",
                 (("document_ref", "legal_term"), ("standard", "legal_term"))),
    RelationSpec("RELATED_TO", "связано с", "связь есть, но её тип определить не удалось",
                 None),  # None = пары не проверяем, это крайняя мера
)

RELATION_BY_CODE = {r.code: r for r in RELATIONS}
SEMANTIC_CODES = {r.code for r in RELATIONS}

# Версия онтологии. Поднимать при ЛЮБОМ изменении типов или допустимых пар: от неё зависит
# ключ кэша извлечения. Без этого старые ответы модели переиспользуются, и правки онтологии
# не действуют — на живом прогоне 10.10.2026 все связи пришли из кэша и выглядели как RELATED_TO.
ONTOLOGY_EPOCH = 2

# Системные и структурные связи: их пишет код, модель их не порождает.
STRUCTURAL_CODES = {"MENTIONS", "HAS_CHUNK", "SECTION_CHUNK", "HAS_SECTION", "NEW_EDITION_OF"}

# ── Приведение свободных названий к канону ─────────────────────────────────────
# Это главное лечение мусорки BELONGS_TO: модель писала «входит в состав», «является частью»,
# «принадлежит» — всё в один тип без смысла. Теперь такие формулировки раскладываются по канону.
_SYNONYMS: dict[str, str] = {
    # русские формулировки
    "подписан": "SIGNED_BY", "подписано": "SIGNED_BY", "подписал": "SIGNED_BY",
    "издан": "ISSUED_BY", "издано": "ISSUED_BY", "утверждён": "ISSUED_BY", "утвержден": "ISSUED_BY",
    "датирован": "DATED", "дата": "DATED",
    "сумма": "AMOUNT", "стоимость": "AMOUNT",
    "расположен": "LOCATED_AT", "находится": "LOCATED_AT", "адрес": "LOCATED_AT",
    "отменяет": "SUPERSEDES", "заменяет": "SUPERSEDES", "отменён": "SUPERSEDES",
    "изменяет": "AMENDS", "вносит изменения": "AMENDS",
    "ссылается": "REFERENCES", "ссылается на": "REFERENCES", "упоминает стандарт": "REFERENCES",
    "содержит пункт": "HAS_CLAUSE", "пункт": "HAS_CLAUSE", "раздел": "HAS_CLAUSE",
    "требует": "REQUIRES", "устанавливает": "REQUIRES", "устанавливает требование": "REQUIRES",
    "распространяется на": "APPLIES_TO", "применяется к": "APPLIES_TO", "действует на": "APPLIES_TO",
    "входит в состав": "PART_OF", "является частью": "PART_OF", "часть": "PART_OF",
    "включает": "PART_OF", "содержит": "PART_OF",
    "определяет": "DEFINES", "определение": "DEFINES", "термин": "DEFINES",
    # ВНИМАНИЕ: «принадлежит» (BELONGS_TO) раньше раскладывалось в PART_OF и тем самым выдавало
    # догадку за факт. Замер судьёй 10.10.2026: из 12 таких связей судья согласился с типом лишь
    # в части случаев, а чаще относил их к RELATED_TO, APPLIES_TO и DEFINES. Пока тип не определён
    # честно оставляем RELATED_TO, а точный тип назначается отдельным проходом (JEV по вероятностям).
    # английские и кодовые варианты
    "SIGNED": "SIGNED_BY", "SIGNER": "SIGNED_BY", "ISSUER": "ISSUED_BY", "ISSUED": "ISSUED_BY",
    "DATE": "DATED", "AMOUNT_OF": "AMOUNT", "LOCATION": "LOCATED_AT", "LOCATED": "LOCATED_AT",
    "REPLACES": "SUPERSEDES", "SUPERSEDED": "SUPERSEDES", "AMEND": "AMENDS", "MODIFIES": "AMENDS",
    "REFERS_TO": "REFERENCES", "REFERENCE": "REFERENCES", "HAS_PARAGRAPH": "HAS_CLAUSE",
    "CLAUSE": "HAS_CLAUSE", "REQUIREMENT": "REQUIRES", "REQUIRES_COMPLIANCE": "REQUIRES",
    "APPLIES": "APPLIES_TO", "PART": "PART_OF", "INCLUDES": "PART_OF",
    "DEFINES_TERM": "DEFINES", "TERM": "DEFINES",
    "BELONGS_TO": "RELATED_TO", "BELONGS": "RELATED_TO",
    "входит": "PART_OF", "относится": "RELATED_TO",
}


def normalize_relation(raw: str) -> str:
    """Привести название связи от модели к каноническому коду.

    Возвращает код из онтологии либо RELATED_TO, если смысл не распознан (факт не теряем,
    но помечаем — доля RELATED_TO показывает, где задание надо уточнить).
    """
    name = (raw or "").strip().strip("`\"'")
    if not name:
        return "RELATED_TO"
    upper = name.upper().replace(" ", "_").replace("-", "_")
    if upper in SEMANTIC_CODES:
        return upper
    key = name.lower().strip()
    if key in _SYNONYMS:
        return _SYNONYMS[key]
    if upper in _SYNONYMS:
        return _SYNONYMS[upper]
    # Фразы: ищем самое длинное совпадение по вхождению
    for phrase, code in sorted(_SYNONYMS.items(), key=lambda kv: -len(kv[0])):
        if phrase and phrase in key:
            return code
    return "RELATED_TO"


def allowed_pairs(code: str) -> set[tuple[str, str]] | None:
    """Допустимые пары для типа связи. None — пары не проверяются (RELATED_TO)."""
    spec = RELATION_BY_CODE.get((code or "").upper())
    if spec is None or spec.pairs is None:
        return None
    return set(spec.pairs)


def validate_triple(src_type: str, relation: str, dst_type: str) -> tuple[bool, str]:
    """Проверить тройку «тип сущности — связь — тип сущности» перед записью в граф.

    Возвращает (можно_ли_писать, пояснение). Проверка детерминированная и бесплатная:
    она не даёт копиться связям, которых не может быть по смыслу.
    """
    code = normalize_relation(relation)
    if code not in SEMANTIC_CODES:
        return False, f"тип связи «{relation}» не из онтологии"
    if code == "RELATED_TO":
        return True, "тип не определён моделью — пишем как «связано с»"
    pairs = allowed_pairs(code)
    if pairs is None:
        return True, ""
    src = (src_type or "").strip().lower()
    dst = (dst_type or "").strip().lower()
    if (src, dst) in pairs:
        return True, ""
    return False, (f"для «{RELATION_BY_CODE[code].title}» недопустима пара "
                   f"{src or '?'} → {dst or '?'}")


def prompt_rules() -> str:
    """Правила для задания модели: какой тип связи между какими сущностями допустим.

    Это то, чего не было раньше: без списка модель придумывала типы и сваливала всё в одну мусорку.
    """
    lines = [
        "РАЗРЕШЁННЫЕ СВЯЗИ (выбирай ТОЛЬКО из этого списка; код пиши латиницей, как указано):",
    ]
    for spec in RELATIONS:
        if spec.pairs is None:
            continue
        pairs = "; ".join(f"{a} → {b}" for a, b in spec.pairs)
        lines.append(f"- {spec.code} («{spec.title}») — {spec.meaning}. Допустимо: {pairs}")
    lines += [
        f"- RELATED_TO («{RELATION_BY_CODE['RELATED_TO'].title}») — только если ни один тип выше "
        f"не подходит. Злоупотребление этим типом означает, что связь бесполезна для поиска.",
        "",
        "ТИПЫ СУЩНОСТЕЙ: " + ", ".join(f"{k} ({v})" for k, v in ENTITY_TYPES.items()),
        "",
        "ПРАВИЛА ПРОВЕРКИ (нарушение = связь не попадёт в граф):",
        "1. Тип связи — только из списка выше.",
        "2. Пара «тип источника → тип цели» должна быть среди допустимых для этого типа.",
        "3. Если подходящего типа нет — не выдумывай новый и не используй BELONGS_TO: "
        "оставь RELATED_TO.",
    ]
    return "\n".join(lines)


if __name__ == "__main__":  # быстрая самопроверка
    cases = [
        ("document_ref", "SIGNED_BY", "person"),          # да
        ("person", "SIGNED_BY", "person"),                # нет: человек человека не подписывает
        ("location", "LOCATED_AT", "location"),           # нет: место в месте не находится
        ("legal_term", "DATED", "date"),                  # нет для этого типа (см. пары)
        ("document_ref", "входит в состав", "document_ref"),   # канонизация → PART_OF
        ("document_ref", "BELONGS_TO", "legal_term"),     # мусорка → PART_OF (пары сходятся)
        ("document_ref", "придумал_сам", "person"),       # нет такого типа → RELATED_TO
    ]
    for src, rel, dst in cases:
        ok, note = validate_triple(src, rel, dst)
        print(f"{src:14s} --{rel:22s}--> {dst:14s} : {'МОЖНО' if ok else 'НЕЛЬЗЯ'} "
              f"(канон {normalize_relation(rel)}) {note}")
    print()
    print(prompt_rules()[:600], "…")
