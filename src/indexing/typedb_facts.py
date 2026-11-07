"""TypeDB-контур фактов для chat-пайплайна KAG (ADR-005, переход на TypeDB).

Модуль вызывает дедуктивные функции онтологии TypeDB (violated_documents и др.)
и возвращает ВЕРИФИЦИРОВАННЫЕ факты о нарушениях. Интеграция в chat_service.py
(шаг 2b, рядом с Neo4j-графом): секция «ФАКТЫ (TypeDB)» попадает в контекст ответа.

Безопасность интеграции:
- guarded import: если пакет typedb-driver не установлен или TypeDB недоступен,
  контур МОЛЧА выключен (is_available()=False) — прод KAG не падает.
- ACL (ADR-002): факты фильтруются по видимым юзеру chunk_id ВСЕГДА;
  пустой set = юзер не видит ничего = 0 фактов (guard `if visible and ...` ЗАПРЕЩЁН).
- Драйвер синхронный — вызывать через asyncio.to_thread (как Neo4j kg_service).

Конфиг из env (НЕ хардкод):
  TYPEDB_ADDR (default localhost:1729), TYPEDB_DB (default cybersecurity_ontology),
  TYPEDB_USER (default admin), TYPEDB_PASS (default ""). Отсутствие TYPEDB_PASS = контур выключен.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Set

logger = logging.getLogger(__name__)

# guarded import: без пакета контур выключен, но KAG работает
try:
    from typedb.driver import (  # noqa: F401
        TypeDB, Credentials, DriverOptions, DriverTlsConfig, TransactionType,
    )
    _TYPEDB_IMPORTED = True
except Exception:  # pragma: no cover - срабатывает только без пакета
    _TYPEDB_IMPORTED = False


def _cfg() -> Dict[str, str]:
    """Подключение берётся из конфигурации приложения (src/config.py или .env),
    а не хардкодится: смена базы — это настройка, а не правка кода."""
    try:
        from src.config import get_settings

        s = get_settings()
        return {
            "addr": s.TYPEDB_ADDR,
            "db": s.TYPEDB_DB,
            "user": s.TYPEDB_USER,
            "pass": s.TYPEDB_PASS,
        }
    except Exception:  # noqa: BLE001
        return {
            "addr": os.getenv("TYPEDB_ADDR", "localhost:1729"),
            "db": os.getenv("TYPEDB_DB", "cybersecurity_ontology"),
            "user": os.getenv("TYPEDB_USER", "admin"),
            "pass": os.getenv("TYPEDB_PASS", ""),
        }


def is_available() -> bool:
    """Контур доступен, только если пакет установлен И задан пароль (env)."""
    if not _TYPEDB_IMPORTED:
        return False
    cfg = _cfg()
    return bool(cfg["pass"])


def _driver():
    from typedb.driver import TypeDB, Credentials, DriverOptions, DriverTlsConfig
    cfg = _cfg()
    return TypeDB.driver(
        cfg["addr"],
        Credentials(cfg["user"], cfg["pass"]),
        DriverOptions(DriverTlsConfig.disabled()),
    )


def _value(x: Any) -> Any:
    """fetch-результат TypeDB 3.x: значение может быть dict с @value."""
    return x.get("@value") if isinstance(x, dict) else x


def query_facts(visible_chunk_ids: Set[str], limit: int = 8) -> List[Dict[str, Any]]:
    """Факты нарушений из TypeDB, ОТФИЛЬТРОВАННЫЕ по видимым chunk_id (ADR-002).

    Вызывает дедуктивную функцию онтологии violated_documents() и собирает
    факты «документ → нарушенное требование» с провенансом source-chunk-id.
    """
    if not is_available():
        return []
    from typedb.driver import TransactionType
    cfg = _cfg()
    facts: List[Dict[str, Any]] = []
    try:
        with _driver() as driver:
            with driver.transaction(cfg["db"], TransactionType.READ) as tx:
                q = (
                    'match let $doc, $law in violated_documents(); '
                    '$doc has title $dt, has source-chunk-id $ch, has source_origin $so; '
                    'fetch {"документ": $dt, "чанк": $ch, "источник": $so, '
                    '"требование": $law.requirement-id, "треб_desc": $law.description};'
                )
                for a in tx.query(q).resolve():
                    chunk_id = _value(a.get("чанк"))
                    # ── ADR-002: фильтр по видимости, ВСЕГДА ─────────────
                    if chunk_id not in visible_chunk_ids:
                        continue
                    facts.append({
                        "document": _value(a.get("документ")),
                        "chunk_id": chunk_id,
                        "source_origin": _value(a.get("источник")),
                        "requirement": _value(a.get("требование")),
                        "requirement_desc": _value(a.get("треб_desc")),
                    })
                    if len(facts) >= limit:
                        break
    except Exception as e:
        logger.debug(f"TypeDB факты пропущены: {e}")
        return []
    return facts


def format_facts_block(facts: List[Dict[str, Any]]) -> str:
    """Секция контекста для промпта (формат ADR-003)."""
    if not facts:
        return ""
    lines = []
    for f in facts:
        lines.append(
            f"- {f.get('document', '?')!r}: нарушено требование "
            f"{f.get('requirement', '?')} ({f.get('requirement_desc', '')}) "
            f"[чанк {f.get('chunk_id', '?')}, {f.get('source_origin', '?')}]"
        )
    return "\n".join(lines)

def outdated_versions() -> List[Dict[str, str]]:
    """Редакции со статусом superseded: документ, версия, дата окончания действия.

    Версии живут в той же базе, что и факты (ADR-008). Возвращает пустой список,
    если контур выключен или версий нет — вызывающий код не должен падать.
    """
    if not is_available():
        return []
    c = _cfg()
    out: List[Dict[str, str]] = []
    try:
        drv = TypeDB.driver(c["addr"], Credentials(c["user"], c["pass"]),
                            DriverOptions(DriverTlsConfig.disabled()))
        try:
            with drv.transaction(c["db"], TransactionType.READ) as tx:
                rows = list(tx.query(
                    'match $v isa document_version, has kag_document_id $d, '
                    'has version_status "superseded"; fetch {"d": $d};'
                ).resolve())
                for r in rows:
                    v = r.get("d")
                    out.append({"document_id": v.get("@value") if isinstance(v, dict) else v})
        finally:
            drv.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("typedb_versions: список устаревших редакций не получен: %s",
                       str(exc).splitlines()[0][:160])
    return out


def outdated_references(visible_chunk_ids: Set[str]) -> List[Dict[str, str]]:
    """Требования, чей фрагмент принадлежит утратившей силу редакции.

    Провенанс требования — source-chunk-id вида "<document_id>_chunk_NNNNN", поэтому
    редакция определяется по префиксу. ACL соблюдается: чанк не в visible — не отдаём.
    """
    versions = outdated_versions()
    if not versions:
        return []
    prefixes = [v["document_id"] for v in versions if v.get("document_id")]
    if not prefixes or not visible_chunk_ids:
        return []
    c = _cfg()
    out: List[Dict[str, str]] = []
    try:
        drv = TypeDB.driver(c["addr"], Credentials(c["user"], c["pass"]),
                            DriverOptions(DriverTlsConfig.disabled()))
        try:
            with drv.transaction(c["db"], TransactionType.READ) as tx:
                rows = list(tx.query(
                    'match $x isa security_requirement, has source-chunk-id $c; '
                    'fetch {"c": $c};'
                ).resolve())
        finally:
            drv.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("outdated_references: запрос не выполнен: %s",
                       str(exc).splitlines()[0][:160])
        return []
    for r in rows:
        v = r.get("c")
        chunk = v.get("@value") if isinstance(v, dict) else v
        if not chunk or chunk not in visible_chunk_ids:
            continue
        for p in prefixes:
            if chunk.startswith(p):
                out.append({"chunk_id": chunk, "document_id": p})
                break
    return out
