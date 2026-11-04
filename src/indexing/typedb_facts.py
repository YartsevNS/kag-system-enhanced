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