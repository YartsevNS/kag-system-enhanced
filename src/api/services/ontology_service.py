"""Редактор онтологии TypeDB для админки (ADR-004) — сервис чтения/валидации/применения.

Перенос прототипа project_typedb/prototype/ontology_service.py в продукт. Отличия:
- настройки берутся из того же места, что и TypeDB-контур фактов (src.indexing.typedb_facts),
  чтобы адрес/база/логин не дублировались в двух местах;
- guarded import: без пакета typedb-driver модуль не валит api, а сообщает недоступность;
- ошибки TypeDB возвращаются как текст без трейсбека (в UI показывается одна строка).

Синтаксис перечисления (проверено на TypeDB 3.14.1, БД cybersecurity_ontology):
  все типы -> match $t sub $r;                       (kind: entity/relation/attribute/relation:role)
  owns     -> match $t sub X; $t owns $a; fetch {"a": $a};
  plays    -> match $t sub X; $t plays $r; fetch {"r": $r};
НЕ работают: sub entity / isa entity / sub thing / @entity / :entity (зарезервированные).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from src.indexing import typedb_facts

logger = logging.getLogger(__name__)


class OntologyUnavailable(RuntimeError):
    """TypeDB не настроен или недоступен — UI показывает это, а не пустую схему."""


class OntologyService:
    def _driver(self):
        if not typedb_facts.is_available():
            raise OntologyUnavailable(
                "TypeDB не настроен: не задан TYPEDB_PASS или не установлен typedb-driver"
            )
        return typedb_facts._driver()

    def _db(self) -> str:
        return typedb_facts._cfg()["db"]

    # ---- чтение схемы -------------------------------------------------
    def get_schema(self) -> Dict[str, Any]:
        """Полная структура онтологии для редактора."""
        from typedb.driver import TransactionType

        with self._driver() as d:
            with d.transaction(self._db(), TransactionType.READ) as tx:
                rows = self._fetch(tx, 'match $t sub $r; fetch { "t": $t, "r": $r };')
                kinds: Dict[str, str] = {}
                for row in rows:
                    t = row.get("t") or {}
                    if t.get("label"):
                        kinds[t["label"]] = t.get("kind", "?")

        entities: List[Dict[str, Any]] = []
        relations: List[Dict[str, Any]] = []
        attributes: List[Dict[str, Any]] = []
        roles: List[Dict[str, Any]] = []

        for label in sorted(kinds):
            kind = kinds[label]
            entry: Dict[str, Any] = {"label": label, "kind": kind}
            if kind == "entity":
                entry["owns"] = self._get_owns(label)
                entry["plays"] = self._get_plays(label)
                entities.append(entry)
            elif kind == "relation":
                entry["owns"] = self._get_owns(label)
                relations.append(entry)
            elif kind == "attribute":
                attributes.append(entry)
            elif kind == "relation:role":
                rel, _, role = label.partition(":")
                entry["relation"], entry["role"] = rel, role
                roles.append(entry)

        return {
            "entities": entities, "relations": relations,
            "attributes": attributes, "roles": roles,
            "ntypes": len(kinds), "source": self._db(),
        }

    def _get_owns(self, type_label: str) -> List[str]:
        from typedb.driver import TransactionType

        with self._driver() as d:
            with d.transaction(self._db(), TransactionType.READ) as tx:
                rows = self._fetch(tx, f'match $t sub {type_label}; $t owns $a; fetch {{"a": $a}};')
                names = [(r.get("a") or {}).get("label") for r in rows if r.get("a")]
                return sorted({n for n in names if n})

    def _get_plays(self, type_label: str) -> List[str]:
        from typedb.driver import TransactionType

        with self._driver() as d:
            with d.transaction(self._db(), TransactionType.READ) as tx:
                rows = self._fetch(tx, f'match $t sub {type_label}; $t plays $r; fetch {{"r": $r}};')
                names = [(r.get("r") or {}).get("label") for r in rows if r.get("r")]
                return sorted({n for n in names if n})

    def _fetch(self, tx, query: str) -> List[dict]:
        try:
            return list(tx.query(query).resolve())
        except Exception as exc:  # noqa: BLE001 — одна битая ветка не должна ронять всю схему
            logger.warning("онтология: запрос не выполнен (%s): %s", query[:60], str(exc).splitlines()[0][:120])
            return []

    # ---- валидация и применение ---------------------------------------
    def validate(self, define: str) -> Dict[str, Any]:
        """Проверить текст на SCHEMA-транзакции с откатом — изменений не остаётся."""
        return self._apply(define, commit=False)

    def apply(self, define: str) -> Dict[str, Any]:
        """Применить текст к схеме (commit)."""
        return self._apply(define, commit=True)

    def _apply(self, define: str, commit: bool) -> Dict[str, Any]:
        from typedb.driver import TransactionType

        text = (define or "").strip()
        if not text:
            return {"ok": False, "error": "пустой текст define", "commit": commit}
        try:
            with self._driver() as d:
                with d.transaction(self._db(), TransactionType.SCHEMA) as tx:
                    tx.query(text).resolve()
                    if commit:
                        tx.commit()
            logger.info("онтология: %s define (%d символов)", "применён" if commit else "проверен", len(text))
            return {"ok": True, "commit": commit, "define_len": len(text)}
        except OntologyUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 — текст ошибки уходит в UI
            msg = str(exc).splitlines()[-1] if str(exc) else "ошибка синтаксиса"
            return {"ok": False, "error": msg[:400], "commit": commit}

    # ---- типы ----------------------------------------------------------
    def create_type(self, kind: str, label: str, owns: List[str] | None = None) -> Dict[str, Any]:
        label = (label or "").strip()
        if not label:
            return {"ok": False, "error": "пустое имя типа"}
        if kind == "attribute":
            define = f"define attribute {label}, value string;"
        elif kind == "entity":
            body = f"entity {label}"
            if owns:
                body += " owns " + ", ".join(o.strip() for o in owns if o.strip())
            define = f"define {body};"
        elif kind == "relation":
            body = f"relation {label}"
            if owns:
                body += " owns " + ", ".join(o.strip() for o in owns if o.strip())
            define = f"define {body};"
        else:
            return {"ok": False, "error": f"неизвестная категория: {kind}"}
        logger.info("онтология: создание типа %s. Текст: %s", label, define)
        return self.apply(define)

    def delete_type(self, kind: str, label: str) -> Dict[str, Any]:
        """Удалить тип через undefine. Тип с экземплярами TypeDB не даст удалить — вернёт ошибку."""
        label = (label or "").strip()
        if not label or " " in label:
            return {"ok": False, "error": "пустое имя типа"}
        kinds = {"entity": "entity", "relation": "relation", "attribute": "attribute"}
        if kind not in kinds:
            return {"ok": False, "error": f"неизвестная категория: {kind}"}
        if kind == "attribute":
            define = f"undefine attribute {label};"
        else:
            define = f"undefine {kinds[kind]} {label};"
        return self.apply(define)


ontology_service = OntologyService()
