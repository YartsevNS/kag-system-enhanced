"""
Генератор документов ИБ из онтологии TypeDB (новые вводные владельца, ADR-001 дополнение).

Ядро: по виду документа (политика/регламент/инструкция/стандарт) и области применения
(ГИС/ИСПДн/КИИ/Финансы) собирает из TypeDB «атомы» онтологии:
    Источник -> порождает -> Требование (параметры: длина, периодичность, состав) ->
    регламентирует -> Документ -> подтверждается -> Доказательство
и оформляет их в .docx (Word) / .odt-совместимый документ с доказательной базой.

Контракт эндпоинта целостен: пустая выдача (требований/доказательств нет) — не 200-OK с
полупустым документом, а честный 404 с причиной (правило kag-ui-development: эндпоинт,
отдающий файл, обязан честно отвечать об ошибке).
"""
from __future__ import annotations

import io
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    from docx import Document
    from docx.shared import Pt, Cm, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    DOCX_AVAILABLE = True
except Exception:  # pragma: no cover
    DOCX_AVAILABLE = False

# guarded import: без typedb-driver генератор выключен (не роняет api)
try:
    from typedb.driver import (  # noqa: F401
        TypeDB, Credentials, DriverOptions, DriverTlsConfig, TransactionType,
    )
    _TYPEDB_IMPORTED = True
except Exception:  # pragma: no cover
    _TYPEDB_IMPORTED = False


def _cfg() -> Dict[str, str]:
    return {
        "addr": os.getenv("TYPEDB_ADDR", "localhost:1729"),
        "db": os.getenv("TYPEDB_DB", "cybersecurity_ontology"),
        "user": os.getenv("TYPEDB_USER", "admin"),
        "pass": os.getenv("TYPEDB_PASS", ""),
    }


def is_available() -> bool:
    if not _TYPEDB_IMPORTED:
        return False
    return bool(_cfg()["pass"])


def _driver():
    from typedb.driver import TypeDB, Credentials, DriverOptions, DriverTlsConfig
    cfg = _cfg()
    return TypeDB.driver(cfg["addr"], Credentials(cfg["user"], cfg["pass"]),
                         DriverOptions(DriverTlsConfig.disabled()))


def _v(x: Any) -> Any:
    return x.get("@value") if isinstance(x, dict) else x


_KIND_LABEL = {
    "standard": "СТАНДАРТ",
    "политика": "ПОЛИТИКА",
    "policy": "ПОЛИТИКА",
    "регламент": "РЕГЛАМЕНТ",
    "regulation": "РЕГЛАМЕНТ",
    "инструкция": "ИНСТРУКЦИЯ",
    "instruction": "ИНСТРУКЦИЯ",
    "standard": "СТАНДАРТ",
}


def fetch_document_data(doc_kind: str, domain: Optional[str] = None) -> Dict[str, Any]:
    """Собрать данные документа из TypeDB: вид -> требования + источники + доказательства.

    doc_kind — doc-kind атрибут compliance-document (политика/регламент/инструкция/стандарт).
    domain — область применения (ГИС/ИСПДн/КИИ/Финансы); None = все области.
    Возвращает структуру для генератора. Пустые списки = данных нет (эндпоинт вернёт 404).
    """
    docs_by_kind: Dict[str, List[Dict[str, Any]]] = {}   # title -> список требований
    # Область применения живёт НЕ на документе, а на требовании: requirement-scope -> application-domain.
    # Без этого условия выбор «КИИ» игнорировался и выдавался любой документ нужного вида.
    domain_clause = ""
    if domain:
        domain_clause = (
            '$sc (requirement: $r, domain: $dom) isa requirement-scope; '
            f'$dom has name "{domain}"; '
        )

    # 1. Документы нужного вида + связанные требования (без union; параметры — отдельным запросом)
    q_docs = (
        'match $doc isa compliance-document, has title $dtitle, has doc-kind $dk; '
        f'$dk == "{doc_kind}"; '
        '$req (requirement: $r, document: $doc) isa requirement-regulation; '
        '$r isa security-requirement, has requirement-id $rid; '
        f'{domain_clause}'
        'fetch { "документ": $dtitle, "вид": $dk, "требование": $rid };'
    )
    try:
        with _driver() as driver:
            with driver.transaction(_cfg()["db"], TransactionType.READ) as tx:
                rows = list(tx.query(q_docs).resolve())
    except Exception as e:
        logger.warning(f"TypeDB запрос документа пропущен: {e}")
        return {"title": _KIND_LABEL.get(doc_kind, doc_kind),
                "doc_kind": doc_kind, "domain": domain or "", "documents": {}}

    visible_docs: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        dtitle = _v(row.get("документ"))
        if not dtitle:
            continue
        d = visible_docs.setdefault(dtitle, {"title": dtitle, "kind": _v(row.get("вид")) or doc_kind,
                                              "requirements": []})
        d["requirements"].append({"requirement_id": _v(row.get("требование")) or ""})

    # 1b. Параметры требований: явное `has` (в TypeDB 3.x fetch-атрибут через `$r.имя` НЕ работает).
    #     Каждый атрибут отдельным запросом (union с необязательными переменными ломается).
    param_fields = [
        # (имя ключа в dict, имя атрибута, имя переменной в fetch)
        ("length", "password-length", "длина"),
        ("period", "change-period", "периодичность"),
        ("composition", "char-composition", "состав"),
        ("obligation", "obligation-level", "обязательность"),
        ("clause", "source-clause", "пункт"),
    ]
    params_by_id: Dict[str, Dict[str, Any]] = {}
    try:
        with _driver() as driver:
            with driver.transaction(_cfg()["db"], TransactionType.READ) as tx:
                for key, attr, fname in param_fields:
                    q = (f'match $r isa security-requirement, has requirement-id $rid, '
                         f'has {attr} $val; fetch {{ "ид": $rid, "значение": $val }};')
                    for row in tx.query(q).resolve():
                        rid = _v(row.get("ид"))
                        if rid:
                            params_by_id.setdefault(rid, {})[key] = _v(row.get("значение"))
                # описание
                qd = ('match $r isa security-requirement, has requirement-id $rid, has description $val; '
                      'fetch { "ид": $rid, "значение": $val };')
                for row in tx.query(qd).resolve():
                    rid = _v(row.get("ид"))
                    if rid:
                        params_by_id.setdefault(rid, {})["description"] = _v(row.get("значение"))
    except Exception as e:
        logger.warning(f"TypeDB параметры пропущены: {e}")
    for d in visible_docs.values():
        for r in d["requirements"]:
            r.update(params_by_id.get(r["requirement_id"], {}))

    # 2. Доказательная база требований (источники + доказательства)
    #    Для каждого требования собираем его доказательства и порождающие источники.
    q_src = (
        'match $r isa security-requirement, has requirement-id $rid; '
        '$src (source: $s, requirement: $r) isa requirement-generation; $s has name $sname; '
        'fetch { "требование": $rid, "источник": $sname };'
    )
    q_ev = (
        'match $r isa security-requirement, has requirement-id $rid; '
        '$ev (requirement: $r, evidence: $e) isa requirement-evidence; $e has name $ename, has evidence-kind $ekind; '
        'fetch { "требование": $rid, "доказательство": $ename, "вид_доказ": $ekind };'
    )
    prov_by_req: Dict[str, Dict[str, Any]] = {}
    try:
        with _driver() as driver:
            with driver.transaction(_cfg()["db"], TransactionType.READ) as tx:
                rows_src = list(tx.query(q_src).resolve())
                rows_ev = list(tx.query(q_ev).resolve())
        for row in rows_src:
            rid = _v(row.get("требование"))
            if rid:
                prov_by_req.setdefault(rid, {"sources": [], "evidences": []})["sources"].append(_v(row.get("источник")))
        for row in rows_ev:
            rid = _v(row.get("требование"))
            if rid:
                prov_by_req.setdefault(rid, {"sources": [], "evidences": []})["evidences"].append(
                    {"name": _v(row.get("доказательство")), "kind": _v(row.get("вид_доказ"))})
    except Exception as e:
        logger.warning(f"TypeDB провенанс пропущен: {e}")

    # привязать провенанс к требованиям
    for dtitle, d in visible_docs.items():
        for req in d["requirements"]:
            p = prov_by_req.get(req["requirement_id"])
            req["sources"] = (p or {}).get("sources", [])
            req["evidences"] = (p or {}).get("evidences", [])

    return {"title": _KIND_LABEL.get(doc_kind, doc_kind),
            "doc_kind": doc_kind,
            "domain": domain or "",
            "documents": visible_docs}


def build_docx(data: Dict[str, Any]) -> bytes:
    """Собрать .docx из структуры онтологии (python-docx)."""
    if not DOCX_AVAILABLE:
        raise RuntimeError("python-docx не установлен")
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Times New Roman"
    style.font.size = Pt(12)

    # Титул
    title = data.get("title", "ДОКУМЕНТ")
    tp = doc.add_paragraph()
    tp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = tp.add_run(title)
    run.bold = True
    run.font.size = Pt(20)
    doc.add_paragraph(f"Дата: {datetime.now().strftime('%d.%m.%Y')}")
    if data.get("domain"):
        doc.add_paragraph(f"Область применения: {data['domain']}")

    # Основные положения / основания
    doc.add_heading("1. Общие положения", level=1)
    all_sources = set()
    for d in data.get("documents", {}).values():
        for req in d.get("requirements", []):
            all_sources.update(req.get("sources", []))
    if all_sources:
        doc.add_paragraph("Настоящий документ разработан в соответствии с:")
        for s in sorted(all_sources):
            doc.add_paragraph(f"- {s}", style="List Bullet")
    else:
        doc.add_paragraph("Требований с источниками не найдено.")

    # Требования по каждому документу (политика/инструкция/… могут включать группы)
    section = 2
    for d in data.get("documents", {}).values():
        doc.add_heading(f"{section}. Требования ({d.get('kind', '')})", level=1)
        doc.add_paragraph(f"{d.get('title', '')}.")
        # сгруппировать по параметрам — таблица требований
        tbl = doc.add_table(rows=1, cols=5)
        tbl.style = "Table Grid"
        hdr = tbl.rows[0].cells
        headers = ["Требование", "Параметр", "Значение", "Обязательность", "Пункт источника"]
        for i, h in enumerate(headers):
            hdr[i].text = h
            for par in hdr[i].paragraphs:
                for r in par.runs:
                    r.bold = True
        for req in d.get("requirements", []):
            params = [
                ("по длине", req.get("length")),
                ("период смены", req.get("period")),
                ("состав", req.get("composition")),
            ]
            for pname, pval in params:
                row = tbl.add_row().cells
                row[0].text = req.get("requirement_id", "")
                row[1].text = pname
                row[2].text = str(pval if pval is not None else "")
                row[3].text = req.get("obligation") or ""
                row[4].text = req.get("clause") or ""
        section += 1

    # Доказательная база
    doc.add_heading(f"{section}. Доказательная база", level=1)
    evidence_seen = set()
    for d in data.get("documents", {}).values():
        for req in d.get("requirements", []):
            for ev in req.get("evidences", []):
                key = ev["name"]
                if key in evidence_seen:
                    continue
                evidence_seen.add(key)
                desc = f"- {key}"
                if ev.get("kind"):
                    desc += f" ({ev['kind']})"
                doc.add_paragraph(desc, style="List Bullet")
    if not evidence_seen:
        doc.add_paragraph("Доказательная база не найдена.")

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf.getvalue()