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


_SCHEMA_HEADER_LINES = ['# Схема онтологии KAG (TypeDB), выгружена из живой базы.', '#', '# Правила заполнения составных ключей (без них insert упадёт по @key):', '#   requirement_key = document_key + "|" + requirement-id', '#                     (requirement-id уникален только внутри документа)', '#   measure_key     = document_key + "|" + code', '#   document_key    = "издатель|тип|номер"', '#', '# Допустимые значения (функции вывода ищут точные строки):', '#   version_status   : current | superseded | repealed | draft', '#   requirement_kind : certification | organizational | technical | process', '#   requirement_kind — КАТЕГОРИЯ требования, а не степень обязательности;', '#   степень обязательности хранится в obligation-level.', '#', '# Источник правды по сертификации — объект certificate и связь certification;', '# атрибута is-certified в схеме нет намеренно.', '# Ограничения функций вывода (TypeDB не хранит комментарии, поэтому здесь):', '#   hierarchy_cycle / supersedes_cycle  ловят только 2-циклы; петли ищут', '#                                       отдельные hierarchy_self_loop и supersedes_self_loop,', '#                                       цикл из 3+ участников не находит НИ ОДНА из них', '#   invalid_date_range                  проверяет только версии с обеими датами', '#   missing_version_status              страховка от записи в обход схемы;', '#                                       при обычном insert не срабатывает', '#   current_status_without_link         аудит неполноты, а НЕ нарушение: связь', '#                                       current_version_of может появиться позже', '#   violated_certification / uncovered_documents', '#                                       не учитывают актуальность версии требования:', '#                                       требование из заменённой редакции считается действующим', '#', '# Необязательные атрибуты (могут отсутствовать в тексте, это норма):', '#   cert_authority  — орган сертификации;', '#   offender_class  — класс нарушителя у источника угроз.']


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
            # В отдельный журнал онтологии (/app/data/ontology/errors.jsonl, виден с хоста):
            # общий лог api для разбора схемы не годится — там всё подряд.
            from src.api.services.ontology_errors import log_error

            log_error(
                msg,
                error_type="validation_failed" if not commit else "insert_failed",
                source="typedb",
                details={"stage": "apply" if commit else "validate",
                         "define_len": len(text), "define": text[:600]},
            )
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

    def live_schema_text(self) -> str:
        """Текст схемы из TypeDB с шапкой правил, без которых она нечитаема.

        TypeDB не хранит комментарии define, поэтому формулы ключей и допустимые
        значения добавляются здесь: схема должна быть самодостаточной в файле,
        а не только в промпте.
        """
        text = self._driver().databases.get(self._db()).schema()
        return chr(10).join(_SCHEMA_HEADER_LINES) + chr(10) + chr(10) + text


    def tbox_summary(self, max_chars: int = 4000) -> str:
        """Компактный TBox для промпта извлечения, собранный из ЖИВОЙ схемы.

        Зачем из живой, а не из текста промпта: прозаический TBox в промпте уже
        один раз разошёлся со схемой (роль source_requirement, которой в v3 нет).
        Здесь типы, owns и роли берутся из схемы базы, поэтому дрейф невозможен.
        Звёздочкой помечены обязательные атрибуты (@key / @card(1..1)).
        """
        import re as _re

        driver = self._driver()
        try:
            text = driver.databases.get(self._db()).schema()
        finally:
            try:
                driver.close()
            except Exception:
                pass

        entities, relations = [], []
        cur = None
        for raw in text.splitlines():
            line = raw.strip().rstrip(",")
            if not line or line.startswith("#") or line.startswith("define"):
                continue
            m = _re.match(r"^entity\s+([\w-]+)\s*(.*)$", line)
            if m:
                cur = {"kind": "entity", "name": m.group(1), "owns": []}
                tail = m.group(2)
                if "@abstract" in tail:
                    cur["abstract"] = True
                entities.append(cur)
                continue
            m = _re.match(r"^relation\s+([\w-]+)\s*(.*)$", line)
            if m:
                cur = {"kind": "relation", "name": m.group(1), "relates": [], "owns": []}
                relations.append(cur)
                continue
            if cur is None:
                continue
            m = _re.match(r"^owns\s+([\w-]+)(.*)$", line)
            if m:
                attr, ann = m.group(1), m.group(2)
                req = "@key" in ann or "@card(1..1)" in ann
                cur["owns"].append(attr + ("*" if req else ""))
                continue
            m = _re.match(r"^relates\s+([\w-]+)(.*)$", line)
            if m and cur["kind"] == "relation":
                cur["relates"].append(m.group(1))
                continue
            if line.endswith(";"):
                cur = None

        out = []
        for e in entities:
            if e.get("abstract"):
                continue
            owns = ", ".join(e["owns"])
            out.append(f"entity {e['name']}: {owns}" if owns else f"entity {e['name']}")
        for r in relations:
            out.append(f"relation {r['name']}({', '.join(r['relates'])})")
        result = "\n".join(out)
        return result[:max_chars]

    # ---- шаблон: структура -> TypeQL -----------------------------------
    @staticmethod
    def fingerprint(schema: Dict[str, Any] | None = None, *, text: str | None = None) -> str:
        """Отпечаток схемы = SHA-256 её живого текста из TypeDB.

        Считается по тексту, а не по структуре из API: структура не содержит
        аннотаций (@key/@card/@abstract/@unique), поэтому изменение кардинальности
        или уникальности не меняло отпечаток — страница выглядела «не изменившейся»,
        хотя схема другая. Текст базы содержит всё.
        """
        import hashlib

        if text is None:
            try:
                text = ontology_service.live_schema_text()
            except Exception:  # noqa: BLE001 — схема недоступна: отпечаток пустой
                return ""
        norm = " ".join(line.strip() for line in (text or "").splitlines() if line.strip())
        return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]

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
