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
    "regulator": ("name",),                     # или short_name
    "regulatory_document": ("title",),          # или number
    "structural_element": ("text_content",),    # или index
    "security_measure": ("code",),              # или description
    "cii_object": ("category",),                # или system_type
    "threat_source": ("offender_class",),
}

# роль → допустимые типы участника (по схеме БД и промпту)
RELATION_ROLES: Dict[str, Dict[str, tuple]] = {
    "document_issuance": {"issuer": ("regulator",), "regulated_item": ("regulatory_document",)},
    "document_hierarchy": {"structural_part": ("structural_element",),
                           "sub_part": ("structural_element",)},
    "measure_definition": {"source_requirement": ("structural_element",),
                           "core_measure": ("security_measure",)},
    "applicability_criteria": {"applied_requirement": ("security_measure", "structural_element"),
                               "target_system": ("cii_object",)},
    "compensatory_relation": {"core_measure": ("security_measure",),
                              "alternative_measure": ("security_measure",)},
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
    """Ключ дедупликации по типу и опознавательному атрибуту (как в промпте).

    Регулятор/документ/мера дедуплицируются между документами; структурный
    элемент — внутри документа (номер пункта повторяется в разных ГОСТах).
    """
    if etype == "regulator":
        base = str(attrs.get("short_name") or attrs.get("name") or "")
    elif etype == "regulatory_document":
        base = f"{attrs.get('number', '')}|{attrs.get('type') or attrs.get('doc_type') or attrs.get('title', '')}"
    elif etype == "structural_element":
        base = f"{doc}|{attrs.get('index', '')}|{str(attrs.get('text_content') or '')[:200]}"
    elif etype == "security_measure":
        base = str(attrs.get("code") or str(attrs.get("description") or "")[:200])
    elif etype == "cii_object":
        base = f"{attrs.get('category', '')}|{attrs.get('system_type', '')}"
    else:  # threat_source
        base = f"{attrs.get('offender_class', '')}|{attrs.get('potential', '')}"
    digest = hashlib.sha1(base.lower().strip().encode("utf-8")).hexdigest()[:16]
    return f"{etype}:{digest}"


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
    elif etype == "regulatory_document":
        add("number", attrs.get("number"), 120)
        add("title", attrs.get("title"), 600)
        add("doc_type", attrs.get("type") or attrs.get("doc_type"), 80)
    elif etype == "structural_element":
        add("struct_index", attrs.get("index") or attrs.get("struct_index"), 120)
        add("text_content", attrs.get("text_content"), 3000)
    elif etype == "security_measure":
        add("code", attrs.get("code"), 80)
        add("description", attrs.get("description"), 2000)
    elif etype == "cii_object":
        add("category", attrs.get("category"), 120)
        add("system_type", attrs.get("system_type"), 120)
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
                clause: str = "") -> Dict[str, int]:
    """Записать собранные сущности/связи. Идемпотентно."""
    if not entities and not relations:
        return {"entities": 0, "relations": 0, "entities_existing": 0, "relations_existing": 0}

    # какие ключи уже есть в базе
    existing_keys: Set[str] = set()
    try:
        existing_keys = {_value(r.get("k")) for r in
                         _read('match $x has ent-key $k; fetch { "k": $k };')}
    except Exception as exc:  # noqa: BLE001
        logger.debug("typedb_writer: существующие ключи не прочитаны: %s", exc)

    inserted_e = inserted_r = 0

    # ── сущности: новые — put, существующим — только провенанс ──────────────
    for key, e in entities.items():
        has = _attr_line(key, e["attrs"], e["chunks"], clause)
        if key in existing_keys:
            # уже есть: добавляем недостающий провенанс, атрибуты не трогаем
            try:
                ch_lines = ", ".join(
                    f'source-chunk-ids "{_esc(ch, 200)}"' for ch in sorted(e["chunks"])[:40]
                )
                if ch_lines:
                    _write(f'match $x isa {e["type"]}, has ent-key "{key}"; insert $x has {ch_lines};')
            except Exception as exc:  # noqa: BLE001
                logger.debug("typedb_writer: провенанс не добавлен для %s: %s", key, exc)
            continue
        try:
            _write(f'put $x isa {e["type"]}{has};')
            inserted_e += 1
        except Exception as exc:  # noqa: BLE001 - один сбойный текст не должен рвать прогон
            logger.warning("typedb_writer: сущность не записана (%s): %s", key, str(exc)[:160])

    # ── связи: только те, у кого все участники уже в базе ───────────────────
    try:
        keys_now = {_value(r.get("k")) for r in
                    _read('match $x has ent-key $k; fetch { "k": $k };')}
    except Exception:  # noqa: BLE001
        keys_now = set()

    rels_to_write = {
        sig: rel for sig, rel in relations.items()
        if all(k in keys_now for k in rel["parts"].values())
    }
    skipped_rel = len(relations) - len(rels_to_write)

    if rels_to_write:
        # существующие сигнатуры — чтобы не плодить дубли связей
        existing_sigs: Set[str] = set()
        for rtype, roles in RELATION_ROLES.items():
            role_items = list(roles.keys())
            if len(role_items) < 2:
                continue
            r1, r2 = role_items[0], role_items[1]
            try:
                for row in _read(
                    f'match ({r1}: $a, {r2}: $b) isa {rtype}; '
                    f'$a has ent-key $ka; $b has ent-key $kb; '
                    f'fetch {{ "a": $ka, "b": $kb }};'
                ):
                    parts = {r1: _value(row.get("a")), r2: _value(row.get("b"))}
                    existing_sigs.add(f"{rtype}|" + "|".join(f"{r}={parts[r]}" for r in sorted(parts)))
            except Exception as exc:  # noqa: BLE001
                logger.debug("typedb_writer: существующие связи %s не прочитаны: %s", rtype, exc)

        for sig, rel in rels_to_write.items():
            if sig in existing_sigs:
                continue
            match_lines, roles_str = [], []
            for i, (role, key) in enumerate(rel["parts"].items()):
                etype = key.split(":", 1)[0]
                match_lines.append(f'$v{i} isa {etype}, has ent-key "{key}";')
                roles_str.append(f"{role}: $v{i}")
            q = (f"match\n  " + "\n  ".join(match_lines)
                 + f"\ninsert\n  {rel['type']} ({', '.join(roles_str)});")
            try:
                _write(q)
                inserted_r += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("typedb_writer: связь не записана (%s): %s", sig[:80], str(exc)[:160])

    return {"entities": inserted_e, "relations": inserted_r,
            "entities_existing": len(entities) - inserted_e,
            "relations_existing": len(rels_to_write) - inserted_r + skipped_rel}


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
        "index": clause or str(proposal.get("requirement_id") or ""),
        "text_content": text,
    }
    e_key = ent_key("structural_element", e_attrs, doc)
    entities[e_key] = {"type": "structural_element", "attrs": e_attrs, "chunks": chunks}

    if code:
        m_attrs = {"code": code, "description": text}
        m_key = ent_key("security_measure", m_attrs)
        entities[m_key] = {"type": "security_measure", "attrs": m_attrs, "chunks": chunks}
        sig = f"measure_definition|core_measure={m_key}|source_requirement={e_key}"
        relations[sig] = {
            "type": "measure_definition",
            "parts": {"source_requirement": e_key, "core_measure": m_key},
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


def _render_prompt(chunk_text: str, clause: str = "", document: str = "") -> str:
    """Промпт из БД + метаданные фрагмента (пункт нужен для requirement-id)."""
    try:
        from src.api.services.ontology_prompts import prompt_store

        template = prompt_store.get()["content"]
    except Exception:  # noqa: BLE001 - БД недоступна: заводской текст
        from src.api.services.ontology_prompts import DEFAULT_ENTITY_PROMPT

        template = DEFAULT_ENTITY_PROMPT
    header = []
    if document:
        header.append(f"Документ: {document}")
    if clause:
        header.append(f"пункт: {clause}")
    text = (("[" + "; ".join(header) + "]\n") if header else "") + chunk_text
    return template.replace("{text_chunk}", text)


async def extract_and_store_typedb(
    chunk_text: str, chunk_id: str, document_id: str = "",
    clause: str = "", filename: str = "", auto_schema: bool = True,
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

    prompt = _render_prompt(chunk_text, clause=clause, document=filename or document_id)
    payload = await _call_llm(prompt, chunk_id)
    if not payload:
        return {"status": "empty", "reason": "модель не вернула фактов"}

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
