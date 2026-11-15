"""Живая схема онтологии: что реально собрано в ядре и модулях.

Задача страницы `/ontology/schema` — показать факт, а не замысел:
какие типы объявлены в `core.tql` и `modules/*.tql`, какие атрибуты и роли у них есть,
загружены ли эти типы в базу TypeDB и как наш состав соотносится с внешним списком
сущностей (`docs/ontology/primer/`).

Источник структуры — файлы схемы (они же попадают в образ), источник подтверждения —
сама база: если тип объявлен в файле, но в базе его нет, страница обязана это показать.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Русские пояснения: что означает тип (страница должна читаться без словаря) ──
EXPLAIN = {
    # ядро — родовые понятия
    "identified_entity": "любой объект с постоянным ключом (основа всех типов)",
    "sourced_entity": "объект с происхождением: чанк, пункт, документ-источник",
    "document": "документ: закон, приказ, стандарт или документ компании",
    "version": "редакция документа (версия со статусом и хешем текста)",
    "clause": "структурный элемент документа: раздел, пункт, приложение",
    "requirement": "требование: что должно быть сделано",
    "control": "мера или контроль: чем требование выполняется",
    "evidence": "доказательство выполнения: сертификат, акт, протокол",
    "party": "субъект: организация, орган, человек, роль-исполнитель",
    "asset": "актив: система, объект, оборудование, ПО, услуга",
    "data_object": "объект данных: набор сведений, обрабатываемый системой",
    "data_category": "категория данных: ПДн, КТ, ГТ, коммерческая тайна",
    "threat": "угроза: источник или событие, способное нанести ущерб",
    "vulnerability": "уязвимость: слабое место, через которое действует угроза",
    "risk": "риск: сочетание угрозы, уязвимости и ценности актива",
    "incident": "инцидент: произошедшее событие с ущербом",
    "violation": "нарушение: невыполненное требование или отсутствующая мера",
    "term": "термин с определением (словарная статья в онтологии)",
    "business_function": "функция: что организация делает (оценка рисков, аудит)",
    "business_process": "процесс: как функция выполняется",
    "actor_role": "роль исполнителя: кто отвечает за действие",
    # связи ядра
    "document_issuance": "документ издан органом (регулятором)",
    "document_has_version": "у документа есть редакция",
    "current_version_of": "редакция действует сейчас",
    "supersedes": "редакция заменяет предыдущую",
    "document_structure": "редакция содержит структурный элемент",
    "document_hierarchy": "элемент вложен в другой элемент (раздел → пункт)",
    "requirement_derived_from": "требование выведено из пункта документа",
    "requirement_definition": "мера определяет требование (что именно выполняется)",
    "measure_source": "мера описана в пункте документа",
    "measure_detailing": "подмера детализирует меру (АВЗ.1.1 → АВЗ.1)",
    "compensatory_relation": "одна мера компенсирует другую",
    "requirement_applicability": "требование относится к объекту",
    "measure_applicability": "мера применяется к объекту",
    "requirement_evidence": "требование подтверждается доказательством",
    "requirement_basis": "требование опирается на документ-основание",
    "requirement_coverage": "документ компании покрывает требование",
    "document_compliance": "документ компании соответствует нормативному",
    "document_assignment": "документ распространяется на субъект",
    "alignment": "соответствие между доменами (мера ИБ ↔ ИТ-контроль)",
    "asset_ownership": "у актива есть владелец",
    "asset_part_of": "актив входит в состав другого актива",
    "data_processing": "субъект обрабатывает данные",
    "data_classification": "данные отнесены к категории",
    "responsibility": "субъект отвечает за требование, меру или процесс",
    "risk_composition": "из чего сложился риск: угроза, уязвимость, актив",
    "threat_exploits": "угроза использует уязвимость",
    "threat_target": "угроза направлена на актив",
    "incident_impact": "инцидент затронул актив",
    "function_execution": "функцию выполняет субъект",
    "document_reference": "документ ссылается на другой документ",
    "control_mitigation": "мера снижает угрозу",
    # домен ИБ
    "regulator": "регулятор: ФСТЭК, ФСБ, Банк России, Минцифры",
    "regulatory_document": "нормативный документ регулятора",
    "company_document": "документ компании (политика, приказ, регламент)",
    "document_version": "редакция нормативного документа",
    "structural_element": "пункт нормативного документа",
    "security_requirement": "требование информационной безопасности",
    "security_measure": "мера защиты информации (АВЗ.1, ЗНИ.2)",
    "cii_object": "объект критической информационной инфраструктуры",
    "security_software": "средство защиты информации",
    "certificate": "сертификат соответствия на СЗИ",
    "threat_source": "источник угрозы: класс нарушителя и его возможности",
    "certification": "сертификат подтверждает соответствие СЗИ требованию",
    "asset_protection": "СЗИ защищает объект",
    "threat_applicability": "угроза актуальна для объекта",
    "threat_measure": "мера противодействует угрозе",
    # домен ИТ
    "it_document": "документ домена ИТ (регламент, инструкция)",
    "it_standard": "стандарт домена ИТ (например, ISO 27001, Cobit)",
    "it_requirement": "требование ИТ-контура",
    "it_control": "ИТ-контроль (AC-2, AU-6)",
    "it_service": "ИТ-услуга",
    "it_asset": "ИТ-актив: сервер, сеть, рабочая станция",
    "it_configuration_item": "единица конфигурации (CI) в ITSM",
}

ONTOLOGY_DIR = Path(__file__).resolve().parents[3] / "src" / "ontology"
MODULES = [("core", ONTOLOGY_DIR / "core.tql")]
for _p in sorted((ONTOLOGY_DIR / "modules").glob("*.tql")):
    MODULES.append((_p.stem, _p))

ANNOT_RE = re.compile(r"@([a-z]+)(?:\(([^)]*)\))?")
DECL_RE = re.compile(r"^(attribute|entity|relation)\s+([A-Za-z0-9_-]+)")
OWN_RE = re.compile(r"\bowns\s+([A-Za-z0-9_-]+)((?:\s+@[a-z]+(?:\([^)]*\))?)*)")
REL_RE = re.compile(r"\brelates\s+([A-Za-z0-9_-]+)((?:\s+@[a-z]+(?:\([^)]*\))?)*)")
PLAY_RE = re.compile(r"\bplays\s+([A-Za-z0-9_-]+):([A-Za-z0-9_-]+)")
SUB_RE = re.compile(r"\bsub\s+([A-Za-z0-9_-]+)")
VALUE_RE = re.compile(r"value\s+([A-Za-z0-9_-]+)")


def _annotations(tail: str) -> Dict[str, str]:
    """@key / @abstract / @card(1..1) → {key: '', abstract: '', card: '1..1'}."""
    out: Dict[str, str] = {}
    for name, arg in ANNOT_RE.findall(tail or ""):
        out[name] = arg or ""
    return out


def _statements(text: str) -> List[str]:
    """Операторы .tql: режем по ';', комментарии `# ...` убираем построчно."""
    lines = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0]
        if line.strip():
            lines.append(line)
    body = " ".join(lines)
    return [" ".join(s.split()) for s in body.split(";") if s.strip()]


def parse_module(name: str, path: Path) -> Dict[str, Any]:
    """Разобрать файл модуля в структуру: атрибуты, сущности, связи.

    Один тип может объявляться несколькими операторами (`entity x @abstract;`,
    `entity x sub y, owns ...;`, `entity x plays rel:role;`) — записи сливаются по имени,
    иначе на странице будет три «document» и ни одного с атрибутами.
    """
    if not path.exists():
        return {"name": name, "file": path.name, "missing_file": True,
                "entities": [], "relations": [], "attributes": [], "error": "файл не найден"}
    text = path.read_text(encoding="utf-8")

    entities: Dict[str, Dict[str, Any]] = {}
    relations: Dict[str, Dict[str, Any]] = {}
    attributes: List[Dict[str, Any]] = []

    def ent(label: str) -> Dict[str, Any]:
        return entities.setdefault(label, {"label": label,
                                           "ru": EXPLAIN.get(label, ""),
                                           "abstract": False,
                                           "super": None, "owns": [], "plays": []})

    def rel(label: str) -> Dict[str, Any]:
        return relations.setdefault(label, {"label": label,
                                            "ru": EXPLAIN.get(label, ""),
                                            "abstract": False,
                                            "super": None, "owns": [], "relates": []})

    role_players: Dict[str, List[str]] = {}

    for st in _statements(text):
        m = DECL_RE.match(st)
        if not m:
            continue
        kind, label = m.group(1), m.group(2)
        rest = st[m.end():]
        if kind == "attribute":
            v = VALUE_RE.search(rest)
            attributes.append({"label": label, "value": v.group(1) if v else "string"})
            continue
        # `entity X plays rel:role;` — подписка роли для типа, объявленного В ДРУГОМ МОДУЛЕ
        # (например, asset играет роль в связи модуля sec). Это не новый тип модуля.
        plays_only = (kind == "entity" and PLAY_RE.search(rest) and not OWN_RE.search(rest)
                      and not SUB_RE.search(rest) and "@abstract" not in rest)
        if plays_only:
            role_players.setdefault(label, []).extend(
                f"{r}:{ro}" for r, ro in PLAY_RE.findall(rest))
            continue
        ann = _annotations(rest)
        sup = SUB_RE.search(rest)
        item = ent(label) if kind == "entity" else rel(label)
        item["abstract"] = item["abstract"] or ("abstract" in ann)
        if sup and not item["super"]:
            item["super"] = sup.group(1)
        plays = [f"{r}:{ro}" for r, ro in PLAY_RE.findall(rest)]
        if plays:
            item.setdefault("plays", [])
            item["plays"] += [p for p in plays if p not in item["plays"]]
        if kind == "relation":
            for role, tail in REL_RE.findall(rest):
                a = _annotations(tail)
                item["relates"].append({"role": role, "card": a.get("card", "")})
        for attr, tail in OWN_RE.findall(rest):
            a = _annotations(tail)
            entry = {"attr": attr, "card": a.get("card", ""),
                     "key": "key" in a, "unique": "unique" in a}
            if entry not in item["owns"]:
                item["owns"].append(entry)

    # подписки ролей, у которых тип объявлен в другом модуле: показываем отдельной строкой
    foreign_players = {k: sorted(set(v)) for k, v in role_players.items() if k not in entities}
    return {"name": name, "file": path.name, "missing_file": False,
            "entities": list(entities.values()), "relations": list(relations.values()),
            "attributes": attributes, "role_players": foreign_players}


def _live(db: Optional[str]) -> Dict[str, Any]:
    """Что из объявленного реально лежит в базе."""
    from src.indexing import typedb_facts

    if not typedb_facts.is_available():
        return {"ok": False, "reason": "TypeDB не настроен (нет пакета или пароля)", "labels": {}}
    cfg = typedb_facts._cfg()
    target = db or cfg["db"]
    try:
        from typedb.driver import TransactionType

        out: Dict[str, str] = {}
        with typedb_facts._driver() as driver:
            with driver.transaction(target, TransactionType.READ) as tx:
                rows = list(tx.query('match $t sub $r; fetch { "t": $t };').resolve())
        for row in rows:
            t = row.get("t") or {}
            if not isinstance(t, dict):
                continue
            label = t.get("label")
            if not label:
                continue
            kind = t.get("kind") or ""
            if "role" in kind:
                continue
            out[label] = kind or "type"
        return {"ok": True, "db": target, "labels": out}
    except Exception as exc:  # noqa: BLE001
        logger.warning("schema_view: база %s недоступна: %s", target, exc)
        return {"ok": False, "db": target, "reason": str(exc)[:200], "labels": {}}


# ── Сверка с внешним списком сущностей (docs/ontology/primer/) ──────────────
# Источник: ответ модели на запрос «онтология ИБ/ИТ по требованиям РФ».
# Статус: есть | частично | нет. «Частично» — понятие покрыто другим типом или подтипом.
PRIMER_ENTITY_ROWS = [
    ("document", "есть", "ядро `document` + regulatory_document, company_document, it_document, it_standard"),
    ("requirement", "есть", "ядро `requirement` + security_requirement, it_requirement"),
    ("control", "есть", "ядро `control` + security_measure, it_control"),
    ("asset", "есть", "ядро `asset` (asset_kind, criticality) + cii_object, security_software, it_asset"),
    ("subject", "есть", "ядро `party` (party_kind) + regulator; исполнители — `actor_role`"),
    ("data_category", "есть", "ядро `data_category` и `data_object` (data_kind)"),
    ("threat", "есть", "ядро `threat` (threat_kind) + threat_source (класс нарушителя)"),
    ("vulnerability", "есть", "ядро `vulnerability` + признаки БДУ: bdu-id, cvss-score, severity-level"),
    ("risk", "есть", "ядро `risk` (risk_level, likelihood, impact)"),
    ("incident", "есть", "ядро `incident` (incident_date, incident_kind)"),
    ("violation", "есть", "ядро `violation` (violation_kind); вывод uncovered_documents остаётся"),
    ("certification", "есть", "`certificate` — подтип evidence; связь `certification`"),
    ("evidence", "есть", "ядро `evidence` (evidence_kind, evidence_date)"),
    ("term", "есть", "ядро `term` с атрибутом definition; словарь /terms остаётся для очереди пар"),
    ("function", "есть", "ядро `business_function`"),
    ("process", "есть", "ядро `business_process`; ИТ-услуга — it_service"),
    ("role", "есть", "ядро `actor_role` (роль исполнителя, отдельно от ролей TypeDB)"),
]

PRIMER_RELATION_ROWS = [
    ("requirement_source", "есть", "разложена на три: requirement_derived_from, measure_source, document_structure"),
    ("requirement_applicability", "есть", "ядро `requirement_applicability`"),
    ("requirement_satisfaction", "есть", "requirement_coverage + control: мера определяет требование"),
    ("control_mitigation", "есть", "ядро `control_mitigation` (мера снижает угрозу)"),
    ("threat_target", "есть", "ядро `threat_target` (угроза → актив)"),
    ("threat_exploits", "есть", "ядро `threat_exploits` (угроза → уязвимость)"),
    ("risk_composition", "есть", "ядро `risk_composition` (риск, угроза, уязвимость, актив)"),
    ("asset_ownership", "есть", "ядро `asset_ownership` (владелец → актив)"),
    ("asset_part_of", "есть", "ядро `asset_part_of` (часть → целое)"),
    ("data_processing", "есть", "ядро `data_processing` (субъект → данные)"),
    ("data_classification", "есть", "ядро `data_classification` (данные → категория)"),
    ("responsibility", "есть", "ядро `responsibility` (субъект → требование, мера, процесс)"),
    ("document_reference", "есть", "ядро `document_reference` (документ → документ)"),
    ("document_supersedes", "есть", "ядро `supersedes` (редакция → редакция)"),
    ("compliance_assessment", "есть", "document_compliance + requirement_evidence (оценка = документ + доказательство)"),
    ("certification_confirms", "есть", "`certification` (ПО → сертификат → требование-основание)"),
    ("incident_impact", "есть", "ядро `incident_impact` (инцидент, актив, угроза)"),
    ("function_execution", "есть", "ядро `function_execution` (функция, исполнитель, мера)"),
]


def _databases() -> List[str]:
    """Какие базы есть на сервере TypeDB — чтобы страница могла показать любую."""
    from src.indexing import typedb_facts

    if not typedb_facts.is_available():
        return []
    try:
        with typedb_facts._driver() as driver:
            names = [d.name for d in driver.databases.all()]

        def _ver(n: str) -> int:
            tail = n.rsplit("_v", 1)[-1]
            return int(tail) if tail.isdigit() else 0

        return sorted((n for n in names if "ontology" in n), key=_ver, reverse=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("schema_view: список баз недоступен: %s", exc)
        return []


def build_view(db: Optional[str] = None) -> Dict[str, Any]:
    """Собрать полную картину: модули, счётчики, подтверждение из базы, сверка."""
    modules = [parse_module(name, path) for name, path in MODULES]
    live = _live(db)
    labels = live.get("labels") or {}

    declared: List[str] = []
    for m in modules:
        declared += [e["label"] for e in m["entities"]]
        declared += [r["label"] for r in m["relations"]]
        declared += [a["label"] for a in m["attributes"]]
    missing = sorted({x for x in declared if labels and x not in labels})

    totals = {
        "entities": sum(len(m["entities"]) for m in modules),
        "relations": sum(len(m["relations"]) for m in modules),
        "attributes": sum(len(m["attributes"]) for m in modules),
        "roles": sum(len(r.get("relates") or []) for m in modules for r in m["relations"]),
        "plays": sum(len(e.get("plays") or []) for m in modules for e in m["entities"]),
        "in_db": len(labels),
    }
    return {
        "modules": modules,
        "totals": totals,
        "databases": _databases(),
        "live": {
            "ok": live.get("ok", False),
            "db": live.get("db"),
            "reason": live.get("reason"),
            "missing": missing,
            "in_db": len(labels),
        },
        "primer": {
            "entities": [{"name": n, "status": s, "note": t} for n, s, t in PRIMER_ENTITY_ROWS],
            "relations": [{"name": n, "status": s, "note": t} for n, s, t in PRIMER_RELATION_ROWS],
            "counts": {
                "primer_entities": len(PRIMER_ENTITY_ROWS),
                "primer_relations": len(PRIMER_RELATION_ROWS),
                "have": sum(1 for _, s, _ in PRIMER_ENTITY_ROWS if s == "есть"),
                "partial": sum(1 for _, s, _ in PRIMER_ENTITY_ROWS if s == "частично"),
                "absent": sum(1 for _, s, _ in PRIMER_ENTITY_ROWS if s == "нет"),
                "rel_have": sum(1 for _, s, _ in PRIMER_RELATION_ROWS if s == "есть"),
                "rel_partial": sum(1 for _, s, _ in PRIMER_RELATION_ROWS if s == "частично"),
                "rel_absent": sum(1 for _, s, _ in PRIMER_RELATION_ROWS if s == "нет"),
                "absent_entities": [n for n, s, _ in PRIMER_ENTITY_ROWS if s == "нет"],
                "absent_relations": [n for n, s, _ in PRIMER_RELATION_ROWS if s == "нет"],
            },
        },
    }
