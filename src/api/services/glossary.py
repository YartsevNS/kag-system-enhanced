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
    """Пары для глоссария в порядке полезности для извлечения.

    Порядок важен: в словаре 500+ подтверждённых пар, а в задание попадают
    первые 60. Если брать по алфавиту, блок заполняют английские сокращения
    из чужого словаря (3GPP, FMLN), и полезные доменные термины туда не влезают.
    Приоритет: термины предметной области → организации ИБ → остальное.
    """
    from sqlalchemy import text

    from src.database.session import get_session_local

    db = get_session_local()()
    try:
        approved = db.execute(text(
            "SELECT alias, canonical_name FROM entity_aliases "
            "WHERE verdict = 'approved' AND coalesce(canonical_name, '') <> '' "
            "ORDER BY CASE WHEN entity_type = 'term' THEN 0 "
            "              WHEN domain = 'infosec' THEN 1 "
            "              WHEN entity_type = 'organization' THEN 2 ELSE 3 END, "
            "         alias LIMIT :lim"
        ), {"lim": max(MAX_APPROVED * 4, 200)}).all()
        rejected = db.execute(text(
            "SELECT alias, canonical_name FROM entity_aliases "
            "WHERE verdict = 'rejected' AND coalesce(canonical_name, '') <> '' "
            "ORDER BY alias LIMIT :lim"
        ), {"lim": MAX_REJECTED}).all()
    finally:
        db.close()
    return approved, rejected


def _is_cyrillic(text: str) -> bool:
    return any("а" <= ch.lower() <= "я" or ch.lower() == "ё" for ch in (text or ""))


def _select(pairs: List[tuple]) -> List[tuple]:
    """Отобрать пары под бюджет задания.

    Доменные пары (кириллица хотя бы с одной стороны) идут целиком до 60,
    иностранные сокращения — не больше 12: они нужны редко и вытесняют
    русские термины из блока.
    """
    out: List[tuple] = []
    foreign = 0
    for alias, canonical in pairs:
        if not (_is_cyrillic(alias) or _is_cyrillic(canonical)):
            if foreign >= 12:
                continue
            foreign += 1
        out.append((alias, canonical))
        if len(out) >= MAX_APPROVED:
            break
    return out


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

    approved = _sanitize(_select(approved))

    lines = [
        "ГЛОССАРИЙ (решения человека по прошлым документам — соблюдай их буквально):",
        "канонические соответствия — используй ПРАВУЮ часть как название сущности:",
    ]
    used = sum(len(x) + 1 for x in lines)
    for alias, canonical in approved:
        item = f"  {alias} → {canonical}"
        if used + len(item) + 1 > MAX_CHARS:
            break
        lines.append(item)
        used += len(item) + 1
    if rejected:
        head = "НЕ сливать в одну сущность (человек проверил и отклонил):"
        if used + len(head) + 1 <= MAX_CHARS:
            lines.append(head)
            used += len(head) + 1
            for alias, canonical in rejected:
                item = f"  {alias} ≠ {canonical}"
                if used + len(item) + 1 > MAX_CHARS:
                    break
                lines.append(item)
                used += len(item) + 1

    block = "\n".join(lines)
    _cache.update({"at": now, "block": block})
    return block


def _sanitize(pairs: List[tuple]) -> List[tuple]:
    """Убрать пары, которые нельзя показывать модели как правило.

    Словарь копился годами и в нём есть противоречия: одновременно
    «Интернет → сеть Интернет» и «сеть Интернет → Интернет», а также обратные
    пары вида «Банк России → ЦБ» (каноном записано сокращение). Если отдать это
    модели буквально, извлечение получит взаимоисключающие указания — хуже,
    чем без глоссария. Поэтому:
      1) из взаимно обратных пар оставляем одну — ту, у которой канон
         информативнее (больше слов, затем длиннее);
      2) пару, где канон — короткое сокращение, а вариант — развёрнутое
         название, не показываем: направление явно перепутано.
    """
    norm = lambda s: " ".join((s or "").lower().replace("ё", "е").split())  # noqa: E731
    index = {(norm(a), norm(c)): (a, c) for a, c in pairs}
    out: List[tuple] = []
    for (na, nc), (a, c) in index.items():
        rev = index.get((nc, na))
        if rev is not None:
            # канон информативнее — остаётся эта пара; иначе достанется обратной
            if (len(rev[1].split()), len(rev[1])) > (len(c.split()), len(c)):
                continue
        if _looks_inverted(a, c):
            continue
        out.append((a, c))
    return out


def _looks_inverted(alias: str, canonical: str) -> bool:
    """Канон — короткое сокращение, вариант — развёрнутое название."""
    c = (canonical or "").strip()
    a = (alias or "").strip()
    c_short = len(c) <= 6 and c.isupper() and c.replace("-", "").isalpha()
    a_long = len(a.split()) >= 2 and not a.isupper()
    return c_short and a_long


def stats() -> Dict[str, int]:
    approved, rejected = ([], [])
    try:
        approved, rejected = _rows()
    except Exception:  # noqa: BLE001
        pass
    return {"approved": len(approved), "rejected": len(rejected),
            "chars": len(build())}
