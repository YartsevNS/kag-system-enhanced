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


# ── схема: что вообще разрешено писать (ADR-001, продуктовый слой) ──────────
ENTITY_TYPES: Dict[str, tuple] = {
    # тип: (обязательные атрибуты для ключа, допустимые атрибуты)
    "security-requirement": ("requirement-id",),
    "regulation-source": ("name",),
    "compliance-document": ("title",),
    "company-document": ("title",),
    "evidence": ("name",),
    "application-domain": ("name",),
    "cii-object": ("name",),
    "security-software": ("name",),
}

# роль → допустимые типы участника
RELATION_ROLES: Dict[str, Dict[str, tuple]] = {
    "requirement-generation": {"source": ("regulation-source",), "requirement": ("security-requirement",)},
    "requirement-regulation": {"requirement": ("security-requirement",),
                               "document": ("compliance-document", "company-document")},
    "requirement-scope": {"requirement": ("security-requirement",), "domain": ("application-domain",)},
    "requirement-evidence": {"requirement": ("security-requirement",), "evidence": ("evidence",)},
    "requirement-relation": {
        "stricter": ("security-requirement",), "weaker": ("security-requirement",),
        "supplement": ("security-requirement",), "original": ("security-requirement",),
        "conflicting": ("security-requirement",), "conflicted": ("security-requirement",),
    },
    "asset-protection": {"protected-system": ("cii-object",), "deployed-szi": ("security-software",)},
    "document-assignment": {"regulated-subject": ("cii-object",), "regulated-doc": ("company-document",)},
}

# Типы, которым нужен ключ дедупликации и провенанс (дополняется аддитивно)
_PROVENANCE_TYPES = tuple(ENTITY_TYPES.keys())


def _cfg() -> Dict[str, str]:
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
    """Отдельный выключатель записи (system/typedb.write_enabled), по умолчанию — вкл."""
    try:
        from src.api.services.config_store import config_store

        cfg = config_store.get("system", "typedb") or {}
        if "write_enabled" in cfg:
            return bool(cfg["write_enabled"])
    except Exception:  # noqa: BLE001
        pass
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


def ent_key(etype: str, attrs: Dict[str, Any]) -> str:
    """Ключ дедупликации по типу и опознавательному атрибуту (ADR-007)."""
    if etype == "security-requirement":
        base = str(attrs.get("requirement-id") or "")
    elif etype == "regulation-source":
        base = f"{attrs.get('name', '')}|{attrs.get('version', '')}"
    elif etype == "compliance-document":
        base = f"{attrs.get('title', '')}|{attrs.get('doc-kind', '')}"
    elif etype == "company-document":
        base = str(attrs.get("title") or "")
    elif etype == "evidence":
        base = f"{attrs.get('name', '')}|{attrs.get('evidence-kind', '')}"
    elif etype == "cii-object":
        base = f"{attrs.get('name', '')}|{attrs.get('cii-category', '')}"
    else:  # application-domain, security-software
        base = str(attrs.get("name") or "")
    digest = hashlib.sha1(base.lower().strip().encode("utf-8")).hexdigest()[:16]
    return f"{etype}:{digest}"


def _attr_line(key: str, attrs: Dict[str, Any], chunks: Iterable[str]) -> str:
    """has-выражения сущности. Порядок и объём ограничены, чтобы не раздувать запрос."""
    etype = key.split(":", 1)[0]
    has: List[str] = []

    def add(attr: str, value, limit: int = 3000, as_int: bool = False):
        if value is None or value == "":
            return
        if as_int:
            try:
                has.append(f"has {attr} {int(float(value))}")
            except (TypeError, ValueError):
                return
        else:
            has.append(f'has {attr} "{_esc(value, limit)}"')

    if etype == "security-requirement":
        add("requirement-id", attrs.get("requirement-id"), 200)
        add("description", attrs.get("description"), 3000)
        add("obligation-level", attrs.get("obligation-level"), 40)
        add("source-clause", attrs.get("source-clause"), 80)
        add("password-length", attrs.get("password-length"), as_int=True)
        add("change-period", attrs.get("change-period"), as_int=True)
        add("char-composition", attrs.get("char-composition"), 200)
    elif etype == "regulation-source":
        add("name", attrs.get("name"), 300)
        add("version", attrs.get("version"), 80)
    elif etype == "compliance-document":
        add("title", attrs.get("title"), 600)
        add("doc-kind", attrs.get("doc-kind"), 60)
        add("version", attrs.get("version"), 80)
    elif etype == "company-document":
        add("title", attrs.get("title"), 600)
        add("version", attrs.get("version"), 80)
    elif etype == "evidence":
        add("name", attrs.get("name"), 300)
        add("evidence-kind", attrs.get("evidence-kind"), 60)
    elif etype == "cii-object":
        add("name", attrs.get("name"), 300)
        add("cii-category", attrs.get("cii-category"), as_int=True)
    elif etype == "security-software":
        add("name", attrs.get("name"), 300)
        if attrs.get("is-certified") is not None:
            val = str(attrs.get("is-certified")).strip().lower()
            if val in ("true", "false"):
                has.append(f"has is-certified {val}")

    has.append(f'has ent-key "{key}"')
    chunk_list = sorted({c for c in chunks if c})
    if chunk_list:
        has.append(f'has source-chunk-id "{_esc(chunk_list[0], 200)}"')
        for ch in chunk_list[:40]:
            has.append(f'has source-chunk-ids "{_esc(ch, 200)}"')
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


def _collect(payload: Dict[str, Any], chunk_id: str) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, int]]:
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
        required = ENTITY_TYPES[etype]
        if any(not str(attrs.get(a) or "").strip() for a in required):
            stats["dropped_missing_attr"] += 1
            continue
        key = ent_key(etype, attrs)
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


# ── схема: аддитивное дополнение (ent-key, многозначный провенанс) ───────────
_PROVENANCE_SCHEMA = "\n".join(
    [f"entity {t} owns ent-key;" for t in _PROVENANCE_TYPES]
    + [f"entity {t} owns source-chunk-id;" for t in _PROVENANCE_TYPES]
    + [f"entity {t} owns source-chunk-ids @card(0..);" for t in _PROVENANCE_TYPES]
)

# Схема дополняется один раз на процесс: повторные define стоят времени, а
# результат от них не меняется. None = ещё не проверяли.
_PROVENANCE_OK: bool | None = None


def ensure_provenance_schema() -> Dict[str, Any]:
    """Добавить целевым типам ent-key и провенанс (однократно, аддитивно).

    Зачем: в продуктовом слое (ADR-001) эти атрибуты объявлены не у всех типов
    (у compliance-document/evidence/application-domain их нет вообще), а без них
    запись провенанса падает на ошибке владения. Определения аддитивны —
    существующие данные и владения не ломаются.
    """
    global _PROVENANCE_OK
    if _PROVENANCE_OK is True:
        return {"ok": True, "cached": True}
    if not is_available():
        return {"ok": False, "reason": "typedb недоступен"}
    try:
        from src.api.services.ontology_service import ontology_service

        ok, errors = 0, []
        for line in _PROVENANCE_SCHEMA.splitlines():
            line = line.strip()
            if not line:
                continue
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


def write_facts(chunk_id: str, entities: Dict[str, Any], relations: Dict[str, Any]) -> Dict[str, int]:
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
        has = _attr_line(key, e["attrs"], e["chunks"])
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


# ── LLM ───────────────────────────────────────────────────────────────────────
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

    entities, relations, stats = _collect(payload, chunk_id)
    if not entities:
        return {"status": "empty", "stats": stats}

    try:
        written = await asyncio.to_thread(write_facts, chunk_id, entities, relations)
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
