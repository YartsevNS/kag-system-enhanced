"""Реестр данных компании: объекты КИИ, средства защиты, требования, привязка документов.

Зачем: правила онтологии (`violated_certification`, `violated_documents`) требуют связки
«объект КИИ + развёрнутое СЗИ + требование о сертификации + политика компании». В корпусе
нормативки этих данных нет — они приходят от заказчика списком. Модуль загружает такой
список в TypeDB идемпотентно (повторный запуск не создаёт дублей).

Формат файла (JSON):

    {
      "objects": [
        {"key": "asu-tp-1", "name": "АСУ ТП цеха № 1", "category": "1"},
        {"key": "is-2", "name": "ИС персональных данных", "category": "2"}
      ],
      "software": [
        {"key": "kaspersky-ksc", "name": "Kaspersky Endpoint Security", "certified": false}
      ],
      "protects": [{"system": "asu-tp-1", "software": "kaspersky-ksc"}],
      "requirements": [
        {"key": "fstec-239-p11", "requirement_id": "п. 11.1", "kind": "certification",
         "obligation": "обязательное", "description": "Применять сертифицированные СЗИ"}
      ],
      "documents": [
        {"document_key": "внутренняя|политика|", "subject": "asu-tp-1", "title": "Политика ИБ"}
      ]
    }
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)


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


def _q(value: Any) -> str:
    """Экранирование строки для TypeQL."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _ent(prefix: str, key: str) -> str:
    return f"{prefix}:{key}"


class CompanyRegistry:
    """Загрузка реестра компании в онтологию."""

    def __init__(self) -> None:
        self.cfg = _cfg()

    # ------------------------------------------------------------------ объекты
    def upsert_object(self, tx, item: Dict[str, Any]) -> bool:
        key = str(item["key"]).strip()
        ent = _ent("cii_object", key)
        if list(tx.query(f'match $x isa cii_object, has ent-key "{_q(ent)}";').resolve()):
            return False
        attrs = [f'has ent-key "{_q(ent)}"', f'has name "{_q(item.get("name") or key)}"']
        if item.get("category"):
            attrs.append(f'has cii-category {int(item["category"])}')
        if item.get("source_origin"):
            attrs.append(f'has source_origin "{_q(item["source_origin"])}"')
        tx.query(f'insert $x isa cii_object, {", ".join(attrs)};').resolve()
        return True

    # ------------------------------------------------------------------ СЗИ
    def upsert_software(self, tx, item: Dict[str, Any]) -> bool:
        key = str(item["key"]).strip()
        ent = _ent("security_software", key)
        if list(tx.query(f'match $x isa security_software, has ent-key "{_q(ent)}";').resolve()):
            return False
        certified = "true" if item.get("certified") else "false"
        attrs = [f'has ent-key "{_q(ent)}"', f'has name "{_q(item.get("name") or key)}"',
                 f'has is-certified {certified}']
        if item.get("source_origin"):
            attrs.append(f'has source_origin "{_q(item["source_origin"])}"')
        tx.query(f'insert $x isa security_software, {", ".join(attrs)};').resolve()
        return True

    # ------------------------------------------------------------------ требования
    def upsert_requirement(self, tx, item: Dict[str, Any]) -> bool:
        key = str(item["key"]).strip()
        ent = _ent("security_requirement", key)
        if list(tx.query(f'match $x isa security_requirement, has ent-key "{_q(ent)}";').resolve()):
            return False
        attrs = [f'has ent-key "{_q(ent)}"']
        if item.get("requirement_id"):
            attrs.append(f'has requirement-id "{_q(item["requirement_id"])}"')
        if item.get("kind"):
            attrs.append(f'has requirement_kind "{_q(item["kind"])}"')
        if item.get("obligation"):
            attrs.append(f'has obligation-level "{_q(item["obligation"])}"')
        if item.get("description"):
            attrs.append(f'has description "{_q(item["description"])}"')
        if item.get("source_origin"):
            attrs.append(f'has source_origin "{_q(item["source_origin"])}"')
        tx.query(f'insert $x isa security_requirement, {", ".join(attrs)};').resolve()
        return True

    # ------------------------------------------------------------------ связи
    def link_protection(self, tx, system_key: str, software_key: str) -> bool:
        sys_ent = _q(_ent("cii_object", system_key))
        sw_ent = _q(_ent("security_software", software_key))
        if list(tx.query(
                f'match $a isa cii_object, has ent-key "{sys_ent}";'
                f' $s isa security_software, has ent-key "{sw_ent}";'
                f' $r isa asset_protection (protected_system: $a, deployed_szi: $s);').resolve()):
            return False
        tx.query(
            f'match $a isa cii_object, has ent-key "{sys_ent}";'
            f' $s isa security_software, has ent-key "{sw_ent}";'
            f' insert $r isa asset_protection (protected_system: $a, deployed_szi: $s);'
        ).resolve()
        return True

    def link_document(self, tx, document_key: str, subject_key: str) -> bool:
        doc_key = _q(document_key)
        sub_ent = _q(_ent("cii_object", subject_key))
        if list(tx.query(
                f'match $d isa company_document, has document_key "{doc_key}";'
                f' $a isa cii_object, has ent-key "{sub_ent}";'
                f' $r isa document_assignment (regulated_subject: $a, regulated_doc: $d);').resolve()):
            return False
        tx.query(
            f'match $d isa company_document, has document_key "{doc_key}";'
            f' $a isa cii_object, has ent-key "{sub_ent}";'
            f' insert $r isa document_assignment (regulated_subject: $a, regulated_doc: $d);'
        ).resolve()
        return True

    # ------------------------------------------------------------------ загрузка
    def load(self, data: Dict[str, Any]) -> Dict[str, int]:
        from typedb.driver import Credentials, DriverOptions, DriverTlsConfig, TransactionType, TypeDB

        result = {"objects": 0, "software": 0, "requirements": 0,
                  "protects": 0, "documents": 0, "skipped": 0}
        c = self.cfg
        drv = TypeDB.driver(c["addr"], Credentials(c["user"], c["pass"]),
                            DriverOptions(DriverTlsConfig.disabled()))
        try:
            steps: List[Tuple[str, Any]] = []
            for item in data.get("objects") or []:
                steps.append(("objects", lambda tx, i=item: self.upsert_object(tx, i)))
            for item in data.get("software") or []:
                steps.append(("software", lambda tx, i=item: self.upsert_software(tx, i)))
            for item in data.get("requirements") or []:
                steps.append(("requirements", lambda tx, i=item: self.upsert_requirement(tx, i)))
            for item in data.get("protects") or []:
                steps.append(("protects", lambda tx, i=item: self.link_protection(
                    tx, str(i["system"]), str(i["software"]))))
            for item in data.get("documents") or []:
                steps.append(("documents", lambda tx, i=item: self.link_document(
                    tx, str(i["document_key"]), str(i["subject"]))))

            # Каждый шаг — своя транзакция: одна ошибка не откатывает остальное
            # (в версиях документов именно пачечный коммит это и сломал).
            for name, fn in steps:
                with drv.transaction(c["db"], TransactionType.WRITE) as tx:
                    try:
                        created = fn(tx)
                        tx.commit()
                        if created:
                            result[name] += 1
                        else:
                            result["skipped"] += 1
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("реестр: шаг %s не выполнен: %s",
                                       name, str(exc).splitlines()[0][:200])
                        result["skipped"] += 1
        finally:
            drv.close()
        return result


def load_file(path: str) -> Dict[str, int]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return CompanyRegistry().load(data)
