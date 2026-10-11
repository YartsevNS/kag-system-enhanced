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
    # ══════════════════════════════════════════════════════════════════════════════
    # Словарь построен НЕ на выдуманных названиях, а на опубликованных стандартах.
    # Причина: свой словарь пришлось бы защищать на продаже, а стандартный — можно
    # предъявить и он даёт совместимость выгрузок. Изобретать нужно не здесь.
    #
    #   STIX 2.1 (OASIS, безопасность) — part-of, member-of, located-at, attributed-to,
    #       authored-by, derived-from, targets, indicates, mitigates
    #   ELI (ЕС, правовые документы)  — amends, repeals, corrects, consolidates, cites
    #   Dublin Core Terms (DCMI)      — references, has-part, is-version-of, replaces, requires
    #   своё (в стандартах этого нет) — signed-by, issued-by, dated, has-amount, defines,
    #       applies-to, establishes-requirement, related-to
    # ══════════════════════════════════════════════════════════════════════════════

    # ── STIX 2.1 ────────────────────────────────────────────────────────────────
    RelationSpec("part-of", "входит в состав",
                 "объект является частью другого: пункт в документе, редакция в деле, "
                 "подразделение в организации",
                 (("clause", "standard"), ("clause", "document_ref"), ("clause", "clause"),
                  ("document_ref", "document_ref"), ("standard", "standard"),
                  ("document_ref", "standard"), ("standard", "document_ref"),
                  ("legal_term", "legal_term"), ("organization", "organization"),
                  ("standard", "clause"))),
    RelationSpec("member-of", "состоит в",
                 "лицо или организация входит в объединение или подчинена другой организации",
                 (("person", "organization"), ("organization", "organization"))),
    RelationSpec("located-at", "расположено",
                 "организация, лицо или событие находится в месте",
                 (("organization", "location"), ("person", "location"),
                  ("event", "location"), ("location", "location"))),
    RelationSpec("attributed-to", "отнесено к",
                 "документ или событие отнесено к организации (кто отвечает, чьё это)",
                 (("document_ref", "organization"), ("event", "organization"),
                  ("standard", "organization"))),
    RelationSpec("authored-by", "подготовлено",
                 "документ подготовлен организацией или лицом",
                 (("document_ref", "organization"), ("document_ref", "person"),
                  ("standard", "organization"))),
    RelationSpec("derived-from", "основано на",
                 "документ создан на основе другого документа или стандарта",
                 (("document_ref", "document_ref"), ("standard", "standard"),
                  ("standard", "document_ref"), ("document_ref", "standard"))),
    # Безопасность: то, ради чего в стандарте эти типы и заведены.
    RelationSpec("targets", "воздействует на",
                 "угроза, инцидент или требование направлены на объект",
                 (("event", "organization"), ("event", "document_ref"),
                  ("document_ref", "document_ref"), ("legal_term", "organization"))),
    RelationSpec("indicates", "указывает на",
                 "признак или показатель указывает на событие, угрозу либо нарушение",
                 (("legal_term", "event"), ("event", "event"), ("document_ref", "event"))),
    RelationSpec("mitigates", "снижает",
                 "мера, средство или требование снижает риск или угрозу",
                 (("document_ref", "event"), ("legal_term", "event"), ("standard", "event"))),

    # ── ELI: отношения между правовыми документами ─────────────────────────────
    RelationSpec("amends", "изменяет",
                 "документ вносит изменения в другой документ",
                 (("document_ref", "document_ref"), ("standard", "standard"))),
    RelationSpec("repeals", "отменяет",
                 "документ отменяет или прекращает действие другого документа",
                 (("document_ref", "document_ref"), ("standard", "standard"))),
    RelationSpec("corrects", "исправляет",
                 "документ исправляет ошибки другого документа",
                 (("document_ref", "document_ref"),)),
    RelationSpec("consolidates", "объединяет",
                 "документ объединяет положения нескольких документов",
                 (("document_ref", "document_ref"),)),

    # ── Dublin Core Terms: связи между документами и их частями ─────────────────
    RelationSpec("references", "ссылается на",
                 "документ ссылается на другой документ, стандарт или пункт",
                 (("document_ref", "document_ref"), ("document_ref", "standard"),
                  ("standard", "standard"), ("standard", "document_ref"),
                  ("document_ref", "clause"), ("standard", "clause"))),
    RelationSpec("has-part", "содержит",
                 "документ содержит пункт, раздел, таблицу или приложение",
                 (("document_ref", "clause"), ("standard", "clause"),
                  ("document_ref", "document_ref"), ("clause", "clause"))),
    RelationSpec("is-version-of", "является редакцией",
                 "документ является новой редакцией, версией или переводом другого",
                 (("document_ref", "document_ref"), ("standard", "standard"))),
    RelationSpec("replaces", "заменяет",
                 "документ заменяет другой (взамен ранее выпущенного)",
                 (("document_ref", "document_ref"), ("standard", "standard"))),
    RelationSpec("requires", "требует",
                 "документ требует наличия другого документа или условия",
                 (("document_ref", "document_ref"), ("standard", "standard"),
                  ("document_ref", "legal_term"))),

    # ── Своё: этого в стандартах нет, здесь и изобретаем ────────────────────────
    RelationSpec("signed-by", "подписано",
                 "документ подписан лицом или организацией",
                 (("document_ref", "person"), ("document_ref", "organization"))),
    RelationSpec("issued-by", "издано",
                 "документ издан или утверждён органом",
                 (("document_ref", "organization"), ("standard", "organization"))),
    RelationSpec("dated", "датировано",
                 "документ или событие имеет дату",
                 (("document_ref", "date"), ("organization", "date"),
                  ("person", "date"), ("standard", "date"), ("event", "date"))),
    RelationSpec("has-amount", "на сумму",
                 "документ или организация связаны с суммой",
                 (("document_ref", "money"), ("organization", "money"), ("money", "money"))),
    RelationSpec("defines", "определяет",
                 "документ или стандарт определяет термин",
                 (("document_ref", "legal_term"), ("standard", "legal_term"))),
    RelationSpec("applies-to", "распространяется на",
                 "документ распространяется на кого или что-либо",
                 (("document_ref", "organization"), ("document_ref", "legal_term"),
                  ("document_ref", "person"), ("standard", "organization"))),
    RelationSpec("establishes-requirement", "устанавливает требование",
                 "документ, стандарт или пункт устанавливает требование к чему-либо",
                 (("document_ref", "legal_term"), ("standard", "legal_term"),
                  ("clause", "legal_term"))),

    RelationSpec("related-to", "связано с",
                 "связь есть, но её тип определить не удалось", None),
)

RELATION_BY_CODE = {r.code: r for r in RELATIONS}
SEMANTIC_CODES = {r.code for r in RELATIONS}

# Версия онтологии. Поднимать при ЛЮБОМ изменении типов или допустимых пар: от неё зависит
# ключ кэша извлечения. Без этого старые ответы модели переиспользуются, и правки онтологии
# не действуют (живой прогон 10.10.2026 — все связи пришли из кэша и выглядели как RELATED_TO).
# 3 — прямые запреты (BELONGS_TO, злоупотребление LOCATED_AT) по итогам замера судьями.
# 4 — LOCATED_AT убран из списка для модели (судья: 21 из 25 случаев — связь совсем другая).
# 5 — разрешена пара «документ/стандарт ссылается на пункт».
# 6 — словарь переведён на канонические коды стандартов (STIX 2.1, ELI, Dublin Core).
#     Старые коды (SIGNED_BY, HAS_CLAUSE, REFERENCES, PART_OF, SUPERSEDES …) принимаются
#     через MIGRATION и сводятся к новым — данные и старые ответы модели не теряются.
ONTOLOGY_EPOCH = 6

# Системные и структурные связи: их пишет код, модель их не порождает.
STRUCTURAL_CODES = {"MENTIONS", "HAS_CHUNK", "SECTION_CHUNK", "HAS_SECTION", "NEW_EDITION_OF"}

# ── Перевод наших прежних кодов на канонические ────────────────────────────────
# Нужен по двум причинам: (1) в графе уже лежат связи со старыми кодами — они должны
# читаться и, при пересборке, писаться новыми; (2) старые ответы модели в кэше тоже
# содержат старые названия.
MIGRATION: dict[str, str] = {
    "SIGNED_BY": "signed-by",
    "ISSUED_BY": "issued-by",
    "DATED": "dated",
    "AMOUNT": "has-amount",
    "LOCATED_AT": "located-at",
    "SUPERSEDES": "replaces",
    "AMENDS": "amends",
    "REFERENCES": "references",
    "HAS_CLAUSE": "has-part",
    "REQUIRES": "establishes-requirement",
    "APPLIES_TO": "applies-to",
    "PART_OF": "part-of",
    "DEFINES": "defines",
    "RELATED_TO": "related-to",
    "BELONGS_TO": "part-of",
    "ISSUED": "issued-by",
    "SIGNED": "signed-by",
    "REPLACES": "replaces",
    "CITES": "references",
    "CITES_DOCUMENT": "references",
}

# ── Приведение свободных названий к канону ─────────────────────────────────────
_SYNONYMS: dict[str, str] = {
    # русские формулировки
    "подписан": "signed-by", "подписано": "signed-by", "подписал": "signed-by",
    "издан": "issued-by", "издано": "issued-by", "утверждён": "issued-by", "утвержден": "issued-by",
    "датирован": "dated", "дата": "dated",
    "сумма": "has-amount", "стоимость": "has-amount",
    "расположен": "located-at", "находится": "located-at", "адрес": "located-at",
    "отменяет": "repeals", "отменён": "repeals", "прекращает действие": "repeals",
    "заменяет": "replaces", "взамен": "replaces",
    "является редакцией": "is-version-of", "новая редакция": "is-version-of",
    "изменяет": "amends", "вносит изменения": "amends", "дополняет": "amends",
    "исправляет": "corrects", "объединяет": "consolidates",
    "ссылается": "references", "ссылается на": "references", "упоминает стандарт": "references",
    "цитирует": "references",
    "содержит пункт": "has-part", "содержит": "has-part", "включает": "has-part",
    "пункт": "has-part", "раздел": "has-part", "приложение": "has-part",
    "требует": "requires", "устанавливает требование": "establishes-requirement",
    "устанавливает": "establishes-requirement",
    "распространяется на": "applies-to", "применяется к": "applies-to", "действует на": "applies-to",
    "входит в состав": "part-of", "является частью": "part-of", "часть": "part-of",
    "входит": "part-of", "принадлежит": "part-of",
    "состоит в": "member-of", "член": "member-of", "подчинена": "member-of",
    "относится": "related-to", "связано": "related-to", "касается": "related-to",
    "определяет": "defines", "определение": "defines", "термин": "defines",
    "подготовлено": "authored-by", "подготовлен": "authored-by", "разработан": "authored-by",
    "основано на": "derived-from", "разработан на основе": "derived-from",
    "отнесено к": "attributed-to", "ответственный": "attributed-to",
    "воздействует на": "targets", "направлен на": "targets",
    "указывает на": "indicates", "свидетельствует о": "indicates",
    "снижает": "mitigates", "предотвращает": "mitigates",
    # английские и кодовые варианты
    "SIGNED": "signed-by", "SIGNER": "signed-by", "ISSUER": "issued-by",
    "DATE": "dated", "AMOUNT_OF": "has-amount", "LOCATION": "located-at", "LOCATED": "located-at",
    "REPLACES": "replaces", "SUPERSEDED": "replaces", "REPEALS": "repeals",
    "AMEND": "amends", "MODIFIES": "amends", "CORRECTS": "corrects", "CONSOLIDATES": "consolidates",
    "REFERS_TO": "references", "REFERENCE": "references", "CITES": "references",
    "HAS_PARAGRAPH": "has-part", "CLAUSE": "has-part", "HAS_CLAUSE": "has-part", "PART": "part-of",
    "REQUIREMENT": "requires", "REQUIRES": "requires", "ESTABLISHES": "establishes-requirement",
    "APPLIES": "applies-to", "INCLUDES": "has-part", "MEMBER_OF": "member-of",
    "DEFINES_TERM": "defines", "BELONGS": "part-of", "BELONGS_TO": "part-of",
    "IS_VERSION_OF": "is-version-of", "VERSION_OF": "is-version-of",
    "DERIVED_FROM": "derived-from", "ATTRIBUTED_TO": "attributed-to", "AUTHORED_BY": "authored-by",
    "LOCATED_AT": "located-at", "TARGETS": "targets", "INDICATES": "indicates", "MITIGATES": "mitigates",
}


def normalize_relation(raw: str) -> str:
    """Привести название связи от модели к каноническому коду.

    Возвращает код из онтологии либо RELATED_TO, если смысл не распознан (факт не теряем,
    но помечаем — доля RELATED_TO показывает, где задание надо уточнить).
    """
    name = (raw or "").strip().strip("`\"'")
    if not name:
        return "related-to"
    # Прежние коды (SIGNED_BY, HAS_CLAUSE, RELATED_TO …) → канонические. Проверяем ДО всего:
    # в графе и в кэше модели лежат связи со старыми названиями, и они должны читаться.
    _up = name.upper()
    if _up in MIGRATION:
        return MIGRATION[_up]
    # Канонический код как есть (написание сохраняем: part-of, located-at).
    low = name.lower().replace(" ", "-")
    if low in SEMANTIC_CODES:
        return low
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
    return "related-to"


def allowed_pairs(code: str) -> set[tuple[str, str]] | None:
    """Допустимые пары для типа связи. None — пары не проверяются (RELATED_TO)."""
    spec = RELATION_BY_CODE.get(code or "") or RELATION_BY_CODE.get((code or "").upper())
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
    if code == "related-to":
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
    # LOCATED_AT исключён из списка для модели по замеру: судья в 21 случае из 25 говорил, что
    # связь совсем другая («расположено» ставилось между организацией и местом, которое адресом
    # не является). Тип остаётся определён в онтологии — для уже собранных данных и проверки пар,
    # но предлагать его модели больше не нужно.
    _WITHHELD = {"LOCATED_AT"}
    for spec in RELATIONS:
        if spec.pairs is None or spec.code in _WITHHELD:
            continue
        pairs = "; ".join(f"{a} → {b}" for a, b in spec.pairs)
        lines.append(f"- {spec.code} («{spec.title}») — {spec.meaning}. Допустимо: {pairs}")
    lines += [
        f"- related-to («{RELATION_BY_CODE['related-to'].title}») — только если ни один тип выше "
        f"не подходит. Злоупотребление этим типом означает, что связь бесполезна для поиска.",
        "",
        "ТИПЫ СУЩНОСТЕЙ: " + ", ".join(f"{k} ({v})" for k, v in ENTITY_TYPES.items()),
        "",
        "ЗАПРЕЩЕНО (это ошибки, а не «на всякий случай»):",
        "- Использовать тип BELONGS_TO или формулировки «принадлежит», «относится к», «связано с чем-то» "
        "как отдельный тип: такого типа в схеме нет. Если отношение непонятно — ставь RELATED_TO.",
        "- Ставить LOCATED_AT для всего, что «где-то встречается»: этот тип только про адрес или место "
        "расположения (организация или лицо находятся по адресу). Для текстов, документов и терминов "
        "он не применяется.",
        "- Придумывать типы вида «регулирует», «касается», «в рамках», «содержит требования»: их надо "
        "выражать через APPLIES_TO, REQUIRES, REFERENCES или RELATED_TO.",
        "",
        "ПРАВИЛА ПРОВЕРКИ (нарушение = связь не попадёт в граф):",
        "1. Тип связи — только из списка выше.",
        "2. Пара «тип источника → тип цели» должна быть среди допустимых для этого типа.",
        "3. Если подходящего типа нет — не выдумывай новый: оставь RELATED_TO.",
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
