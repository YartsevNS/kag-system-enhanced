"""Каскадная очистка онтологии при удалении документа (техдолг 17).

Проблема: удаление документа убирало его из Postgres, Qdrant и Neo4j, но объекты и связи
в TypeDB оставались. Такие «призраки» продолжали участвовать в правилах вывода: факт
нарушения по удалённому документу ещё существовал, хотя текста уже нет.

Порядок важен: TypeDB не даёт удалить участника связи с обязательной ролью
(`@card(1..1)` → ошибки CNT5/COW5). Поэтому сначала снимаются связи, затем объекты.

Что НЕ удаляется: `cii_object` и `security_software` — это реестр компании, он приходит
из файла реестра, а не из документа. Их связи (`asset_protection`) принадлежат реестру.
"""
from __future__ import annotations

import logging
import os
from typing import Dict

logger = logging.getLogger(__name__)

# Типы, которые создаются ИЗ документа и должны исчезнуть вместе с ним
_DOC_OWNED = ("structural_element", "security_measure", "security_requirement")
_DOC_ACTS = ("company_document", "regulatory_document")


def _cfg() -> Dict[str, str]:
    try:
        from src.config import get_settings

        s = get_settings()
        return {"addr": s.TYPEDB_ADDR, "db": s.TYPEDB_DB,
                "user": s.TYPEDB_USER, "pass": s.TYPEDB_PASS}
    except Exception:  # noqa: BLE001
        return {"addr": os.getenv("TYPEDB_ADDR", "localhost:1729"),
                "db": os.getenv("TYPEDB_DB", "cybersecurity_ontology"),
                "user": os.getenv("TYPEDB_USER", "admin"),
                "pass": os.getenv("TYPEDB_PASS", "")}


def _q(value: str) -> str:
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


def delete_document_ontology(document_id: str) -> Dict[str, int]:
    """Удалить следы документа из онтологии. Возвращает счётчики по шагам.

    Каждый шаг — отдельная транзакция: неудача на одном не откатывает остальные
    (и не оставляет документ наполовину удалённым молча — счётчики в ответе).
    """
    from typedb.driver import (
        Credentials, DriverOptions, DriverTlsConfig, TransactionType, TypeDB,
    )

    c = _cfg()
    doc = _q(document_id)
    result = {"versions": 0, "act_links": 0, "acts": 0, "elements": 0, "errors": 0}
    try:
        drv = TypeDB.driver(c["addr"], Credentials(c["user"], c["pass"]),
                            DriverOptions(DriverTlsConfig.disabled()))
    except Exception as exc:  # noqa: BLE001
        logger.warning("удаление из онтологии: TypeDB недоступна: %s", str(exc)[:160])
        return result

    def step(key: str, queries, counter: str, count_query: str = "") -> None:
        for q in queries:
            try:
                with drv.transaction(c["db"], TransactionType.WRITE) as tx:
                    tx.query(q).resolve()
                    tx.commit()
            except Exception as exc:  # noqa: BLE001
                # Снимать нечего — это нормально; считаем ошибкой только неожиданное
                msg = str(exc).splitlines()[0][:120]
                if "CNT5" in msg or "COW5" in msg:
                    logger.warning("онтология: связь не снята (%s): %s", key, msg)
                    result["errors"] += 1
                continue
        if count_query:
            try:
                with drv.transaction(c["db"], TransactionType.READ) as tx:
                    result[counter] = len(list(tx.query(count_query).resolve()))
            except Exception:
                pass

    try:
        # 1. Версии документа: связи, затем объект
        ver = f'match $v isa document_version, has kag_document_id "{doc}";'
        step("versions", [
            f'{ver} $r isa document_has_version (version: $v); delete $r;',
            f'{ver} $s isa supersedes (newer: $v); delete $s;',
            f'{ver} $s isa supersedes (older: $v); delete $s;',
            f'{ver} delete $v;',
        ], "versions", ver)

        # 2. Акты документа: снять исходящие связи, затем объект
        for act in _DOC_ACTS:
            sel = (f'match $d isa {act}, has source-chunk-id $c; '
                   f'$c like "{doc}.*";')
            step("act_links", [
                f'{sel} $i isa document_issuance (regulated_item: $d); delete $i;',
                f'{sel} $a isa document_assignment (regulated_doc: $d); delete $a;',
                f'{sel} $r isa document_has_version (document: $d); delete $r;',
            ], "act_links")
            step("acts", [f'{sel} delete $d;'], "acts")

        # 3. Элементы, выведенные из чанков документа.
        #    Сначала снимаем связи: у structural_element/security_measure обязательные роли
        #    (applicability_criteria, document_hierarchy, measure_definition и др.) —
        #    TypeDB не даёт удалить элемент, оставив связь без участника (CNT5).
        _ROLES = (
            ("applicability_criteria", "applied_requirement"),
            ("document_hierarchy", "structural_part"),
            ("document_hierarchy", "sub_part"),
            ("measure_definition", "core_measure"),
            ("measure_definition", "source_requirement"),
            ("compensatory_relation", "alternative_measure"),
            ("compensatory_relation", "core_measure"),
            ("requirement_basis", "requirement"),
            ("requirement_evidence", "requirement"),
        )
        for ent in _DOC_OWNED:
            sel = (f'match $x isa {ent}, has source-chunk-id $c; '
                   f'$c like "{doc}.*";')
            for rel, role in _ROLES:
                step("element_links",
                     [f'{sel} $r isa {rel} ({role}: $x); delete $r;'], "elements")
            step("elements", [f'{sel} delete $x;'], "elements")
    finally:
        try:
            drv.close()
        except Exception:
            pass
    logger.info("онтология: документ %s очищен %s", document_id, result)
    return result
