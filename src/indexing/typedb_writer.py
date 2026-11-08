"""Запись сущностей и связей из документа в TypeDB (ADR-007).

Зачем отдельный модуль: до него TypeDB в KAG только ЧИТАЛАСЬ (typedb_facts),
а наполнялась пилотными скриптами из project_typedb. Из-за этого онтология
была снимком, а не частью flow. Здесь — путь записи из обработки документа.

Что делает:
1. Берёт промпт из БД (ontology_prompts, страница /ontology/prompt) — правка
   в админке действует со следующего прогона без пересборки образа.
2. Вызывает модель, привязанную к функции graph (provider_service), с
   отключённым reasoning — как в entity_extractor (иначе бюджет уходит в thinking).
3. Валидирует ответ по схеме: тип сущности, тип связи, роль, тип участника.
   Всё, чего нет в схеме, отбрасывается со счётчиком — не пишется в граф.
4. Дедуплицирует по ent-key (тип + опознавательный атрибут) и по сигнатуре
   связи; провенанс копится в source-chunk-id / source-chunk-ids.
5. Пишет ТОЛЬКО в WRITE-транзакции. Существующим сущностям добавляет
   провенанс, но не затирает атрибуты, которые уже есть.

Границы: любая ошибка (LLM, парсинг, TypeDB) логируется и возвращается как
status=error — обработка документа при этом НЕ падает.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from typing import Any, Dict, Iterable, List, Set

logger = logging.getLogger(__name__)

# Замок на запись по документу: чанки одного документа обрабатываются параллельно
# (semaphore 3), и без сериализации два потока успевали вставить одну и ту же связь —
# отсюда дубли вида current_version_of 16 при 12 версиях. Один замок на document_id
# делает запись последовательной, не замедляя извлечение LLM (оно идёт параллельно).
_WRITE_LOCKS: Dict[str, "threading.Lock"] = {}
_WRITE_LOCKS_GUARD = None


def _doc_lock(doc_id: str):
    """Вернуть (создать) замок записи для документа."""
    global _WRITE_LOCKS_GUARD
    import threading

    if _WRITE_LOCKS_GUARD is None:
        _WRITE_LOCKS_GUARD = threading.Lock()
    with _WRITE_LOCKS_GUARD:
        lock = _WRITE_LOCKS.get(doc_id)
        if lock is None:
            lock = threading.Lock()
            _WRITE_LOCKS[doc_id] = lock
        return lock

# ── зависимость TypeDB ────────────────────────────────────────────────────────
try:
    from typedb.driver import (  # noqa: F401
        TypeDB, Credentials, DriverOptions, DriverTlsConfig, TransactionType,
    )
    _TYPEDB_IMPORTED = True
except Exception:  # pragma: no cover - срабатывает только без пакета
    _TYPEDB_IMPORTED = False


# ── схема: что вообще разрешено писать (по промпту из БД) ───────────────────
# КОНТУР КОРПУСА — типы, которые извлекаются из нормативных документов
# (регулятор, документ, пункт, мера, объект КИИ, источник угрозы). Именно они
# объявлены в БД с ent-key и провенансом, и именно по ним идёт наполнение.
# Второй контур — ДОКАЗАТЕЛЬНЫЙ (требования, документы компании, СЗИ, доказательства) —
# наполняется внутренними документами заказчика, см. write_proposal().
ENTITY_TYPES: Dict[str, tuple] = {
    # тип: (атрибуты-кандидаты для опознавания, в порядке приоритета)
    "regulator": ("name", "short_name"),
    "regulatory_document": ("document_key", "title"),
    "company_document": ("document_key", "title"),
    "document_version": ("kag_document_id", "version_string"),
    "structural_element": ("struct_index", "text_content"),
    "security_requirement": ("requirement_key", "requirement-id"),
    "security_measure": ("measure_key", "code"),
    "evidence": ("name",),
    "cii_object": ("name",),
    "security_software": ("name",),
    "certificate": ("cert_number",),
    "threat_source": ("offender_class",),
}

# Обязательные атрибуты схемы v3 (@key / @card(1..1)). Без них TypeDB отвергнет запись,
# поэтому writer их дозаполняет, а если не может — сущность пропускается с логом.
REQUIRED_ATTRS: Dict[str, tuple] = {
    "regulator": ("name",),
    "regulatory_document": ("title", "document_key"),
    "company_document": ("title", "document_key"),
    "document_version": ("version_string",),
    "structural_element": ("struct_index", "text_content"),
    "security_requirement": ("requirement_key", "requirement-id", "requirement_kind"),
    "security_measure": ("measure_key",),
    "evidence": ("name",),
    "cii_object": ("name",),
    "security_software": ("name",),
    "certificate": ("cert_number",),
    "threat_source": ("offender_class",),
}

# роль → допустимые типы участника (схема v3)
RELATION_ROLES: Dict[str, Dict[str, tuple]] = {
    # «издан регулятором» — только нормативный акт: политику компании регулятор не издаёт
    "document_issuance": {"issuer": ("regulator",), "regulated_item": ("regulatory_document",)},
    "document_has_version": {"document": ("regulatory_document", "company_document"),
                             "version": ("document_version",)},
    "current_version_of": {"document": ("regulatory_document", "company_document"),
                           "version": ("document_version",)},
    "supersedes": {"newer": ("document_version",), "older": ("document_version",)},
    "document_structure": {"version": ("document_version",), "element": ("structural_element",)},
    "document_hierarchy": {"parent": ("structural_element",), "child": ("structural_element",)},
    "requirement_derived_from": {"requirement": ("security_requirement",),
                                 "clause": ("structural_element",)},
    "requirement_definition": {"requirement": ("security_requirement",),
                               "measure": ("security_measure",)},
    "measure_source": {"measure": ("security_measure",), "clause": ("structural_element",)},
    "compensatory_relation": {"core_measure": ("security_measure",),
                              "alternative_measure": ("security_measure",)},
    "requirement_applicability": {"requirement": ("security_requirement",),
                                  "target_system": ("cii_object",)},
    "measure_applicability": {"measure": ("security_measure",), "target_system": ("cii_object",)},
    "requirement_evidence": {"requirement": ("security_requirement",), "evidence": ("evidence",)},
    # требование регулятора опирается на нормативный акт; внутренние основания
    # требований компании — отдельная задача, здесь их не смешиваем
    # документ компании покрывает требование (для uncovered_documents)
    "requirement_coverage": {"covering_document": ("company_document",),
                             "covered_requirement": ("security_requirement",)},
    "requirement_basis": {"requirement": ("security_requirement",),
                          "source_document": ("regulatory_document",)},
    "document_compliance": {"company_doc": ("company_document",),
                            "regulated_doc": ("regulatory_document",)},
    "document_assignment": {"regulated_doc": ("regulatory_document", "company_document"),
                            "regulated_subject": ("cii_object",)},
    "asset_protection": {"protected_system": ("cii_object",), "deployed_szi": ("security_software",)},
    "certification": {"certified_software": ("security_software",), "certificate": ("certificate",),
                      "basis_requirement": ("security_requirement",)},
    "threat_applicability": {"threat": ("threat_source",), "target_system": ("cii_object",)},
    "threat_measure": {"threat": ("threat_source",), "measure": ("security_measure",)},
}

# Типы, которым нужен ключ дедупликации и провенанс (дополняется аддитивно)
_PROVENANCE_TYPES = tuple(ENTITY_TYPES.keys())


def _cfg() -> Dict[str, str]:
    """Параметры подключения: сначала файл конфигурации/окружение приложения,
    затем — переменные процесса (совместимость со старыми вызовами)."""
    try:
        from src.config import get_settings

        s = get_settings()
        return {
            "addr": s.TYPEDB_ADDR,
            "db": s.TYPEDB_DB,
            "user": s.TYPEDB_USER,
            "pass": s.TYPEDB_PASS,
        }
    except Exception:  # noqa: BLE001 — конфиг недоступен (пилотный скрипт, тест)
        return {
            "addr": os.getenv("TYPEDB_ADDR", "localhost:1729"),
            "db": os.getenv("TYPEDB_DB", "cybersecurity_ontology"),
            "user": os.getenv("TYPEDB_USER", "admin"),
            "pass": os.getenv("TYPEDB_PASS", ""),
        }


def is_available() -> bool:
    """Писать можно, только если пакет установлен И задан пароль."""
    return _TYPEDB_IMPORTED and bool(_cfg()["pass"])


def write_enabled() -> bool:
    """Выключатель записи: настройка админки system/typedb.write_enabled главнее,
    затем — TYPEDB_WRITE_ENABLED из конфигурации приложения."""
    try:
        from src.api.services.config_store import config_store

        cfg = config_store.get("system", "typedb") or {}
        if "write_enabled" in cfg:
            return bool(cfg["write_enabled"])
    except Exception:  # noqa: BLE001
        pass
    try:
        from src.config import get_settings

        return bool(get_settings().TYPEDB_WRITE_ENABLED)
    except Exception:  # noqa: BLE001
        return True


def _driver():
    from typedb.driver import TypeDB, Credentials, DriverOptions, DriverTlsConfig

    c = _cfg()
    return TypeDB.driver(
        c["addr"], Credentials(c["user"], c["pass"]), DriverOptions(DriverTlsConfig.disabled())
    )


# ── helpers ───────────────────────────────────────────────────────────────────
def _esc(text: Any, limit: int = 3000) -> str:
    """Строковый литерал TypeQL: управляющие символы, кавычки, обратные слеши."""
    s = str(text or "").strip()[:limit]
    s = "".join(ch if (ch >= " " and ch != "\x7f") else " " for ch in s)
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    return re.sub(r"\s+", " ", s)


def _value(x: Any) -> Any:
    return x.get("@value") if isinstance(x, dict) else x


def ent_key(etype: str, attrs: Dict[str, Any], doc: str = "") -> str:
    """Ключ дедупликации сущности (ent-key) по типу и опознавательному атрибуту.

    В v3 у большинства типов есть естественный ключ (@key) — берём его первым,
    чтобы один и тот же документ не создавался заново из каждого чанка.
    """
    if etype == "regulator":
        base = str(attrs.get("short_name") or attrs.get("name") or "")
    elif etype in ("regulatory_document", "company_document"):
        base = str(attrs.get("document_key") or attrs.get("title") or "")
    elif etype == "document_version":
        # ключ не должен зависеть от того, подставлен ли kag_document_id: иначе
        # повторная обработка того же файла создала бы вторую редакцию
        base = f"{attrs.get('kag_document_id') or doc}|{attrs.get('version_string') or ''}"
    elif etype == "structural_element":
        # Ключ по номеру пункта, а НЕ по тексту: модель переформулирует текст между
        # прогонами, и элемент с тем же пунктом получал новый ключ — при переиндексации
        # граф рос (1292 вместо 1284). Текст участвует только если пункта нет.
        idx = str(attrs.get("struct_index") or attrs.get("index") or "").strip()
        if idx:
            base = f"{doc}|{idx}"
        else:
            base = f"{doc}|text:{str(attrs.get('text_content') or '')[:200]}"
    elif etype == "security_requirement":
        base = str(attrs.get("requirement_key") or attrs.get("requirement-id") or "")
    elif etype == "security_measure":
        base = str(attrs.get("measure_key") or attrs.get("code")
                   or str(attrs.get("description") or "")[:200])
    elif etype == "certificate":
        base = str(attrs.get("cert_number") or "")
    elif etype in ("cii_object", "security_software", "evidence"):
        base = str(attrs.get("name") or "")
    else:  # threat_source
        base = f"{attrs.get('offender_class', '')}|{attrs.get('potential', '')}"
    digest = hashlib.sha1(base.lower().strip().encode("utf-8")).hexdigest()[:16]
    return f"{etype}:{digest}"


def _norm_key(value: Any) -> str:
    """Нормализация естественного ключа: нижний регистр, один разделитель."""
    s = str(value or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s[:200]


def ensure_required(entities: Dict[str, Dict[str, Any]], doc_key: str = "",
                    doc_id: str = "") -> tuple:
    """Дозаполнить обязательные атрибуты v3 и отбросить то, что дозаполнить нельзя.

    Зачем в writer, а не в промпте: модель не видит граф и не знает document_key
    соседнего фрагмента. Ключи, которые выводятся из данных, надёжнее построить здесь:
    requirement_key = document_key|пункт, measure_key = document_key|код.
    Возвращает (готовые сущности, число отброшенных, причины).
    """
    ready: Dict[str, Dict[str, Any]] = {}
    dropped: List[str] = []

    # Сначала находим ключ документа в этой пачке — он нужен производным ключам.
    for e in entities.values():
        if e["type"] in ("regulatory_document", "company_document"):
            dk = e["attrs"].get("document_key")
            if not dk:
                dk = f"{_norm_key(e['attrs'].get('number')) or _norm_key(e['attrs'].get('title'))}"
                e["attrs"]["document_key"] = dk
            if not doc_key:
                doc_key = dk

    for key, e in entities.items():
        etype, attrs = e["type"], e["attrs"]
        if etype == "document_version" and doc_id and not attrs.get("kag_document_id"):
            # ссылка на файл-источник: связывает редакцию с документом в Postgres/Qdrant
            attrs["kag_document_id"] = doc_id
        if etype == "security_requirement" and not attrs.get("requirement_key"):
            rid = attrs.get("requirement-id") or attrs.get("requirement_id")
            if rid:
                attrs["requirement_key"] = f"{doc_key}|{_norm_key(rid)}" if doc_key else _norm_key(rid)
        if etype == "security_measure" and not attrs.get("measure_key"):
            code = attrs.get("code")
            base = code or str(attrs.get("description") or "")[:60]
            if base:
                attrs["measure_key"] = f"{doc_key}|{_norm_key(base)}" if doc_key else _norm_key(base)

        missing = [a for a in REQUIRED_ATTRS.get(etype, ()) if not attrs.get(a)]
        if missing:
            dropped.append(f"{etype}: нет {', '.join(missing)}")
            continue
        ready[key] = e
    return ready, len(dropped), dropped


def _attr_line(key: str, attrs: Dict[str, Any], chunks: Iterable[str], clause: str = "") -> str:
    """has-выражения сущности по схеме промпта.
    
    source-chunk-id однозначный (@card 0..1): туда первый чанк, полный
    провенанс — в многозначный source-chunk-ids (иначе CNT5 валит вставку).
    """
    etype = key.split(":", 1)[0]
    has: List[str] = []

    def add(attr: str, value, limit: int = 3000):
        if value is None or value == "":
            return
        has.append(f'has {attr} "{_esc(value, limit)}"')

    if etype == "regulator":
        add("name", attrs.get("name"), 300)
        add("short_name", attrs.get("short_name"), 120)
    elif etype in ("regulatory_document", "company_document"):
        add("title", attrs.get("title"), 600)
        add("doc_type", attrs.get("type") or attrs.get("doc_type"), 80)
        add("document_key", attrs.get("document_key"), 300)
        if etype == "regulatory_document":
            add("number", attrs.get("number"), 120)
    elif etype == "document_version":
        add("version_string", attrs.get("version_string"), 80)
        add("version_status", attrs.get("version_status") or "current", 40)
        add("version_hash", attrs.get("version_hash"), 80)
        add("kag_document_id", attrs.get("kag_document_id"), 120)
        add("effective_from", attrs.get("effective_from"), 40)
        add("effective_to", attrs.get("effective_to"), 40)
    elif etype == "structural_element":
        add("struct_index", attrs.get("index") or attrs.get("struct_index"), 120)
        add("text_content", attrs.get("text_content"), 3000)
    elif etype == "security_requirement":
        add("requirement_key", attrs.get("requirement_key"), 300)
        add("requirement-id", attrs.get("requirement-id") or attrs.get("requirement_id"), 120)
        add("requirement_kind", attrs.get("requirement_kind"), 80)
        add("description", attrs.get("description"), 2000)
        add("obligation-level", attrs.get("obligation-level") or attrs.get("obligation_level"), 60)
    elif etype == "security_measure":
        add("measure_key", attrs.get("measure_key"), 300)
        add("code", attrs.get("code"), 80)
        add("description", attrs.get("description"), 2000)
    elif etype == "evidence":
        add("name", attrs.get("name"), 300)
        add("evidence_kind", attrs.get("evidence_kind"), 80)
        add("evidence_date", attrs.get("evidence_date"), 40)
    elif etype == "cii_object":
        add("name", attrs.get("name"), 300)
        add("system_type", attrs.get("system_type"), 120)
        cat = attrs.get("cii-category") or attrs.get("cii_category")
        try:
            cat_int = int(cat) if cat not in (None, "") else None
        except (TypeError, ValueError):
            cat_int = None
        if cat_int is not None:
            has.append(f"has cii-category {cat_int}")   # integer, без кавычек
    elif etype == "security_software":
        add("name", attrs.get("name"), 300)
        add("vendor", attrs.get("vendor"), 120)
        add("software_version", attrs.get("software_version"), 80)
        # is-certified удалён из схемы: источник правды — объект certificate
        # и связь certification. Boolean расходился с реальными сертификатами.
    elif etype == "certificate":
        add("cert_number", attrs.get("cert_number"), 120)
        add("cert_authority", attrs.get("cert_authority"), 200)
        add("cert_until", attrs.get("cert_until"), 40)
    elif etype == "threat_source":
        add("offender_class", attrs.get("offender_class"), 200)
        add("potential", attrs.get("potential"), 80)

    has.append(f'has ent-key "{key}"')
    chunk_list = sorted({c for c in chunks if c})
    if chunk_list:
        has.append(f'has source-chunk-id "{_esc(chunk_list[0], 200)}"')
        for ch in chunk_list[:40]:
            has.append(f'has source-chunk-ids "{_esc(ch, 200)}"')
    if clause:
        add("source-clause", clause, 80)
    return "".join(", " + h for h in has)


def _parse_json(text: str) -> Dict[str, Any]:
    """Устойчивый разбор ответа модели (markdown, обрезки, лишний текст)."""
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"```\s*$", "", s).strip()
    try:
        return json.loads(s)
    except Exception:
        m = re.search(r"\{.*\}", s, re.S)
        if not m:
            return {}
        try:
            return json.loads(m.group())
        except Exception:
            return {}


def _inject_doc_context(payload: Dict[str, Any], doc_ctx: Dict[str, str] | None) -> None:
    """Заменить у документов и версий ключевые поля значениями из метаданных загрузки.

    Модель не видит граф и не может знать, что документ уже создан другим чанком.
    Поэтому document_key/version_string/kag_document_id берутся из метаданных, а не из ответа
    модели: тогда все чанки одного файла сходятся в один акт и одну редакцию.
    """
    if not doc_ctx:
        return
    for ent in payload.get("entities") or []:
        if not isinstance(ent, dict):
            continue
        etype = str(ent.get("type") or "")
        attrs = ent.setdefault("attributes", {})
        if etype in ("regulatory_document", "company_document"):
            if doc_ctx.get("document_key"):
                attrs["document_key"] = doc_ctx["document_key"]
            if doc_ctx.get("title") and not attrs.get("title"):
                attrs["title"] = doc_ctx["title"]
            if doc_ctx.get("doc_type") and not attrs.get("doc_type") and not attrs.get("type"):
                attrs["doc_type"] = doc_ctx["doc_type"]
            if doc_ctx.get("number") and not attrs.get("number"):
                attrs["number"] = doc_ctx["number"]
        elif etype == "document_version":
            if doc_ctx.get("version_string"):
                attrs["version_string"] = doc_ctx["version_string"]
            if doc_ctx.get("kag_document_id"):
                attrs["kag_document_id"] = doc_ctx["kag_document_id"]


def _collect(payload: Dict[str, Any], chunk_id: str, document_id: str = "") -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, int]]:
    """Валидация и дедупликация одного ответа модели.

    Возвращает (entities, relations, stats). Всё, что вне схемы, отбрасывается.
    """
    stats = {"entities_raw": 0, "entities_kept": 0, "relations_raw": 0,
             "relations_kept": 0, "dropped_type": 0, "dropped_role": 0,
             "dropped_missing_attr": 0}
    entities: Dict[str, Dict[str, Any]] = {}
    local: Dict[str, List[tuple]] = {}

    for ent in payload.get("entities") or []:
        if not isinstance(ent, dict):
            continue
        stats["entities_raw"] += 1
        etype = str(ent.get("type") or "").strip()
        if etype not in ENTITY_TYPES:
            stats["dropped_type"] += 1
            continue
        attrs = {k: v for k, v in (ent.get("attributes") or {}).items() if v not in (None, "")}
        # обязательные атрибуты: хотя бы один из пары (для регулятора/документа/меры)
        if not attrs:
            stats["dropped_missing_attr"] += 1
            continue
        key = ent_key(etype, attrs, document_id)
        local.setdefault(str(ent.get("id") or ""), []).append((etype, key))
        slot = entities.setdefault(key, {"type": etype, "attrs": {}, "chunks": set()})
        for k, v in attrs.items():
            if v not in (None, "") and slot["attrs"].get(k) in (None, ""):
                slot["attrs"][k] = v
        if chunk_id:
            slot["chunks"].add(chunk_id)

    relations: Dict[str, Dict[str, Any]] = {}
    for rel in payload.get("relations") or []:
        if not isinstance(rel, dict):
            continue
        stats["relations_raw"] += 1
        rtype = str(rel.get("type") or "").strip()
        roles = RELATION_ROLES.get(rtype)
        if not roles:
            stats["dropped_type"] += 1
            continue
        parts: Dict[str, str] = {}
        for p in rel.get("participants") or []:
            if not isinstance(p, dict):
                continue
            role = str(p.get("role") or "").strip()
            if role not in roles:
                stats["dropped_role"] += 1
                continue
            candidates = local.get(str(p.get("target_id") or "")) or []
            wanted = roles[role]
            key = next((k for t, k in candidates if t in wanted), None)
            if not key:
                stats["dropped_role"] += 1
                continue
            parts[role] = key
        # связь без всех участников не имеет смысла
        if len(parts) < len(roles):
            stats["dropped_role"] += 1
            continue
        sig = f"{rtype}|" + "|".join(f"{r}={parts[r]}" for r in sorted(parts))
        relations.setdefault(sig, {"type": rtype, "parts": parts})

    stats["entities_kept"] = len(entities)
    stats["relations_kept"] = len(relations)
    return entities, relations, stats


# ── схема: провенанс (типы контура корпуса уже объявлены в БД) ──────────────
# В базе у всех шести типов уже есть ent-key, source-chunk-id и
# source-chunk-ids @card(0..) — дополнительных определений не нужно. Функция
# оставлена как быстрая проверка: если атрибута нет, добавляет только его.
_PROVENANCE_OK: bool | None = None


def ensure_provenance_schema() -> Dict[str, Any]:
    """Убедиться, что у целевых типов есть ключ и провенанс (идемпотентно)."""
    global _PROVENANCE_OK
    if _PROVENANCE_OK is True:
        return {"ok": True, "cached": True}
    if not is_available():
        return {"ok": False, "reason": "typedb недоступен"}
    try:
        from src.api.services.ontology_service import ontology_service

        schema = ontology_service.get_schema()
        missing: List[str] = []
        for e in schema.get("entities", []):
            label = e.get("label")
            if label not in ENTITY_TYPES:
                continue
            owns = set(e.get("owns") or [])
            for attr in ("ent-key", "source-chunk-id"):
                if attr not in owns:
                    missing.append(f"entity {label} owns {attr};")
            if "source-chunk-ids" not in owns:
                missing.append(f"entity {label} owns source-chunk-ids @card(0..);")

        if not missing:
            _PROVENANCE_OK = True
            return {"ok": True, "applied": 0}

        ok, errors = 0, []
        for line in missing:
            try:
                res = ontology_service.apply("define\n  " + line)
                if res.get("ok"):
                    ok += 1
                else:
                    errors.append((line[:70], str(res.get("error"))[:100]))
            except Exception as exc:  # noqa: BLE001
                errors.append((line[:70], str(exc)[:100]))
        _PROVENANCE_OK = not errors
        return {"ok": _PROVENANCE_OK, "applied": ok, "errors": errors[:5]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


# ── запись ────────────────────────────────────────────────────────────────────
def _write(query: str) -> None:
    from typedb.driver import TransactionType

    c = _cfg()
    with _driver() as dr:
        with dr.transaction(c["db"], TransactionType.WRITE) as tx:
            list(tx.query(query).resolve())
            tx.commit()


def _read(query: str) -> list:
    from typedb.driver import TransactionType

    c = _cfg()
    with _driver() as dr:
        with dr.transaction(c["db"], TransactionType.READ) as tx:
            return list(tx.query(query).resolve())


def write_facts(chunk_id: str, entities: Dict[str, Any], relations: Dict[str, Any],
                clause: str = "") -> Dict[str, Any]:
    """Записать сущности и связи документа ОДНОЙ транзакцией и проверить целостность.

    Почему одной транзакцией: раньше на каждую сущность и связь открывалась своя
    транзакция. Это медленно и, главное, не позволяло откатить документ, если правила
    целостности нарушены: половина уже записана. Теперь при нарушении — rollback.

    Ролевые ограничения (одна текущая редакция, один родитель, пункт в одной редакции)
    проверяются функциями схемы v3 ДО commit: в TypeDB 3.14 @key/@unique на ролях
    не поддерживаются, поэтому иначе это не проверить.
    """
    if not entities and not relations:
        return {"entities": 0, "relations": 0, "entities_existing": 0, "relations_existing": 0}

    doc_lock_id = str(chunk_id).split("_chunk_")[0] if chunk_id else ""
    _lock = _doc_lock(doc_lock_id)
    with _lock:
        return _write_facts_locked(chunk_id, entities, relations, clause)


def _write_facts_locked(chunk_id: str, entities: Dict[str, Any], relations: Dict[str, Any],
                        clause: str = "") -> Dict[str, Any]:
    """Тело write_facts под замком документа (см. _doc_lock)."""

    doc_id = str(chunk_id).split("_chunk_")[0] if chunk_id else ""
    entities, dropped, reasons = ensure_required(entities, doc_id=doc_id)
    if dropped:
        logger.warning("typedb_writer: пропущено %d сущностей без обязательных полей: %s",
                       dropped, "; ".join(reasons[:5]))

    existing_keys: Set[str] = set()
    try:
        existing_keys = {_value(r.get("k")) for r in
                         _read('match $x has ent-key $k; fetch { "k": $k };')}
    except Exception as exc:  # noqa: BLE001
        logger.debug("typedb_writer: существующие ключи не прочитаны: %s", exc)

    entity_queries: List[str] = []
    inserted_e = 0
    entity_keys_now = set(existing_keys)

    for key, e in entities.items():
        if key in existing_keys:
            # существующей сущности добавляем только провенанс
            ch_lines = ", ".join(
                f'source-chunk-ids "{_esc(ch, 200)}"' for ch in sorted(e["chunks"])[:40])
            if ch_lines:
                entity_queries.append(
                    f'match $x isa {e["type"]}, has ent-key "{key}"; insert $x has {ch_lines};')
            continue
        has = _attr_line(key, e["attrs"], e["chunks"], clause)
        entity_queries.append(f'put $x isa {e["type"]}{has};')
        inserted_e += 1
        entity_keys_now.add(key)

    # Связи: только те, у кого все участники есть в базе или вставляются этой же пачкой.
    # Существующие связи читаем заранее: без этого повторная обработка плодит дубли
    # (на ГОСТ 57580.2 вышло 48 document_has_version при одном акте и одной версии).
    existing_sigs: Set[str] = set()
    try:
        for rtype, roles in RELATION_ROLES.items():
            role_items = list(roles.keys())
            if len(role_items) < 2:
                continue
            r1, r2 = role_items[0], role_items[1]
            for row in _read(
                f'match ({r1}: $a, {r2}: $b) isa {rtype}; '
                f'$a has ent-key $ka; $b has ent-key $kb; '
                f'fetch {{ "a": $ka, "b": $kb }};'
            ):
                existing_sigs.add(
                    f"{rtype}|{r1}={_value(row.get('a'))}|{r2}={_value(row.get('b'))}")
    except Exception as exc:  # noqa: BLE001
        logger.debug("typedb_writer: существующие связи не прочитаны: %s", str(exc)[:120])

    def build_relations() -> tuple:
        """Собрать запросы связей с учётом уже существующих и уже собранных."""
        _existing: Set[str] = set()
        try:
            for rtype, roles in RELATION_ROLES.items():
                role_items = list(roles.keys())
                if len(role_items) < 2:
                    continue
                r1, r2 = role_items[0], role_items[1]
                for row in _read(
                    f'match ({r1}: $a, {r2}: $b) isa {rtype}; '
                    f'$a has ent-key $ka; $b has ent-key $kb; '
                    f'fetch {{ "a": $ka, "b": $kb }};'
                ):
                    _existing.add(
                        f"{rtype}|{r1}={_value(row.get('a'))}|{r2}={_value(row.get('b'))}")
        except Exception as exc:  # noqa: BLE001
            logger.debug("typedb_writer: существующие связи не прочитаны: %s", str(exc)[:120])

        _queries: List[str] = []
        _written: Set[str] = set()
        _skipped = 0
        for sig, rel in relations.items():
            parts = rel["parts"]
            if any(k not in entity_keys_now for k in parts.values()):
                _skipped += 1
                continue
            roles_sorted = "|".join(f"{k}={v}" for k, v in sorted(parts.items()))
            if sig in _written or f"{rel['type']}|{roles_sorted}" in _existing:
                _skipped += 1
                continue
            match_lines, roles_str = [], []
            for i, (role, key) in enumerate(parts.items()):
                etype = key.split(":", 1)[0]
                match_lines.append(f'$v{i} isa {etype}, has ent-key "{key}";')
                roles_str.append(f"{role}: $v{i}")
            _queries.append("match\n  " + "\n  ".join(match_lines)
                            + "\ninsert\n  " + rel["type"]
                            + " (" + ", ".join(roles_str) + ");")
            _written.add(sig)
        return _queries, _skipped

    rel_queries, skipped_rel = build_relations()

    if not entity_queries and not rel_queries:
        return {"entities": 0, "relations": 0,
                "entities_existing": len(entities), "relations_existing": skipped_rel,
                "dropped": dropped}

    from typedb.driver import TransactionType as _TT

    cfg = _cfg()
    problems: List[Dict[str, Any]] = []
    last_error = ""
    # Чанки документа обрабатываются параллельно (semaphore 3) и могут одновременно
    # вставить один и тот же акт: второй получает конфликт @unique по document_key.
    # Это не ошибка данных, а гонка — поэтому повторяем с перечитыванием существующих ключей.
    for attempt in range(3):
        drv = _driver()
        try:
            with drv.transaction(cfg["db"], _TT.WRITE) as tx:
                for q in entity_queries + rel_queries:
                    tx.query(q).resolve()
                problems = _integrity_in_tx(tx, set(entities.keys()))
                if problems:
                    logger.warning("typedb_writer: документ не записан, нарушений целостности %d: %s",
                                   len(problems), problems[:3])
                    return {"entities": 0, "relations": 0, "entities_existing": 0,
                            "relations_existing": 0, "dropped": dropped, "integrity": problems}
                tx.commit()
            last_error = ""
            break
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            low = last_error.lower()
            race = ("cnt9" in low or "@unique" in low or "@key" in low or "unique" in low)
            if race and attempt < 2:
                logger.info("typedb_writer: гонка по ключу, повтор %d/3", attempt + 1)
                # перечитать существующие ключи и пересобрать пачку «только новое»
                try:
                    existing_keys = {_value(r.get("k")) for r in
                                     _read('match $x has ent-key $k; fetch { "k": $k };')}
                except Exception:  # noqa: BLE001
                    existing_keys = set()
                entity_queries = []
                entity_keys_now = set(existing_keys)
                for key, e in entities.items():
                    if key in existing_keys:
                        continue
                    has = _attr_line(key, e["attrs"], e["chunks"], clause)
                    entity_queries.append(f'put $x isa {e["type"]}{has};')
                    entity_keys_now.add(key)
                rel_queries, skipped_rel = build_relations()
                continue
            logger.error("typedb_writer: транзакция документа не выполнена: %s", last_error[:300])
            return {"entities": 0, "relations": 0, "entities_existing": 0,
                    "relations_existing": 0, "dropped": dropped,
                    "error": last_error.splitlines()[0][:200]}
        finally:
            try:
                drv.close()
            except Exception:
                pass
    if last_error:
        return {"entities": 0, "relations": 0, "entities_existing": 0,
                "relations_existing": 0, "dropped": dropped,
                "error": last_error.splitlines()[0][:200]}

    return {"entities": inserted_e, "relations": len(rel_queries),
            "entities_existing": len(entities) - inserted_e,
            "relations_existing": skipped_rel, "dropped": dropped}


# Проверки читают ent-key участников: по ним нарушение привязывается к текущей пачке.
# Иначе одно старое нарушение в базе блокировало бы загрузку любого документа.
_INTEGRITY_QUERIES = (
    ('match let $d, $v in multiple_current_versions(); fetch {"d": $d.ent-key, "v": $v.ent-key};',
     "у документа больше одной действующей редакции"),
    ('match let $c, $p1, $p2 in multiple_parents(); fetch {"c": $c.ent-key, "p1": $p1.ent-key};',
     "у пункта несколько родителей"),
    ('match let $e, $v1, $v2 in element_in_multiple_structures(); fetch {"e": $e.ent-key, "v1": $v1.ent-key};',
     "пункт принадлежит нескольким редакциям"),
)


def _integrity_in_tx(tx, keys: Set[str] | None = None) -> List[Dict[str, Any]]:
    """Прогнать проверки целостности внутри текущей транзакции записи.

    Функции читаются в той же транзакции, поэтому видят ещё не закоммиченные данные —
    именно это позволяет откатить документ до commit. Если функции в схеме нет
    (база v2), проверка пропускается, а не роняет запись.
    """
    out: List[Dict[str, Any]] = []
    for query, title in _INTEGRITY_QUERIES:
        try:
            rows = list(tx.query(query).resolve())
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).splitlines()[0][:120]
            if "QEX" in msg or "TSV" in msg:
                continue
            logger.debug("typedb_writer: проверка целостности недоступна: %s", msg)
            continue
        for row in rows:
            subject = {k: _value(v) for k, v in row.items()}
            if keys is not None:
                # нарушение относится к этому документу, только если его участник — из пачки
                if not any(str(v) in keys for v in subject.values()):
                    continue
            out.append({"problem": title, "subject": subject})
    return out


# ── запись ПРИНЯТОГО предложения (очередь онтологии → TypeDB) ──────────────
def write_proposal(proposal: Dict[str, Any]) -> Dict[str, Any]:
    """Записать принятое человеком предложение в онтологию (контур корпуса).

    Очередь предложений живёт в Postgres, онтология — в TypeDB, и пока между
    ними не было звена, «принято» означало только смену статуса: у всех
    accepted `typedb_fact_id` оставался пустым.

    Маппинг предложения на целевую схему:
      пункт документа → structural_element (struct_index = пункт, text_content = текст)
      мера            → security_measure (code, description) — если в предложении есть код
      связь           → measure_definition (source_requirement → core_measure)

    Возвращает {"fact_id": ent-key пункта, "written": {...}}.
    """
    doc = str(proposal.get("document_id") or "")
    chunk = str(proposal.get("chunk_id") or "")
    clause = str(proposal.get("clause") or "")
    text = str(proposal.get("text") or "").strip()
    code = str(proposal.get("measure_code") or "").strip()
    if not text:
        raise ValueError("пустой текст предложения")

    entities: Dict[str, Dict[str, Any]] = {}
    relations: Dict[str, Dict[str, Any]] = {}
    chunks = {chunk} if chunk else set()

    e_attrs: Dict[str, Any] = {
        "struct_index": clause or str(proposal.get("requirement_id") or ""),
        "text_content": text,
    }
    e_key = ent_key("structural_element", e_attrs, doc)
    entities[e_key] = {"type": "structural_element", "attrs": e_attrs, "chunks": chunks}

    if code:
        m_attrs = {"code": code, "description": text}
        m_key = ent_key("security_measure", m_attrs)
        entities[m_key] = {"type": "security_measure", "attrs": m_attrs, "chunks": chunks}
        # v3: «мера описана в пункте» — measure_source (раньше роль называлась
        # source_requirement и играл её пункт, что было семантической ошибкой).
        sig = f"measure_source|measure={m_key}|clause={e_key}"
        relations[sig] = {
            "type": "measure_source",
            "parts": {"measure": m_key, "clause": e_key},
        }

    written = write_facts(chunk, entities, relations, clause)
    return {"fact_id": e_key, "written": written}


def _llm_payload(messages: list, model: str, provider: str, api_key: str, temperature: float) -> dict:
    payload = {
        "model": model,
        "messages": messages,
        "temperature": min(max(temperature, 0.0), 0.5),
        "max_tokens": 2000,
        "response_format": {"type": "json_object"},
    }
    try:
        from src.llm.nothink import nothink_payload

        payload.update(nothink_payload(provider, model) or {})
    except Exception:  # noqa: BLE001
        pass
    return payload


async def _call_llm(prompt: str, chunk_id: str) -> Dict[str, Any]:
    """Один вызов модели по промпту из БД. Возвращает распарсенный JSON."""
    try:
        from src.api.services.provider_service import provider_service

        cfg = provider_service.get_function_llm_config("graph")
    except Exception as exc:  # noqa: BLE001
        logger.warning("typedb_writer: конфиг модели не получен: %s", exc)
        return {}

    if not cfg or not cfg.get("model"):
        logger.warning("typedb_writer: функция graph не настроена — извлечение пропущено")
        return {}

    provider = cfg.get("provider", "custom")
    url = (cfg.get("url") or "").rstrip("/")
    api_key = cfg.get("api_key", "")
    model = cfg["model"]
    temperature = float((cfg.get("parameters") or {}).get("temperature", 0.05) or 0.05)

    payload = _llm_payload(
        messages=[{"role": "user", "content": prompt}], model=model,
        provider=provider, api_key=api_key, temperature=temperature,
    )
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    import aiohttp

    # deepseek и совместимые отвечают на /v1/chat/completions
    endpoint = f"{url}/v1/chat/completions" if provider != "ollama" else f"{url}/api/generate"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(endpoint, json=payload, headers=headers,
                              timeout=aiohttp.ClientTimeout(total=90)) as r:
                body = await r.text()
                if r.status != 200:
                    logger.warning("typedb_writer: LLM %s вернул %s: %s", provider, r.status, body[:200])
                    return {}
                data = json.loads(body)
    except Exception as exc:  # noqa: BLE001
        logger.warning("typedb_writer: вызов модели не удался для %s: %s", chunk_id, exc)
        return {}

    if provider == "ollama":
        content = data.get("response", "") or ""
    else:
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    if not content.strip():
        return {}
    return _parse_json(content)


def _render_prompt(chunk_text: str, clause: str = "", document: str = "",
                   doc_ctx: Dict[str, str] | None = None) -> str:
    """Промпт из БД + метаданные фрагмента и документа.

    doc_ctx (document_key, version_string, kag_document_id, title, doc_type) подставляется
    буквально: без него модель на каждом чанке придумывает новый document_key и создаёт
    ещё один «документ» — на ГОСТ 57580.2 так появилось 5 актов и 2 версии из одного файла.
    """
    try:
        from src.api.services.ontology_prompts import prompt_store

        template = prompt_store.get()["content"]
    except Exception:  # noqa: BLE001 - БД недоступна: заводской текст
        from src.api.services.ontology_prompts import DEFAULT_ENTITY_PROMPT

        template = DEFAULT_ENTITY_PROMPT
    header = []
    if doc_ctx:
        parts = [f"{k}={v}" for k, v in doc_ctx.items() if v]
        if parts:
            header.append("КОНТЕКСТ ДОКУМЕНТА (используй эти значения буквально, "
                          "не придумывай свои): " + "; ".join(parts))
    if document:
        header.append(f"Документ: {document}")
    if clause:
        header.append(f"пункт: {clause}")
    text = (("[" + "; ".join(header) + "]\n") if header else "") + chunk_text
    return template.replace("{text_chunk}", text)


async def extract_and_store_typedb(
    chunk_text: str, chunk_id: str, document_id: str = "",
    clause: str = "", filename: str = "", auto_schema: bool = True,
    source_kind: str = "", doc_ctx: Dict[str, str] | None = None,
) -> Dict[str, Any]:
    """Извлечь сущности/связи из чанка и записать в TypeDB (ADR-007).

    Один публичный вход write-пути. Никогда не бросает исключение наружу:
    обработка документа не должна падать из-за онтологии.
    """
    if not is_available() or not write_enabled():
        return {"status": "skipped", "reason": "typedb выключен"}
    if not chunk_text or len(chunk_text.strip()) < 40:
        return {"status": "skipped", "reason": "фрагмент слишком короткий"}

    if auto_schema:
        try:
            await asyncio.to_thread(ensure_provenance_schema)
        except Exception as exc:  # noqa: BLE001
            logger.debug("typedb_writer: схема не дополнена: %s", exc)

    prompt = _render_prompt(chunk_text, clause=clause,
                            document=filename or document_id, doc_ctx=doc_ctx)
    payload = await _call_llm(prompt, chunk_id)
    if not payload:
        return {"status": "empty", "reason": "модель не вернула фактов"}
    _inject_doc_context(payload, doc_ctx)

    # Документ организации: модель по форме текста всё равно называет его «нормативным
    # документом», поэтому тип переопределяем ДО валидации. Связь выдачи
    # (document_issuance: regulator → regulatory_document) для документа компании
    # не ставится — организацию никто не наделяет полномочиями регулятора.
    if (source_kind or "").strip().lower() == "company":
        for ent in payload.get("entities") or []:
            if isinstance(ent, dict) and ent.get("type") == "regulatory_document":
                ent["type"] = "company_document"
        if payload.get("relations"):
            payload["relations"] = [
                r for r in payload["relations"]
                if not (isinstance(r, dict) and r.get("type") == "document_issuance")
            ]

    entities, relations, stats = _collect(payload, chunk_id, document_id)
    if not entities and not relations:
        return {"status": "empty", "stats": stats}

    try:
        written = await asyncio.to_thread(write_facts, chunk_id, entities, relations, clause)
    except Exception as exc:  # noqa: BLE001
        logger.warning("typedb_writer: запись не удалась для %s: %s", chunk_id, exc)
        return {"status": "error", "error": str(exc)[:200], "stats": stats}

    logger.info(
        "typedb_writer: %s — сущностей %s (+%s уже было), связей %s (+%s уже было), "
        "отброшено: тип %s, роль %s, без ключа %s",
        chunk_id, written["entities"], written["entities_existing"],
        written["relations"], written["relations_existing"],
        stats["dropped_type"], stats["dropped_role"], stats["dropped_missing_attr"],
    )
    return {"status": "ok", "written": written, "stats": stats}
