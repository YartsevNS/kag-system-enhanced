"""Журнал происхождения загруженных файлов: хеш, размер, время, источник — с хеш-цепочкой.

Зачем. Нужна доказательная цепочка: по любому документу сказать, каким он был в момент загрузки
и не подменили ли его потом. Обычная таблица этого не даёт — строку можно переписать, и никто не
заметит. Поэтому журнал устроен так:

* запись ТОЛЬКО добавляется в конец (append-only JSONL);
* каждая запись хранит `prev_hash` (хеш предыдущей записи) и собственный `hash`, посчитанный по
  содержимому записи ВМЕСТЕ с `prev_hash`;
* проверка проходит по цепочке: подмена содержимого, удаление строки или вставка чужой записи
  ломают цепочку и обнаруживаются на конкретном номере.

Это тот же приём, что у «неизменяемого журнала правок» в промышленных онтологиях; в закрытом
контуре (без внешнего сервиса доверия) он — самый дешёвый способ сделать подмену заметной.

Файл журнала: <DATA_DIR>/provenance/journal.jsonl (bind-каталог ./data, живёт вне образа).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
GENESIS = "0" * 64


def _journal_path() -> Path:
    """Путь журнала. DATA_DIR из настроек (в контейнере — /app/data)."""
    try:
        from src.config import get_settings

        base = Path(getattr(get_settings(), "DATA_DIR", "/app/data"))
    except Exception:
        base = Path(os.environ.get("DATA_DIR", "/app/data"))
    return base / "provenance" / "journal.jsonl"


def _canonical(record: Dict[str, Any]) -> str:
    """Каноническая строка для хеша: без поля hash, ключи отсортированы, без пробелов."""
    body = {k: v for k, v in record.items() if k != "hash"}
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_record(record: Dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(record).encode("utf-8")).hexdigest()


def append(document_id: str, sha256: str, size: int, filename: str,
           source: Optional[Dict[str, Any]] = None,
           timestamp: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Добавить запись в журнал. Возвращает запись (или None, если записать не удалось).

    Ошибка записи НЕ должна ломать загрузку документа: журнал — вспомогательный слой,
    поэтому исключения гасятся и пишутся в лог (иначе недоступный диск остановит приём файлов).
    """
    try:
        path = _journal_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            last_hash, seq = GENESIS, 0
            if path.exists() and path.stat().st_size:
                with path.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            prev = json.loads(line)
                        except Exception:
                            continue
                        last_hash, seq = prev.get("hash", last_hash), int(prev.get("seq", seq)) + 1
            record: Dict[str, Any] = {
                "seq": seq,
                "ts": timestamp or datetime.now(timezone.utc).isoformat(),
                "document_id": document_id,
                "filename": filename,
                "size": int(size or 0),
                "sha256": sha256,
                "source": source or None,
                "prev_hash": last_hash,
            }
            record["hash"] = _hash_record(record)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            return record
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[provenance] запись в журнал не удалась для {document_id[:12]}: {e}")
        return None


def _actions_path() -> Path:
    return _journal_path().parent / "actions.jsonl"


def append_action(actor: str, action: str, target: str, details: Optional[Dict[str, Any]] = None,
                  timestamp: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Записать ПРАВКУ, сделанную человеком (аналог «журнала действий» в промышленных онтологиях).

    Хранится отдельно от журнала загрузок и по той же схеме с хеш-цепочкой: видно, КТО, КОГДА,
    ЧТО и С КАКИМ обоснованием изменил. Без этого правка словаря или типа документа неотличима
    от машинной разметки, и через месяц никто не скажет, откуда взялось значение.
    """
    record: Dict[str, Any] = {
        "ts": timestamp or datetime.now(timezone.utc).isoformat(),
        "actor": actor or "unknown",
        "action": action,
        "target": target,
        "details": details or {},
    }
    try:
        path = _actions_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            last_hash, seq = GENESIS, 0
            if path.exists() and path.stat().st_size:
                with path.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            prev = json.loads(line)
                        except Exception:
                            continue
                        last_hash, seq = prev.get("hash", last_hash), int(prev.get("seq", seq)) + 1
            record["seq"] = seq
            record["prev_hash"] = last_hash
            record["hash"] = _hash_record(record)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            return record
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[provenance] журнал действий недоступен: {e}")
        return None


def actions(limit: int = 50) -> List[Dict[str, Any]]:
    """Последние правки человека (свежие — первыми)."""
    path = _actions_path()
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    return list(reversed(out))[: max(1, limit)]


def verify() -> Tuple[bool, int, Optional[int]]:
    """Проверить цепочку. Возвращает (цепочка_цела, число_записей, номер_первой_битой)."""
    path = _journal_path()
    if not path.exists():
        return True, 0, None
    prev_hash, count = GENESIS, 0
    with path.open("r", encoding="utf-8") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except Exception:
                return False, count, i
            if record.get("prev_hash") != prev_hash:
                return False, count, i
            if _hash_record(record) != record.get("hash"):
                return False, count, i
            prev_hash = record["hash"]
            count += 1
    return True, count, None


def records(limit: int = 50) -> List[Dict[str, Any]]:
    """Последние записи журнала (для просмотра), свежие — первыми."""
    path = _journal_path()
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return list(reversed(out))[: max(1, limit)]


def find(document_id: str) -> List[Dict[str, Any]]:
    """Все записи журнала по документу — «откуда он и каким был при загрузке»."""
    return [r for r in records(limit=100000) if r.get("document_id") == document_id]
