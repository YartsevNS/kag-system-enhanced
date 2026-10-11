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
    """Тип связи: человеческое имя, смысл, допустимые пары и примеры для задания модели."""

    code: str                  # канонический код (в данных — латиницей)
    title: str                 # как показать человеку
    meaning: str               # что означает связь
    pairs: tuple[tuple[str, str], ...] | None  # допустимые пары (None — не проверять)
    good: tuple[str, ...] = ()   # положительные примеры: показывают, что сюда попадает
    bad: tuple[str, ...] = ()    # отрицательные: показывают, что сюда НЕ попадает


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
    # Только СТРУКТУРА документа. Убрано 11.10.2026 (по замеру отказов):
    # organization→organization (это member-of или subordinate-to),
    # legal_term→legal_term (это broader-term),
    # document_ref→organization (это issued-by — издатель, а не часть целого).
    RelationSpec("component-of", "структурная часть",
                 "СТРУКТУРНАЯ часть документа: пункт в документе, раздел в разделе, редакция в деле. "
                 "Не про организации и не про термины — для них member-of, subordinate-to, broader-term",
                 (("clause", "standard"), ("clause", "document_ref"), ("clause", "clause"),
                  ("document_ref", "document_ref"), ("standard", "standard"),
                  ("document_ref", "standard"), ("standard", "document_ref"),
                  ("standard", "clause")),
                 good=("пункт 5.2 component-of ГОСТ Р 57580.1", "раздел 2 component-of стандарт"),
                 bad=("ГОСТ component-of Росстандарт — это издатель (issued-by)",
                      "подразделение component-of организация — это member-of")),
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
    # ── Добавлено 11.10.2026 по итогам разбора «принадлежит» (внешняя модель + литература) ──
    RelationSpec("subordinate-to", "подчинено",
                 "организация административно подчинена другой (министерство → агентство → "
                 "подведомственная организация). Частично транзитивна",
                 (("organization", "organization"),),
                 good=("ФГУП «Стандартинформ» subordinate-to Росстандарт",
                       "территориальное управление subordinate-to министерство"),
                 bad=("Минтранс subordinate-to Правительство РФ — это часть или членство, не подчинённость",
                      "ГОСТ subordinate-to Росстандарт — у документа не бывает подчинённости, это издатель")),
    RelationSpec("broader-term", "шире по смыслу",
                 "термин шире или уже другого термина (иерархия классификаторов и предметных "
                 "областей; стандарт SKOS: broader/narrower). Заменяет ложное «входит в состав» "
                 "вида «стандарт → область знаний»",
                 (("legal_term", "legal_term"),),
                 good=("криптографическая защита информации broader-term информационная безопасность",),
                 bad=("ГОСТ Р 34.14 broader-term Информационная технология — документ не термин; "
                      "тема документа это рубрика, а не связь терминов",)),
    RelationSpec("classified-as", "отнесено к рубрике",
                 "документ отнесён к рубрике, области или классу. Это КЛАССИФИКАЦИЯ, а не смысловая "
                 "связь: нужна для поиска по рубрикам, по цепочкам не ходит",
                 (("document_ref", "legal_term"), ("standard", "legal_term")),
                 good=("ГОСТ Р 34.14 classified-as криптографическая защита информации",),
                 bad=("ГОСТ Р 34.14 classified-as Росстандарт — организация это издатель (issued-by), "
                      "а не рубрика",)),
    RelationSpec("adopted-by", "принято",
                 "документ принят или утверждён решением органа (голосование, утверждение)",
                 (("document_ref", "organization"), ("standard", "organization")),
                 good=("ГОСТ Р 34.14 adopted-by Росстандарт", "устав adopted-by совет директоров"),
                 bad=("статья adopted-by автор — статья не принимается органом",)),
    RelationSpec("in-collection", "входит в сборник",
                 "документ входит в комплект, сборник или пакет документов (приложение, дополнение, свод)",
                 (("document_ref", "document_ref"),),
                 good=("приложение № 1 in-collection приказ № 5",),
                 bad=("ГОСТ in-collection Росстандарт — это издатель, а не сборник",)),
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
    RelationSpec("effective-from", "действует с",
                 "дата вступления документа в силу. Отдельно от dated: дата подписания и дата "
                 "вступления в силу у нормативных документов различаются",
                 (("document_ref", "date"), ("standard", "date")),
                 good=("приказ effective-from 1 марта 2026 г.",),
                 bad=("приказ effective-from дата подписания — это dated",)),
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
# 8 — сведено с присланным внешним промптом: part-of переименован в component-of (структурная
#     часть), добавлен effective-from (дата вступления в силу), в задании — жёсткий разбор слова
#     «принадлежит» и требование цитаты-доказательства.
# 7 — разбор «принадлежит»: добавлены subordinate-to, broader-term (SKOS), classified-as,
#     adopted-by, in-collection; part-of сужен до структуры документа. Обоснование — замер
#     отказов и разбор литературы (Winston 1987; Keet; Wikidata P749) + ответ внешней модели.
# 6 — словарь переведён на канонические коды стандартов (STIX 2.1, ELI, Dublin Core).
#     Старые коды (SIGNED_BY, HAS_CLAUSE, REFERENCES, PART_OF, SUPERSEDES …) принимаются
#     через MIGRATION и сводятся к новым — данные и старые ответы модели не теряются.
ONTOLOGY_EPOCH = 8

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
    "PART_OF": "component-of",
    "part-of": "component-of",
    "DEFINES": "defines",
    "RELATED_TO": "related-to",
    "BELONGS_TO": "related-to",  # слипшийся тип: смысл неизвестен, точный выбирает модель
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
    "входит в состав": "component-of", "является частью": "component-of", "часть": "component-of",
    "входит": "component-of",
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
    "HAS_PARAGRAPH": "has-part", "CLAUSE": "has-part", "HAS_CLAUSE": "has-part", "PART": "component-of",
    "REQUIREMENT": "requires", "REQUIRES": "requires", "ESTABLISHES": "establishes-requirement",
    "APPLIES": "applies-to", "INCLUDES": "has-part", "MEMBER_OF": "member-of",
    "DEFINES_TERM": "defines", "BELONGS": "related-to", "BELONGS_TO": "related-to",
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
    """Правила для модели: какой тип связи между какими сущностями допустим.

    Форма задания выбрана по разбору 11.10.2026 (наша практика + ответ внешней модели + литература):
    описание типа + разрешённые пары + ПОЛОЖИТЕЛЬНЫЕ и ОТРИЦАТЕЛЬНЫЕ примеры. Голый список названий
    модель трактует вольно, а на примерах видно границу. Отдельно дано правило разбора для путаницы
    «кто издал / кто написал / чьё мнение / кто подписал» — именно здесь было больше всего ошибок.
    """
    lines = [
        "ЗАДАЧА: для каждого фрагмента текста извлечь сущности и связи МЕЖДУ НИМИ.",
        "Связь описывается только типом из списка ниже. Код пиши латиницей ровно как указано.",
        "",
        "ГЛАВНОЕ ПРАВИЛО ПРОВЕРКИ (нарушение = связь не попадёт в граф):",
        "1. Тип связи — только из списка.",
        "2. Пара «тип источника → тип цели» должна быть среди разрешённых для этого типа.",
        "3. Если подходящего типа нет — оставь related-to. Изобретать типы запрещено.",
        "",
    ]

    # Тип связи: описание, разрешённые пары, примеры.
    _WITHHELD = {"located-at"}  # убран из предложения модели по замеру 10.10.2026
    for spec in RELATIONS:
        if spec.code in _WITHHELD:
            continue
        pairs = ("пары не проверяются" if spec.pairs is None
                 else "; ".join(f"{a} → {b}" for a, b in spec.pairs))
        lines.append(f"{spec.code} («{spec.title}») — {spec.meaning}")
        lines.append(f"   допустимо: {pairs}")
        for g in spec.good:
            lines.append(f"   верно: {g}")
        for b in spec.bad:
            lines.append(f"   НЕВЕРНО: {b}")
        lines.append("")

    lines += [
        "РАЗБОР ПУТАНИЦЫ «кто причастен к документу» — отвечай на вопрос по очереди:",
        "   кто дал документу юридическую силу (официально выпустил, издал)? → issued-by",
        "   кто утвердил или принял решением (голосование, постановление)? → adopted-by",
        "   кто подписал (должностное лицо, подпись)? → signed-by",
        "   кто написал или разработал текст (автор, рабочая группа, институт)? → authored-by",
        "   чьё мнение или чья работа упомянута в тексте? → attributed-to",
        "   организация подчинена другой? → subordinate-to. Лицо или организация ВХОДИТ в объединение? → member-of",
        "ВНИМАНИЕ: издатель, автор, подписант и принявший орган — это РАЗНЫЕ связи, не одна.",
        "",
        "СЛОВО «ПРИНАДЛЕЖИТ» — ПОД ЗАПРЕТОМ. Если в тексте написано «принадлежит», «относится»,",
        "«входит в» — это НЕ готовый тип связи, а приглашение подумать. Выбери по смыслу:",
        "   структурная часть текста → component-of;   членство в коллективе → member-of;",
        "   подчинённость организации → subordinate-to;   входит в сборник → in-collection;",
        "   организация выпустила документ → issued-by;   тематика/рубрика → classified-as.",
        "",
        "ЧЕГО НЕ ДЕЛАТЬ (частые ошибки на нашем корпусе):",
        "   не связывай документ с организацией как с частью целого: частью документа организация не бывает;",
        "   не связывай документ с предметной областью («Информационная технология», «Криптографическая",
        "   защита информации») — для этого есть classified-as, и это рубрика, а не смысловое отношение;",
        "   не обозначай часть-целое словом «принадлежит» — выбери component-of, member-of,",
        "   subordinate-to или in-collection по смыслу;",
        "   не ставь has-amount для номера документа (№ 1070-ст) — это номер, а не сумма;",
        "   не ставь dated термину или понятию — датируют документ или событие.",
        "",
        "ТИПЫ СУЩНОСТЕЙ: " + ", ".join(f"{k} ({v})" for k, v in ENTITY_TYPES.items()),
        "",
        "ПОРЯДОК РАБОТЫ (сначала решай, потом отвечай):",
        "   шаг 1 — найди сущности и определи их типы;",
        "   шаг 2 — для каждой возможной пары сначала назови вопрос из разбора выше и реши его;",
        "   шаг 3 — выбери код из списка и проверь пару по разрешённым;",
        "   шаг 4 — выдай JSON.",
        "",
        "ОБЯЗАТЕЛЬНОЕ ПОЛЕ evidence: для каждой связи приведи КОРОТКУЮ ЦИТАТУ из текста, на которой она",
        "основана (до 12 слов). Связь без цитаты считается выдуманной и будет отброшена. Это правило",
        "существует потому, что каждая связь в графе должна быть проверяема по источнику.",
        "В поле relation_reasoning — одна короткая фраза, почему выбран тип (для разбора, в ответы",
        "пользователю не попадает).",
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
