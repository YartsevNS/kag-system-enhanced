"""Глоссарий из словаря в задание извлечения — цикл Learn.

Зачем: правка человека сейчас умирает там, где её сделали. Человек подтвердил
«АВЗ → Антивирусная защита» или отклонил «СССР → Верховного Совета РСФСР» —
и на следующем документе модель об этом не знает, извлечение повторяет ту же
ошибку, человек правит её второй раз. Здесь решения человека возвращаются
в задание: подтверждённые пары идут как канонические названия, отклонённые —
как явный запрет на слияние.

Это тот же принцип, что «Learn» в auto-ontology (сертифицированные правки
вкатываются обратно в онтологию), но применённый к нашему источнику смысла —
словарю терминов и тексту документов, а не к колонкам БД.

Стоимость: один SELECT с кешем на 10 минут, вставка в промпт ~1–2 КБ. Порог по
размеру обязателен: задание не должно расти до полного словаря (662 пары).
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List

logger = logging.getLogger(__name__)

MAX_APPROVED = 60      # сколько канонических соответствий показываем модели
MAX_REJECTED = 20      # сколько запретов на слияние
MAX_CHARS = 2200       # жёсткий потолок блока в задании

_cache: Dict[str, object] = {"at": 0.0, "block": ""}
_TTL = 600.0


def invalidate() -> None:
    """Сбросить кеш: после решения человека глоссарий обязан обновиться сразу."""
    _cache.update({"at": 0.0, "block": ""})


def _rows() -> tuple[List[tuple], List[tuple]]:
    from sqlalchemy import text

    from src.database.session import get_session_local

    db = get_session_local()()
    try:
        approved = db.execute(text(
            "SELECT alias, canonical_name FROM entity_aliases "
            "WHERE verdict = 'approved' AND coalesce(canonical_name, '') <> '' "
            "ORDER BY entity_type, alias LIMIT :lim"
        ), {"lim": MAX_APPROVED}).all()
        rejected = db.execute(text(
            "SELECT alias, canonical_name FROM entity_aliases "
            "WHERE verdict = 'rejected' AND coalesce(canonical_name, '') <> '' "
            "ORDER BY alias LIMIT :lim"
        ), {"lim": MAX_REJECTED}).all()
    finally:
        db.close()
    return approved, rejected


def build() -> str:
    """Текст глоссария для задания извлечения. Пустая строка — словаря нет."""
    now = time.time()
    if _cache["block"] and now - float(_cache["at"]) < _TTL:
        return str(_cache["block"])

    try:
        approved, rejected = _rows()
    except Exception as exc:  # noqa: BLE001 - без словаря извлечение работает как раньше
        logger.debug("глоссарий: словарь недоступен: %s", str(exc)[:160])
        return ""

    if not approved and not rejected:
        return ""

    lines = [
        "ГЛОССАРИЙ (решения человека по прошлым документам — соблюдай их буквально):",
        "канонические соответствия — используй ПРАВУЮ часть как название сущности:",
    ]
    used = 0
    for alias, canonical in approved:
        item = f"  {alias} → {canonical}"
        if used + len(item) > MAX_CHARS:
            break
        lines.append(item)
        used += len(item)
    if rejected:
        lines.append("НЕ сливать в одну сущность (человек проверил и отклонил):")
        for alias, canonical in rejected:
            item = f"  {alias} ≠ {canonical}"
            if used + len(item) > MAX_CHARS:
                break
            lines.append(item)
            used += len(item)

    block = "\n".join(lines)
    _cache.update({"at": now, "block": block})
    return block


def stats() -> Dict[str, int]:
    approved, rejected = ([], [])
    try:
        approved, rejected = _rows()
    except Exception:  # noqa: BLE001
        pass
    return {"approved": len(approved), "rejected": len(rejected),
            "chars": len(build())}
