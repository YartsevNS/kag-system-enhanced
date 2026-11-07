"""Версии документов в TypeDB (ADR-008).

Акт (`regulatory_document` / `company_document`) — идентичность документа.
Редакция (`document_version`) — конкретный файл: версия, дата начала действия,
статус, провенанс на чанк. Связь `document_version_of` привязывает редакцию к акту,
`supersedes` — цепочка замен.

Почему так: TypeDB не имеет встроенного темпорального моделирования (гайд:
«TypeDB has no built-in temporal model»), версии — модель в схеме, а не функция БД.

Конфиг из env (не хардкод): TYPEDB_ADDR, TYPEDB_DB, TYPEDB_USER, TYPEDB_PASS.
Без пакета/пароля контур молча выключен — обработка документа не падает.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

try:
    from typedb.driver import (  # noqa: F401
        Credentials, DriverOptions, DriverTlsConfig, TransactionType, TypeDB,
    )
    _IMPORTED = True
except Exception:  # pragma: no cover
    _IMPORTED = False

DOC_TYPES = {"regulator": "regulatory_document", "company": "company_document"}


def _cfg() -> Dict[str, str]:
    try:
        from src.config import get_settings

        s = get_settings()
        return {"addr": s.TYPEDB_ADDR, "db": s.TYPEDB_DB,
                "user": s.TYPEDB_USER, "pass": s.TYPEDB_PASS}
    except Exception:  # noqa: BLE001
        return {
            "addr": os.getenv("TYPEDB_ADDR", "localhost:1729"),
            "db": os.getenv("TYPEDB_DB", "cybersecurity_ontology"),
            "user": os.getenv("TYPEDB_USER", "admin"),
            "pass": os.getenv("TYPEDB_PASS", ""),
        }


def is_available() -> bool:
    c = _cfg()
    return bool(_IMPORTED and c.get("pass"))


def normalize_document_key(issuer: str, doc_kind: str, number: str, title: str) -> str:
    """Стабильный ключ акта: издатель + тип + номер (или название, если номера нет).

    Нормализация убирает регистр, знаки «№», кавычки и лишние пробелы, поэтому
    «Приказ ФСТЭК № 239» и «приказ фстэк 239» дают один ключ. Год сюда НЕ входит:
    редакции одного акта должны сойтись на одном ключе.
    """
    parts = [issuer or "", doc_kind or "", number or title or ""]
    raw = "|".join(p.strip().lower() for p in parts if p and p.strip())
    raw = re.sub(r"[№\"«»'`.,;:()\[\]]", " ", raw)
    raw = re.sub(r"\s+", " ", raw)
    raw = re.sub(r"\s*\|\s*", "|", raw)
    raw = raw.strip(" |")
    return raw[:200]


def _q(value: str) -> str:
    """Экранирование строки для TypeQL."""
    return (value or "").replace("\\", "\\\\").replace('"', '\\"')


def _date_literal(value: str) -> Optional[str]:
    """Дата в формате YYYY-MM-DD; иначе None (атрибут не ставим)."""
    if not value:
        return None
    v = str(value).strip()[:10]
    return v if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) else None


def upsert_from_metadata(
    document_id: str,
    title: str,
    source_kind: str,
    metadata: Optional[Dict[str, Any]],
    first_chunk_id: str = "",
) -> Dict[str, Any]:
    """Записать редакцию документа и связать её с актом.

    Идемпотентно: если версия с `ent-key = document_id` уже есть, атрибуты не
    переписываются (повторная обработка не плодит версии). Возвращает счётчики
    для лога/отчёта.
    """
    result = {"created": 0, "linked": 0, "superseded": 0, "skipped": ""}
    if not is_available():
        result["skipped"] = "typedb unavailable"
        return result
    if source_kind not in DOC_TYPES:
        result["skipped"] = f"source_kind={source_kind!r}"
        return result

    meta = metadata or {}
    doc_type = DOC_TYPES[source_kind]
    version_string = str(meta.get("version_string") or "").strip()[:120]
    issuer = str(meta.get("issuer") or "").strip()[:160]
    doc_kind = str(meta.get("doc_kind") or "").strip()[:120]
    supersedes_id = str(meta.get("supersedes_id") or "").strip()
    eff_from = _date_literal(str(meta.get("effective_from") or ""))
    doc_key = normalize_document_key(issuer, doc_kind, "", title)
    eff_to_new = eff_from

    c = _cfg()
    drv = TypeDB.driver(c["addr"], Credentials(c["user"], c["pass"]),
                        DriverOptions(DriverTlsConfig.disabled()))
    try:
        with drv.transaction(c["db"], TransactionType.WRITE) as tx:
            # 1. Версия уже есть — ничего не переписываем (идемпотентность).
            exists = list(tx.query(
                f'match $v isa document_version, has ent-key "{_q(document_id)}";'
            ).resolve())
            if exists:
                result["skipped"] = "version exists"
                tx.commit()
                return result

            # 2. Акт: ищем по ключу, затем по названию. Если не нашли — версию
            #    всё равно пишем: акт появится при извлечении и свяжется в другой раз.
            act_var = None
            for q in (
                f'match $d isa {doc_type}, has document_key $k; $k == "{_q(doc_key)}";',
                f'match $d isa {doc_type}, has title $t; $t == "{_q(title)}";',
            ):
                try:
                    if list(tx.query(q).resolve()):
                        act_var = "$d"
                        break
                except Exception:  # noqa: BLE001 — нет атрибута/нет совпадений
                    continue

            # 3. Версия.
            attrs = [
                f'has ent-key "{_q(document_id)}"',
                f'has kag_document_id "{_q(document_id)}"',
                'has version_status "current"',
            ]
            if version_string:
                attrs.append(f'has version_string "{_q(version_string)}"')
            if eff_from:
                attrs.append(f"has effective_from {eff_from}")
            if first_chunk_id:
                attrs.append(f'has source-chunk-id "{_q(first_chunk_id)}"')
            attrs_sql = ",\n    ".join(attrs)

            if act_var:
                tx.query(
                    f'match $d isa {doc_type}, has title $t; $t == "{_q(title)}";'
                    f' insert\n  $v isa document_version,\n    {attrs_sql};\n'
                    f'  $r isa document_version_of (version: $v, document: $d);'
                ).resolve()
                result["linked"] = 1
            else:
                tx.query(
                    f'insert\n  $v isa document_version,\n    {attrs_sql};'
                ).resolve()
            result["created"] = 1

            # 4. Замена прежней редакции — связью supersedes, а не правкой статусов.
            #    TypeQL 3 не даёт надёжно переписать владение атрибутом в той же
            #    транзакции (синтаксис delete/insert различается между версиями),
            #    поэтому «действующая редакция» вычисляется: версия без входящего
            #    supersedes (older). Атрибут version_status остаётся для случаев
            #    «отменён без замены» (repealed).
            if supersedes_id:
                try:
                    tx.query(
                        f'match $n isa document_version, has kag_document_id "{_q(document_id)}";'
                        f' $o isa document_version, has kag_document_id "{_q(supersedes_id)}";'
                        f' insert $s isa supersedes (newer: $n, older: $o);'
                    ).resolve()
                    result["superseded"] = 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning("versions: замена %s -> %s не применена: %s",
                                   supersedes_id, document_id, str(exc)[:800], exc_info=True)

            tx.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("versions: запись версии %s не удалась: %s",
                       document_id, str(exc)[:600], exc_info=True)
        result["skipped"] = str(exc).splitlines()[0][:160]
    finally:
        drv.close()
    return result
