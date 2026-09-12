"""Порядок чанков для выдачи в интерфейс.

Проблема (2026-09-12): чанки показывались вперемешку. Причина — ключ сортировки
всегда оказывался нулём:
  - `chunk_seq`/`chunk_index` лежат в payload внутри `metadata`, а код читал их с
    верхнего уровня payload (там их нет → 0);
  - запасной разбор номера из `chunk_id` был рассчитан на старый формат
    (`chunk_00001`), а сейчас id выглядит как `<document_id>_chunk_00045` —
    `int()` падал, и функция возвращала `chunk_index` (тоже 0);
  - при одинаковых ключах сортировка ничего не меняет, и порядок оставался таким,
    каким его отдал Qdrant scroll (произвольный).

Этот модуль содержит единый разбор, чтобы порядок был одинаковым везде, где
чанки выдаются наружу (страница «Чанки», API документа, будущие выборки).
"""

from __future__ import annotations

import re
from typing import Any, Dict, Tuple

# Хвостовой номер чанка: <любой_префикс>_chunk_00045 либо просто chunk_00045
_CHUNK_TAIL_RE = re.compile(r"chunk_(\d+)$", re.IGNORECASE)
_TAIL_DIGITS_RE = re.compile(r"(\d+)$")


def chunk_seq_of(payload: Dict[str, Any]) -> int:
    """Номер чанка в документе (1..N). 0 — если определить не удалось."""
    if not isinstance(payload, dict):
        return 0

    # 1. Верхний уровень payload (актуально, если поле туда когда-нибудь поднимут)
    for key in ("chunk_seq", "chunk_index"):
        value = payload.get(key)
        if isinstance(value, int) and value > 0:
            return value

    # 2. metadata — там поля лежат сейчас
    meta = payload.get("metadata") or {}
    if isinstance(meta, dict):
        for key in ("chunk_seq", "chunk_index"):
            value = meta.get(key)
            if isinstance(value, int) and value > 0:
                return value
            if isinstance(value, str) and value.isdigit() and int(value) > 0:
                return int(value)

    # 3. Разбор из chunk_id: <document_id>_chunk_00045 или chunk_00045
    cid = str(payload.get("chunk_id") or "")
    m = _CHUNK_TAIL_RE.search(cid)
    if m:
        return int(m.group(1))
    m = _TAIL_DIGITS_RE.search(cid)
    if m:
        return int(m.group(1))
    return 0


def chunk_sort_key(payload: Dict[str, Any]) -> Tuple[str, int, str]:
    """Ключ сортировки: документ → номер чанка → id (стабильно при равных номерах)."""
    if not isinstance(payload, dict):
        return ("", 0, "")
    doc = str(payload.get("document_id") or payload.get("filename") or "")
    seq = chunk_seq_of(payload)
    # Номера нет вовсе — такие чанки ставим в конец, но порядок между ними
    # оставляем детерминированным (по chunk_id).
    return (doc, seq if seq > 0 else 10 ** 9, str(payload.get("chunk_id") or ""))
