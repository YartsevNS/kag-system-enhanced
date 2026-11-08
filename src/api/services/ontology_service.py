"""Сервис онтологии TypeDB: чтение схемы, валидация, правка, шаблон (ADR-004).

Синтаксис TypeQL 3.x проверен живьём на TypeDB 3.14.1 (БД cybersecurity_ontology), не по догадке:

  чтение      match $t sub $r;  match $t owns $a;  match $t plays $r;  match $r relates $o;
              (одним проходом; тип значения атрибута — из $a.valueType, kind из $t.kind)
  создание    ОДНО определение = ОДИН факт, несколько фактов — в одном define:
                define
                  attribute name, value string;
                  entity person owns name;
                  entity person owns birth-date;     ← owns по одному, списком через запятую НЕЛЬЗЯ
                  relation employment relates employee;   ← relates тоже по одному
                  entity person plays employment:employee;
              «define A; define B;» двумя инструкциями в одном тексте — НЕЛЬЗЯ ([TSV11])
  удаление    undefine <label>;   ← только целиком, БЕЗ категории
              `undefine entity <label>;` и `undefine <label> owns <attr>;` дают [TSV11]:
              в TypeDB 3.14 частично снять owns/plays/relates нельзя, только удалить тип.
              Отсюда ограничение отката: лишние owns/plays он снять не может и говорит об этом.

Почему перечисление одним проходом: на схеме 67 типов поход за owns/plays каждого типа
отдельной транзакцией — это 130+ round-trip к TypeDB на каждую загрузку страницы.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from src.indexing import typedb_facts

logger = logging.getLogger(__name__)

VALUE_TYPES = (
    "string", "integer", "double", "boolean", "date", "datetime", "datetime_tz",
    "decimal", "duration",
)


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
        """Полная структура онтологии: типы, owns, plays, роли, типы значений."""
        from typedb.driver import TransactionType

        kinds: Dict[str, str] = {}
        values: Dict[str, str] = {}
        owns_map: Dict[str, List[str]] = {}
        plays_map: Dict[str, List[str]] = {}
        roles_map: Dict[str, List[str]] = {}

        with self._driver() as d:
            with d.transaction(self._db(), TransactionType.READ) as tx:
                for row in self._fetch(tx, 'match $t sub $r; fetch { "t": $t };'):
                    t = row.get("t") or {}
                    label = t.get("label")
                    if not label:
                        continue
                    kinds[label] = t.get("kind", "?")
                    if t.get("kind") == "attribute":
                        values[label] = t.get("valueType") or "string"

                for row in self._fetch(tx, 'match $t owns $a; fetch { "t": $t, "a": $a };'):
                    owner, attr = row.get("t") or {}, row.get("a") or {}
                    if owner.get("label") and attr.get("label"):
                        owns_map.setdefault(owner["label"], []).append(attr["label"])

                for row in self._fetch(tx, 'match $t plays $r; fetch { "t": $t, "r": $r };'):
                    owner, role = row.get("t") or {}, row.get("r") or {}
                    if owner.get("label") and role.get("label"):
                        plays_map.setdefault(owner["label"], []).append(role["label"])

                for row in self._fetch(tx, 'match $r relates $o; fetch { "r": $r, "o": $o };'):
                    rel, role = row.get("r") or {}, row.get("o") or {}
                    role_label = role.get("label") or ""
                    if rel.get("label") and role_label:
                        # роль приходит как "relation:role" — для схемы нужен собственно role
                        roles_map.setdefault(rel["label"], []).append(role_label.split(":")[-1])

        entities: List[Dict[str, Any]] = []
        relations: List[Dict[str, Any]] = []
        attributes: List[Dict[str, Any]] = []
        roles: List[Dict[str, Any]] = []

        for label in sorted(kinds):
            kind = kinds[label]
            if kind == "entity":
                entities.append({
                    "label": label, "kind": kind,
                    "owns": sorted(set(owns_map.get(label, []))),
                    "plays": sorted(set(plays_map.get(label, []))),
                })
            elif kind == "relation":
                relations.append({
                    "label": label, "kind": kind,
                    "owns": sorted(set(owns_map.get(label, []))),
                    "roles": sorted(set(roles_map.get(label, []))),
                })
            elif kind == "attribute":
                attributes.append({"label": label, "kind": kind, "value": values.get(label, "string")})
            elif kind == "relation:role":
                rel, _, role = label.partition(":")
                roles.append({"label": label, "kind": kind, "relation": rel, "role": role})

        return {
            "entities": entities, "relations": relations,
            "attributes": attributes, "roles": roles,
            "ntypes": len(kinds), "source": self._db(),
        }

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

    # ---- создание/удаление типа ---------------------------------------
    def create_type(
        self,
        kind: str,
        label: str,
        owns: Optional[List[str]] = None,
        roles: Optional[List[str]] = None,
        value: str = "string",
    ) -> Dict[str, Any]:
        """Создать тип. Каждый факт — отдельным определением (списки TypeDB не принимает)."""
        label = (label or "").strip()
        if not label or " " in label:
            return {"ok": False, "error": "пустое имя типа или пробел в имени"}
        if kind == "attribute" and value not in VALUE_TYPES:
            return {"ok": False, "error": f"неизвестный тип значения: {value}"}

        lines: List[str] = []
        if kind == "attribute":
            lines.append(f"attribute {label}, value {value};")
        elif kind == "entity":
            lines.append(f"entity {label};")
            for a in owns or []:
                a = (a or "").strip()
                if a:
                    lines.append(f"entity {label} owns {a};")
        elif kind == "relation":
            lines.append(f"relation {label};")
            for r in roles or []:
                r = (r or "").strip()
                if r:
                    lines.append(f"relation {label} relates {r};")
            for a in owns or []:
                a = (a or "").strip()
                if a:
                    lines.append(f"relation {label} owns {a};")
        else:
            return {"ok": False, "error": f"неизвестная категория: {kind}"}

        text = "define\n  " + "\n  ".join(lines)
        logger.info("онтология: создание типа %s. Текст: %s", label, text)
        return self.apply(text)

    def delete_type(self, kind: str, label: str) -> Dict[str, Any]:
        """Удалить тип. Тип с экземплярами TypeDB не даст удалить — вернёт ошибку.

        Синтаксис: `undefine <label>;` — БЕЗ ключевого слова категории (см. шапку модуля).
        """
        label = (label or "").strip()
        if not label or " " in label:
            return {"ok": False, "error": "пустое имя типа"}
        if kind not in ("entity", "relation", "attribute", "relation:role"):
            return {"ok": False, "error": f"неизвестная категория: {kind}"}
        return self.apply(f"undefine {label};")

    # ---- шаблон: структура -> TypeQL -----------------------------------
    @staticmethod
    def fingerprint(schema: Dict[str, Any]) -> str:
        """Отпечаток структуры схемы (16 hex).

        Нужен, чтобы UI мог отличить «ничего не изменилось» от «база изменилась снаружи»
        без повторной отрисовки графа и без сравнения всего JSON по строкам.
        """
        import hashlib
        import json as _json

        payload = {
            "e": sorted((x["label"], tuple(sorted(x.get("owns") or [])),
                         tuple(sorted(x.get("plays") or [])))
                        for x in schema.get("entities", [])),
            "r": sorted((x["label"], tuple(sorted(x.get("owns") or [])),
                         tuple(sorted(x.get("roles") or [])))
                        for x in schema.get("relations", [])),
            "a": sorted((x["label"], (x.get("value") or "string"))
                        for x in schema.get("attributes", [])),
        }
        blob = _json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def to_tql(snapshot: Dict[str, Any], comment: str = "") -> str:
        """Сгенерировать define-текст из структуры схемы (скачивание шаблона, экспорт).

        Формат проверен валидатором TypeDB 3.14.1: несколько определений в одном define,
        каждый owns/relates/plays — отдельным определением.
        """
        lines: List[str] = []
        if comment:
            lines.extend(f"# {ln}" for ln in comment.splitlines())

        for a in snapshot.get("attributes", []):
            lines.append(f"  attribute {a['label']}, value {a.get('value') or 'string'};")
        for e in snapshot.get("entities", []):
            lines.append(f"  entity {e['label']};")
            for a in e.get("owns", []):
                lines.append(f"  entity {e['label']} owns {a};")
        for r in snapshot.get("relations", []):
            lines.append(f"  relation {r['label']};")
            for role in r.get("roles", []):
                lines.append(f"  relation {r['label']} relates {role};")
            for a in r.get("owns", []):
                lines.append(f"  relation {r['label']} owns {a};")
        for e in snapshot.get("entities", []):
            for p in e.get("plays", []):
                lines.append(f"  entity {e['label']} plays {p};")
        return ("define\n" + "\n".join(lines) + "\n") if lines else ""


ontology_service = OntologyService()
