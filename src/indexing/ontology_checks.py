"""Проверки контекста документа после записи фактов в TypeDB.

Зачем отдельный модуль: это не извлечение и не запись, а контроль того, что записалось
именно то, что задумано. Находит то, что раньше видел только человек, разбирая граф
руками: ключ документа собран из названия вместо метаданных загрузки, у версии нет даты
действия, у одного документа несколько издателей.

Все находки уходят в журнал ошибок онтологии (/errors, файл /var/log/kag/ontology).
Проверки read-only: схему и факты не меняют, обработку документа не валят.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _driver():
    from typedb.driver import Credentials, DriverOptions, DriverTlsConfig, TypeDB

    from src.indexing import typedb_facts

    c = typedb_facts._cfg()
    return TypeDB.driver(
        c["addr"], Credentials(c["user"], c["pass"]), DriverOptions(DriverTlsConfig.disabled())
    ), c["db"]


def _q(value: str) -> str:
    return (value or "").replace("\\", "\\\\").replace('"', '\\"')


def document_issuers(kag_document_id: str) -> List[Dict[str, str]]:
    """Издатели, привязанные к документу через document_issuance."""
    from typedb.driver import TransactionType

    from src.indexing import typedb_facts

    if not (kag_document_id and typedb_facts.is_available()):
        return []
    out: List[Dict[str, str]] = []
    try:
        drv, db = _driver()
        try:
            with drv.transaction(db, TransactionType.READ) as tx:
                rows = list(tx.query(
                    "match $v isa document_version, has kag_document_id "
                    f'"{_q(kag_document_id)}"; '
                    "document_has_version(version: $v, document: $d); "
                    "document_issuance(regulated_item: $d, issuer: $r); "
                    'fetch {"name": $r.name, "short": $r.short_name};'
                ).resolve())
            for r in rows:
                name = r.get("name")
                short = r.get("short")
                out.append({
                    "name": (name.get("@value") if isinstance(name, dict) else name) or "",
                    "short_name": (short.get("@value") if isinstance(short, dict) else short) or "",
                })
        finally:
            drv.close()
    except Exception as exc:  # noqa: BLE001
        logger.debug("проверки онтологии: издатели не прочитаны: %s", str(exc).splitlines()[0][:160])
    return out


def version_has_effective_from(kag_document_id: str) -> Optional[bool]:
    """Есть ли у версии дата действия. None — прочитать не удалось."""
    from typedb.driver import TransactionType

    from src.indexing import typedb_facts

    if not (kag_document_id and typedb_facts.is_available()):
        return None
    try:
        drv, db = _driver()
        try:
            with drv.transaction(db, TransactionType.READ) as tx:
                rows = list(tx.query(
                    "match $v isa document_version, has kag_document_id "
                    f'"{_q(kag_document_id)}", has effective_from $f; '
                    'fetch {"f": $f};'
                ).resolve())
            return bool(rows)
        finally:
            drv.close()
    except Exception as exc:  # noqa: BLE001
        logger.debug("проверки онтологии: дата версии не прочитана: %s", str(exc).splitlines()[0][:160])
        return None


def document_doc_type(kag_document_id: str) -> str:
    """doc_type, записанный в онтологии у документа ('' — нет или не прочитать)."""
    from typedb.driver import TransactionType

    from src.indexing import typedb_facts

    if not (kag_document_id and typedb_facts.is_available()):
        return ""
    try:
        drv, db = _driver()
        try:
            with drv.transaction(db, TransactionType.READ) as tx:
                rows = list(tx.query(
                    "match $v isa document_version, has kag_document_id "
                    f'"{_q(kag_document_id)}"; '
                    "document_has_version(version: $v, document: $d); "
                    'fetch {"kind": $d.doc_type};'
                ).resolve())
            for r in rows:
                kind = r.get("kind")
                val = kind.get("@value") if isinstance(kind, dict) else kind
                if val:
                    return str(val)
        finally:
            drv.close()
    except Exception as exc:  # noqa: BLE001
        logger.debug("проверки онтологии: doc_type не прочитан: %s", str(exc).splitlines()[0][:160])
    return ""


def check_document(
    document_id: str,
    doc_ctx: Dict[str, str],
    source_metadata: Optional[Dict[str, Any]],
    title: str = "",
) -> List[str]:
    """Проверить записанный документ и записать находки в журнал онтологии.

    Возвращает список кодов находок — чтобы вызывающий код мог попасть ими в свой лог.
    """
    from src.api.services.ontology_errors import log_error
    from src.indexing import doc_context

    sm = source_metadata if isinstance(source_metadata, dict) else {}
    found: List[str] = []
    doc_key = (doc_ctx or {}).get("document_key") or ""

    # Ожидаемые значения считаем ТЕМИ ЖЕ функциями, что собирают контекст
    # (src/indexing/doc_context.py): иначе лог пишет одно, код делает другое, и записи
    # вроде expected = «ГОСТ Р 52447— 2005» (номер вместо вида) вводят в заблуждение.
    issuer = str(sm.get("issuer") or (doc_ctx or {}).get("issuer") or "")
    number = str(sm.get("number") or (doc_ctx or {}).get("number") or "")
    expected_kind = doc_context.resolve_doc_type(
        str(sm.get("doc_kind") or ""), str((doc_ctx or {}).get("doc_type") or ""), title,
    ) or ""
    expected_key = doc_context.build_document_key(issuer, expected_kind, number, title)

    # 1. Ключ документа: «издатель|вид|номер», вид — кодом словаря.
    if expected_kind and expected_key and doc_key and doc_key != expected_key:
        found.append("key_mismatch")
        log_error(
            "document_key не совпадает с ключом из метаданных загрузки: ожидался "
            f"«{expected_key}», записан «{doc_key}». Ключ уходит в ent-key и в "
            "requirement_key, поэтому расхождение даёт вторые сущности при перезагрузке.",
            error_type="key_mismatch",
            source="writer",
            document_id=document_id,
            document_key=doc_key,
            entity_type="regulatory_document",
            details={
                "expected": expected_key,
                "expected_doc_type": expected_kind,
                "actual": doc_key,
                "metadata": {"issuer": issuer,
                             "doc_kind": str(sm.get("doc_kind") or ""),
                             "number": number,
                             "version_string": str(sm.get("version_string") or ""),
                             "effective_from": str(sm.get("effective_from") or "")},
                "code": "src/indexing/doc_context.py::build_context",
            },
        )

    # 2. Дата действия версии: у регулятора она есть в метаданных — должна дойти до TypeDB.
    eff = str(sm.get("effective_from") or "")
    if eff:
        has_eff = version_has_effective_from(document_id)
        if has_eff is False:
            found.append("cardinality_violation")
            log_error(
                f"у document_version нет effective_from, хотя в метаданных загрузки дата {eff}. "
                "Атрибут опционален в схеме, поэтому insert не падает — дата просто теряется.",
                error_type="cardinality_violation",
                source="extraction",
                document_id=document_id,
                document_key=doc_key,
                entity_type="document_version",
                details={"expected": eff, "actual": None,
                         "code": "src/api/services/document_service.py::_build_doc_context"},
            )

    # 3. Издатель: на один документ должна быть одна связь document_issuance.
    issuers = document_issuers(document_id)
    if len(issuers) > 1:
        names = sorted({(i.get("name") or i.get("short_name") or "") for i in issuers})
        found.append("key_mismatch")
        log_error(
            f"у документа {len(issuers)} связи document_issuance с разными regulator: "
            + "; ".join(n for n in names if n) + ". Дедупликации издателя по ent-key нет.",
            error_type="key_mismatch",
            source="writer",
            document_id=document_id,
            document_key=doc_key,
            entity_type="regulator",
            details={"issuers": names,
                     "code": "src/indexing/typedb_writer.py::ent_key",
                     "fix": "одна сущность regulator на издателя (ключ по short_name/name)"},
        )

    # 4. Вид документа: в онтологии должен быть КОД словаря, а не слово модели.
    from src.indexing import document_kinds

    in_ontology = document_doc_type(document_id)
    if in_ontology and not document_kinds.is_valid(in_ontology):
        found.append("enum_violation")
        if expected_kind:
            tail = f"По метаданным загрузки вид определяется как {expected_kind}."
        else:
            tail = ("Из метаданных загрузки вид не определяется: поле «Вид документа» "
                    "не заполнено кодом из словаря.")
        log_error(
            f"вид документа в онтологии = «{in_ontology}», это не код словаря. "
            f"Допустимо {len(document_kinds.codes())} кодов. {tail}",
            error_type="enum_violation",
            source="writer",
            document_id=document_id,
            document_key=doc_key,
            entity_type="document",
            details={"actual": in_ontology,
                     "expected": expected_kind,
                     "expected_source": "src/indexing/doc_context.py::resolve_doc_type",
                     "valid_codes": document_kinds.codes(),
                     "code": "src/indexing/typedb_writer.py::_inject_doc_context"},
        )

    return found
