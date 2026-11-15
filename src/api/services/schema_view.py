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
        return entities.setdefault(label, {"label": label, "abstract": False,
                                           "super": None, "owns": [], "plays": []})

    def rel(label: str) -> Dict[str, Any]:
        return relations.setdefault(label, {"label": label, "abstract": False,
                                            "super": None, "owns": [], "relates": []})

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

    return {"name": name, "file": path.name, "missing_file": False,
            "entities": list(entities.values()), "relations": list(relations.values()),
            "attributes": attributes}


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
    ("document", "есть", "ядро `document` + подтипы: regulatory_document, company_document, it_document, it_standard"),
    ("requirement", "есть", "ядро `requirement` + security_requirement, it_requirement"),
    ("control", "есть", "ядро `control` + security_measure, it_control"),
    ("asset", "частично", "cii_object, security_software, it_asset, it_service — общей сущности «актив» нет"),
    ("subject", "частично", "regulator; организаций, людей, операторов, ролей-исполнителей нет"),
    ("data_category", "нет", "категорий данных (ПДн, КТ, ГТ, общедоступные) нет"),
    ("threat", "частично", "threat_source (источник угрозы); общей сущности «угроза» нет"),
    ("vulnerability", "есть", "vulnerability + bdu-id, cvss-score, severity-level"),
    ("risk", "нет", "риска как сущности нет"),
    ("incident", "нет", "инцидентов нет"),
    ("violation", "нет", "нарушений как сущностей нет (есть вывод uncovered_documents)"),
    ("certification", "есть", "certificate + связь certification"),
    ("evidence", "есть", "ядро `evidence` (cert/документ/акт) без доменных подтипов"),
    ("term", "нет", "термины живут в Postgres (словарь /terms), не в онтологии"),
    ("function", "нет", "функций нет"),
    ("process", "частично", "it_service; процессов ИБ нет"),
    ("role", "нет", "ролей-исполнителей нет (роли TypeDB — другое)"),
]

PRIMER_RELATION_ROWS = [
    ("requirement_source", "частично", "requirement_definition, measure_source, document_structure"),
    ("requirement_applicability", "есть", "ядро `requirement_applicability`"),
    ("requirement_satisfaction", "частично", "requirement_coverage (покрытие документом компании)"),
    ("control_mitigation", "частично", "threat_measure (мера против угрозы)"),
    ("threat_target", "частично", "threat_applicability (угроза → объект КИИ)"),
    ("threat_exploits", "нет", "связи «угроза использует уязвимость» нет"),
    ("risk_composition", "нет", "рисков нет"),
    ("asset_ownership", "нет", "владельцев активов нет"),
    ("asset_part_of", "нет", "состава активов нет"),
    ("data_processing", "нет", "обработки данных нет"),
    ("data_classification", "нет", "классификации данных нет"),
    ("responsibility", "нет", "ответственности нет"),
    ("document_reference", "нет", "ссылок между документами нет (есть requirement_basis)"),
    ("document_supersedes", "есть", "ядро `supersedes` (версия → версия)"),
    ("compliance_assessment", "частично", "document_compliance (документ ↔ требование)"),
    ("certification_confirms", "частично", "certification (сертификат ↔ ПО ↔ требование-основание)"),
    ("incident_impact", "нет", "инцидентов нет"),
    ("function_execution", "нет", "функций нет"),
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
            },
        },
    }
