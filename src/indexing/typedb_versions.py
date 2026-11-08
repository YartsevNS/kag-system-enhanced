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


def _number_from_title(title: str) -> str:
    """Номер акта из названия: «от 10.07.2002 N 86-ФЗ», «№ 235», «ГОСТ ISO/IEC 27014-2021».

    Зачем: при загрузке номер часто не заполняют, и ключом становилось всё название.
    Две формулировки одного и того же закона («N 86-ФЗ» и «86-ФЗ») давали два разных
    ключа — в онтологии появлялись два акта на один документ.
    """
    t = title or ""
    for pattern in (
        # «от 10.07.2002 N 86-ФЗ», «от 10.07.2002 86-ФЗ» — знак номера необязателен
        r"от\s*\d{2}\.\d{2}\.\d{4}\s*(?:№|N|No\.?)?\s*([0-9][0-9А-ЯЁA-Za-z\-/]*)",
        r"(?:№|N|No\.?)\s*([0-9][0-9А-ЯЁA-Za-z\-/]*)",
        r"(ГОСТ(?:\s+Р)?\s*(?:ISO/IEC)?\s*[0-9][0-9.\-]*)",
    ):
        m = re.search(pattern, t, re.I)
        if m:
            return m.group(1)
    return ""


def normalize_document_key(issuer: str, doc_kind: str, number: str, title: str) -> str:
    """Стабильный ключ акта: издатель + тип + номер (или название, если номера нет).

    Нормализация убирает регистр, знаки «№», кавычки, даты и лишние пробелы, поэтому
    «Приказ ФСТЭК № 239» и «приказ фстэк 239» дают один ключ. Год сюда НЕ входит:
    редакции одного акта должны сойтись на одном ключе.
    """
    num = (number or "").strip() or _number_from_title(title)
    parts = [issuer or "", doc_kind or "", num or title or ""]
    raw = "|".join(p.strip().lower() for p in parts if p and p.strip())
    raw = re.sub(r"[№\"«»'`.,;:()\[\]]", " ", raw)
    # «от 10.07.2002», «N», «No.» — служебные слова, которые меняют ключ без причины
    raw = re.sub(r"от\s*\d{2}\.\d{2}\.\d{4}", " ", raw)
    raw = re.sub(r"(?:no\.?|n)", " ", raw)
    raw = re.sub(r"\s+", " ", raw)
    raw = re.sub(r"\s*\|\s*", "|", raw)
    return raw.strip(" |")[:200]


def _q(value: str) -> str:
    """Экранирование строки для TypeQL."""
    return (value or "").replace("\\", "\\\\").replace('"', '\\"')


def is_superseded(document_id: str) -> bool:
    """Заменена ли эта редакция более новой (есть входящий supersedes в older).

    Нужна при переобработке старой редакции: Qdrant-чанки перезаписываются
    со статусом current, и без этой проверки выбывшая редакция возвращается в поиск.
    """
    c = _cfg()
    try:
        drv = TypeDB.driver(c["addr"], Credentials(c["user"], c["pass"]),
                            DriverOptions(DriverTlsConfig.disabled()))
        try:
            with drv.transaction(c["db"], TransactionType.READ) as tx:
                return bool(list(tx.query(
                    f'match $o isa document_version, has kag_document_id "{_q(document_id)}";'
                    f' $s isa supersedes (older: $o);').resolve()))
        finally:
            drv.close()
    except Exception as exc:  # noqa: BLE001
        logger.debug("versions: проверка замены не удалась: %s", str(exc)[:120])
        return False


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
    doc_key = normalize_document_key(issuer, doc_kind, str(meta.get("number") or ""), title)
    eff_to_new = eff_from

    c = _cfg()

    def _conn():
        return TypeDB.driver(c["addr"], Credentials(c["user"], c["pass"]),
                             DriverOptions(DriverTlsConfig.disabled()))

    # ------------------------------------------------------------------
    # ЭТАП 1 (чтение): найти акт. Название акта берём ЕГО, а не из метаданных:
    # writer извлекает акт из текста, и название там своё.
    # ------------------------------------------------------------------
    _number = str(meta.get("number") or "").strip()
    _cands = [
        f'match $d isa {doc_type}, has source-chunk-id $c, has title $t, has ent-key $k; '
        f'$c like "{_q(document_id)}.*"; fetch {{"t": $t, "k": $k}};',
    ]
    if _number and doc_kind:
        _cands.append(f'match $d isa {doc_type}, has number "{_q(_number)}", '
                      f'has doc_type "{_q(doc_kind)}", has title $t, has ent-key $k; '
                      f'fetch {{"t": $t, "k": $k}};')
    if doc_key and doc_key not in ("", "||"):
        # Документ компании может не дать «акта» при извлечении (политика, инструкция).
        # Тогда объект создаётся отдельно — по ключу из метаданных загрузки.
        _cands.append(f'match $d isa company_document, has document_key "{_q(doc_key)}", '
                      f'has title $t, has ent-key $k; fetch {{"t": $t, "k": $k}};')
    _cands += [
        f'match $d isa {doc_type}, has document_key "{_q(doc_key)}", has title $t, '
        f'has ent-key $k; fetch {{"t": $t, "k": $k}};',
        f'match $d isa {doc_type}, has title $t, has ent-key $k; '
        f'$t == "{_q(title)}"; fetch {{"t": $t, "k": $k}};',
    ]
    act_title = None
    act_key = None
    drv = _conn()
    try:
        with drv.transaction(c["db"], TransactionType.READ) as tx:
            for q in _cands:
                try:
                    rows = list(tx.query(q).resolve())
                except Exception:  # noqa: BLE001 — нет атрибута у типа/нет совпадений
                    continue
                for row in rows:
                    v = row.get("t")
                    act_title = v.get("@value") if isinstance(v, dict) else v
                    kk = row.get("k")
                    act_key = kk.get("@value") if isinstance(kk, dict) else kk
                    if act_title:
                        break
                if act_title:
                    break
    except Exception as exc:  # noqa: BLE001
        logger.warning("versions: поиск акта не удался: %s", str(exc).splitlines()[0][:160])
    finally:
        drv.close()

    # ------------------------------------------------------------------
    # ЭТАП 2 (запись A): сама версия и её связи. Никаких проглоченных ошибок
    # в этой транзакции: одна неудачная операция откатывает весь коммит.
    # ------------------------------------------------------------------
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

    drv = _conn()
    try:
        with drv.transaction(c["db"], TransactionType.WRITE) as tx:
            if list(tx.query(
                    f'match $v isa document_version, has ent-key "{_q(document_id)}";').resolve()):
                result["skipped"] = "version exists"
                tx.commit()
            else:
                _kind2 = source_kind or str(meta.get("source_kind") or "")
                logger.info("versions этап2: документ=%s act_key=%r kind=%r doc_key=%r title=%r",
                            document_id, act_key, _kind2, doc_key, (title or "")[:60])
                if not act_key and _kind2 == "company" and doc_key:
                    # Документ компании обязан существовать как объект онтологии,
                    # даже если извлечение не нашло в нём документа-акта: иначе
                    # политику нельзя привязать к объекту КИИ и правило не сработает.
                    _ent_txt = _ent_company = f"company_document:{doc_key}"
                    _attrs = [f'has ent-key "{_q(_ent_txt)}"',
                              f'has document_key "{_q(doc_key)}"',
                              f'has title "{_q(title)}"']
                    if first_chunk_id:
                        _attrs.append(f'has source-chunk-id "{_q(first_chunk_id)}"')
                    tx.query(f'insert $d isa company_document, {", ".join(_attrs)};').resolve()
                    act_key = _ent_txt
                    result["company_document"] = 1
                if act_key:
                    tx.query(
                        f'match $d isa {doc_type}, has ent-key "{_q(act_key)}";'
                        f' insert\n  $v isa document_version,\n    {attrs_sql};\n'
                        f'  $r isa document_version_of (version: $v, document: $d);'
                    ).resolve()
                    result["linked"] = 1
                else:
                    tx.query(f'insert\n  $v isa document_version,\n    {attrs_sql};').resolve()
                result["created"] = 1

                if supersedes_id:
                    tx.query(
                        f'match $n isa document_version, has kag_document_id "{_q(document_id)}";'
                        f' $o isa document_version, has kag_document_id "{_q(supersedes_id)}";'
                        f' insert $s isa supersedes (newer: $n, older: $o);'
                    ).resolve()
                    result["superseded"] = 1
                    if eff_from:
                        tx.query(
                            f'match $o isa document_version, has kag_document_id '
                            f'"{_q(supersedes_id)}"; not {{ $o has effective_to $e; }};'
                            f' insert $o has effective_to {eff_from};'
                        ).resolve()
                        result["effective_to"] = 1
                tx.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("versions: запись версии %s не удалась: %s",
                       document_id, str(exc)[:600], exc_info=True)
        result["skipped"] = str(exc).splitlines()[0][:160]
        result["created"] = 0
    finally:
        drv.close()

    # ------------------------------------------------------------------
    # ЭТАП 3 (запись B): издатель, тип, ключ акта. Отдельная транзакция —
    # её сбой не откатывает версию (прежняя ошибка была именно в этом).
    # ------------------------------------------------------------------
    _kind = source_kind or str(meta.get("source_kind") or "")
    logger.info("versions этап3: документ=%s kind=%r act_key=%r issuer=%r doc_kind=%r",
                document_id, _kind, act_key, issuer, doc_kind)
    if act_key and (issuer or doc_kind):
        if _kind != "regulator":
            doc_type = DOC_TYPES.get(_kind, doc_type)
        drv = _conn()
        try:
            with drv.transaction(c["db"], TransactionType.WRITE) as tx:
                if doc_kind and not list(tx.query(
                        f'match $d isa {doc_type}, has ent-key "{_q(act_key)}", '
                        f'has doc_type $dt;').resolve()):
                    tx.query(f'match $d isa {doc_type}, has ent-key "{_q(act_key)}";'
                             f' insert $d has doc_type "{_q(doc_kind)}";').resolve()
                if not list(tx.query(
                        f'match $d isa {doc_type}, has ent-key "{_q(act_key)}", '
                        f'has document_key $k2;').resolve()):
                    tx.query(f'match $d isa {doc_type}, has ent-key "{_q(act_key)}";'
                             f' insert $d has document_key "{_q(doc_key)}";').resolve()
                if issuer:
                    reg_key = _q(normalize_document_key("", "", "", issuer))
                    if not list(tx.query(
                            f'match $r isa regulator, has name "{_q(issuer)}";').resolve()):
                        tx.query(f'insert $r isa regulator, has ent-key "{reg_key}", '
                                 f'has name "{_q(issuer)}";').resolve()
                    tx.query(f'match $r isa regulator, has name "{_q(issuer)}";'
                             f' $d isa {doc_type}, has ent-key "{_q(act_key)}";'
                             f' not {{ $i isa document_issuance (issuer: $r, regulated_item: $d); }};'
                             f' insert $i isa document_issuance (issuer: $r, regulated_item: $d);'
                             ).resolve()
                    result["issuer_linked"] = 1
                tx.commit()
        except Exception as exc:  # noqa: BLE001 — не влияет на версию
            logger.warning("versions: издатель/тип для %s не записаны: %s",
                           document_id, str(exc).splitlines()[0][:200])
        finally:
            drv.close()

    # ------------------------------------------------------------------
    # ЭТАП 4 (контроль): отдельное соединение, после закрытия записи.
    # ------------------------------------------------------------------
    try:
        _chk = _conn()
        try:
            with _chk.transaction(c["db"], TransactionType.READ) as _ctx:
                result["after_commit"] = len(list(_ctx.query(
                    f'match $v isa document_version, has kag_document_id "{_q(document_id)}";'
                ).resolve()))
        finally:
            _chk.close()
        logger.info("versions: база=%s документ=%s после коммита объектов=%s", c["db"], document_id,
                    result.get("after_commit"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("versions: контрольное чтение не удалось: %s", str(exc)[:160])
    return result
