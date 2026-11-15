"""Связывание документа компании с требованиями регуляторов (покрытие).

Третий проход конвейера, только для source_kind=company. После структурного извлечения
для каждого пункта документа компании модель отвечает, какие требования регулятора этот
пункт покрывает. Кандидаты проверяются по существующим requirement-id в TypeDB —
выдуманный номер не породит связь.

Результат: связь requirement_coverage(covering_document, covered_requirement) со статусом
covered; функция uncovered_documents() после этого показывает непокрытые требования.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

COVERAGE_PROMPT = "coverage_extraction"
DEFAULT_COVERAGE_PROMPT = """Ты сопоставляешь пункт документа компании с требованиями регуляторов (ФСТЭК, ФСБ, ГОСТ, Банк России). Верни только JSON.

Формат:
{"covers":[{"id":"<requirement-id требования регулятора>","evidence":"дословная цитата из пункта компании"}],"gap_reason":null}

Правила:
1. id — это номер пункта требования регулятора (например, "3.1", "ИАФ.2"), которое ЭТОТ пункт явно покрывает.
2. Если пункт не покрывает ни одного требования — верни {"covers":[],"gap_reason":"короткая причина"}.
3. Не выдумывай номера: только то, что следует из текста пункта напрямую.
4. evidence — дословная цитата из пункта компании, не пересказ.

Пункт документа компании:
{clause_text}"""


def _norm_rid(v: Any) -> str:
    s = str(v or "").strip().lower()
    s = re.sub(r"^п(ункт)?\.?\s*", "", s)
    return re.sub(r"\s+", " ", s)[:80]


async def _call(prompt: str) -> Dict[str, Any]:
    from src.indexing import typedb_writer as W

    payload = await W._call_llm(prompt, "coverage")
    return payload if isinstance(payload, dict) else {}


def _read(q: str) -> list:
    from typedb.driver import TransactionType
    from src.indexing import typedb_facts

    cfg = typedb_facts._cfg()
    with typedb_facts._driver() as driver:
        with driver.transaction(cfg["db"], TransactionType.READ) as tx:
            return list(tx.query(q).resolve())


def _write(q: str) -> None:
    from typedb.driver import TransactionType
    from src.indexing import typedb_facts

    cfg = typedb_facts._cfg()
    with typedb_facts._driver() as driver:
        with driver.transaction(cfg["db"], TransactionType.WRITE) as tx:
            tx.query(q).resolve()
            tx.commit()


def _clauses_of(document_id: str) -> List[Tuple[str, str]]:
    """Пункты документа компании: (ent-key, struct_index|text)."""
    prefix = document_id[:16]
    rows = _read(
        f'match $x isa structural_element, has source-chunk-id $c; $c like "^{prefix}"; '
        'fetch { "k": $x.ent-key, "i": $x.struct_index, "t": $x.text_content };')
    out: List[Tuple[str, str]] = []
    for r in rows:
        k = r.get("k"); i = r.get("i"); t = r.get("t")
        k = k.get("@value") if isinstance(k, dict) else k
        i = i.get("@value") if isinstance(i, dict) else i
        t = t.get("@value") if isinstance(t, dict) else t
        if k and t:
            out.append((str(k), f"{i or ''}: {t}"[:2600]))
    return out


def _rid_set() -> Set[str]:
    rows = _read('match $x isa security_requirement, has requirement-id $i; '
                 'fetch { "i": $i };')
    out: Set[str] = set()
    for r in rows:
        v = r.get("i")
        v = v.get("@value") if isinstance(v, dict) else v
        if v:
            out.add(_norm_rid(v))
    return out


def _company_doc_key(document_id: str) -> Optional[str]:
    prefix = document_id[:16]
    rows = _read(
        f'match $d isa company_document, has source-chunk-id $c; $c like "^{prefix}"; '
        'fetch { "k": $d.ent-key };')
    for r in rows:
        v = r.get("k")
        v = v.get("@value") if isinstance(v, dict) else v
        if v:
            return str(v)
    return None


def _link_exists(rid: str, doc_key: str) -> bool:
    rows = _read(
        'match $r isa security_requirement, has requirement-id $i; '
        f'$link isa requirement_coverage (covered_requirement: $r, covering_document: $d); '
        f'$d has ent-key "{doc_key}"; $i like "^' + re.escape(rid) + '$"; '
        'fetch { "k": $r.ent-key };')
    return len(rows) > 0


def _req_key_by_rid(rid: str) -> Optional[str]:
    rows = _read(
        'match $r isa security_requirement, has requirement-id $i; '
        '$i like "^' + re.escape(_norm_rid(rid)) + '$"; fetch { "k": $r.ent-key };')
    for r in rows:
        v = r.get("k")
        v = v.get("@value") if isinstance(v, dict) else v
        if v:
            return str(v)
    return None


def _template() -> str:
    try:
        from src.api.services.ontology_prompts import COVERAGE_PROMPT, prompt_store

        return prompt_store.get(COVERAGE_PROMPT)["content"]
    except Exception:  # noqa: BLE001
        return DEFAULT_COVERAGE_PROMPT


def coverage_enabled() -> bool:
    try:
        from src.api.services.config_store import config_store

        cfg = config_store.get("system", "extraction") or {}
        if isinstance(cfg, dict) and cfg.get("coverage") is not None:
            return bool(cfg["coverage"])
    except Exception as exc:  # noqa: BLE001
        logger.debug("coverage_linker: настройка не прочитана: %s", exc)
    return True


async def link_coverage(document_id: str) -> Dict[str, Any]:
    """Сопоставить пункты документа компании с требованиями и записать покрытие."""
    t0 = time.monotonic()
    clauses = _clauses_of(document_id)
    doc_key = _company_doc_key(document_id)
    report: Dict[str, Any] = {"document_id": document_id, "clauses": len(clauses),
                              "doc_key": bool(doc_key), "links": 0, "candidates": 0,
                              "hallucinated": 0, "seconds": 0.0}
    if not clauses or not doc_key:
        report["skipped"] = "нет пунктов или документа компании в базе"
        return report
    valid_rids = _rid_set()
    if not valid_rids:
        report["skipped"] = "в базе нет требований для сопоставления"
        return report
    for ent_key, clause_text in clauses:
        prompt = _template().replace("{clause_text}", clause_text)
        try:
            payload = await _call(prompt)
        except Exception as exc:  # noqa: BLE001
            logger.debug("coverage_linker: вызов модели не удался: %s", exc)
            continue
        for c in payload.get("covers") or []:
            if not isinstance(c, dict):
                continue
            rid = _norm_rid(c.get("id"))
            if not rid or rid not in valid_rids:
                report["hallucinated"] += 1
                continue
            report["candidates"] += 1
            if _link_exists(rid, doc_key):
                report["links"] += 1
                continue
            req_key = _req_key_by_rid(rid)
            if not req_key:
                continue
            try:
                _write(
                    f'match $r isa security_requirement, has ent-key "{req_key}"; '
                    f'$d isa company_document, has ent-key "{doc_key}"; '
                    'insert $link isa requirement_coverage '
                    '(covered_requirement: $r, covering_document: $d), '
                    'has coverage-status "covered";')
                report["links"] += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug("coverage_linker: связь не записана: %s", str(exc)[:120])
    report["seconds"] = round(time.monotonic() - t0, 1)
    return report
