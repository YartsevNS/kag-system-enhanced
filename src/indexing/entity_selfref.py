"""Свои обозначения документа: не создавать сущности из колонтитула.

Проблема (замер 2026-09-13).

Обозначение документа стоит в колонтитуле почти каждой страницы, поэтому попадает в КАЖДЫЙ чанк
своего документа и получает максимум упоминаний. В графе такое самообозначение становится самым
крупным узлом: в прошлом графе 81% узлов были справочными типами (legal_term 4401 + document_ref
2528 из 8506), а самые частые обозначения в текстах чанков — «34.10—2012» (15), «34.11—2012» (13),
«бфбо-1.9-2024» (11), «1323565.1.020—2020» (10), «57580.1—2017» (7). В представлении по документу
(/kg) такой узел вытесняет смысловые сущности, и любое ранжирование по числу упоминаний смещается.

При этом имя файла в промпт извлечения НЕ подставляется (в промпт уходит только текст чанка,
`sample = chunk_text[:1000]`) — то есть сущность рождается из текста, а обозначение в имени файла
лишь повторяет то, что и так стоит в колонтитуле.

Решение.

Считаем варианты «своего» обозначения из имени файла и отбрасываем сущности (и связи, где они
встречаются), которые на эти варианты совпадают. Ссылки на ДРУГИЕ документы не трогаются:
сравнение идёт по точным вариантам и по «свой номер + год», а не по произвольной подстроке.

Примеры (имя файла → что отбрасываем):
    r-1323565.1.pdf      → «Р 1323565.1», «1323565.1», «1323565.1—2017»
    gost-r-34.pdf        → «ГОСТ Р 34», «34», «34—2012» (варианты без типа и без года)
    sto-br-ibbos-1.9.pdf → «СТО БР ИББО 1.9»
    Квитанции.pdf        → «Квитанции» (обычно сущности не создаётся, правило безвредно)
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

# Дефисы разных видов → обычный дефис: «34.10—2012» и «34.10-2012» это одно обозначение.
_DASHES = "–—−―"
# Ведущие «типы документа» в имени файла: gost-r-34 → 34, r-1323565.1 → 1323565.1
_TYPE_PREFIXES = (
    "gost-r-", "gost-r ", "gost-", "gost ", "r-iso-", "iso-", "mek-",
    "sto-br-", "sto-", "r-", "p-",
)
# Префикс хранения файла: «<uuid>_имя.pdf» (см. ids.py)
_UPLOAD_PREFIX_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}_"
)
# Год в конце обозначения: «1323565.1.004-2017», «34.10-2012»
_YEAR_TAIL_RE = re.compile(r"^(?P<core>.+?)-?(?P<year>(19|20)\d{2})$")
# «Ядро» обозначения: достаточно длинное и с цифрами — чтобы не ловить общие слова
# Ядро обозначения: минимум 4 символа с цифрами («34.10», «57580», «1323565.1»).
# Меньше — слишком общие совпадения (например «1.9» ловил бы лишнее).
_CORE_MIN_LEN = 4


def _norm(value: str) -> str:
    """Нормализовать обозначение: регистр, дефисы, пробелы, знаки."""
    s = (value or "").lower()
    for d in _DASHES:
        s = s.replace(d, "-")
    s = s.replace("№", "")
    s = "".join(ch for ch in s if ch.isalnum() or ch in ".-")
    return s.strip("-.")


def self_reference_keys(filename: str, extra: Optional[Sequence[str]] = None) -> Tuple[Set[str], Set[str]]:
    """Варианты «своего» обозначения документа.

    Возвращает (keys, cores):
      keys  — точные варианты (нормализованные), которые надо отбросить целиком;
      cores — «ядра» обозначения (номер без года/типа) для вариантов вида «ядро + год».
    """
    stem = _UPLOAD_PREFIX_RE.sub("", (filename or "").strip())
    if "." in stem:
        stem = stem.rsplit(".", 1)[0]
    norm = _norm(stem)
    keys: Set[str] = {norm} if norm else set()

    # Срез ведущего типа документа: «r-1323565.1» → «1323565.1», «gost-r-34» → «34».
    # Регексп, а не startswith: _norm срезает хвостовой дефис, и «r-» превращалось в «r»
    # (получался ключ «-1323565.1» — живая проверка это поймала).
    # Снимаем тип ЦИКЛОМ: «gost-r-34-2012» → «34-2012» (за один проход снялось бы только
    # «gost-», и ключ остался бы «r-34-2012» — живая проверка это поймала).
    _type_re = re.compile(r"^(gost|гост|sto|сто|r|р|iso|исо|мэк|mek)[\s.-]+", re.IGNORECASE)
    stripped = norm
    while True:
        nxt = _type_re.sub("", stripped, count=1)
        if nxt == stripped:
            break
        stripped = nxt
    if stripped and stripped != norm:
        keys.add(stripped)

    for value in list(extra or []):
        nv = _norm(str(value))
        if nv:
            keys.add(nv)

    # Вариант без года: «1323565.1.004-2017» → «1323565.1.004», «34.10-2012» → «34.10»
    for key in list(keys):
        m = _YEAR_TAIL_RE.match(key)
        if m and len(m.group("core")) >= 4:
            keys.add(m.group("core"))

    # Ядро — числовая часть обозначения: «1323565.1» из «r-1323565.1»,
    # «57580» из «gost57580». Ловит варианты «ядро + год» (1323565.1—2017).
    cores = set()
    for k in keys:
        m = re.search(r"\d[\d.\-]*\d|\d", k)
        if m and len(m.group(0)) >= 3:
            cores.add(m.group(0).strip(".-"))
    cores = {c for c in cores if len(c) >= _CORE_MIN_LEN and any(ch.isdigit() for ch in c)}
    return {k for k in keys if k}, cores


def is_self_reference(name: str, keys: Set[str], cores: Set[str]) -> bool:
    """Это обозначение самого документа (а не ссылка на другой документ)?"""
    n = _norm(name)
    if not n:
        return False
    if n in keys:
        return True
    # «ядро + год/редакция»: 1323565.1.004-2017 при ядре 1323565.1.004.
    # Требуем, чтобы после ядра шёл НЕ цифра (иначе 1323565.1.020 поймается ядром 1323565.1).
    # Сопоставление по ядру — только «ядро + год» и «ядро + .N» (номер части):
    # 1323565.1 при ключе 1323565.1 ловит 1323565.1-2017, но НЕ ловит 1323565.1.020
    # (ссылка на соседний документ серии должна остаться в графе).
    for core in cores:
        if not n.startswith(core):
            continue
        rest = n[len(core):]
        if not rest:
            return True
        if re.fullmatch(r"-(?:19|20)\d{2}", rest):
            return True
    return False


def filter_self_references(
    entities: Iterable[Dict], relations: Iterable[Dict], filename: str,
    extra: Optional[Sequence[str]] = None,
) -> Tuple[List[Dict], List[Dict], List[str]]:
    """Убрать сущности-самообозначения и связи, которые на них ссылаются.

    Возвращает (entities, relations, dropped_names).
    """
    keys, cores = self_reference_keys(filename, extra)
    if not keys:
        return list(entities), list(relations), []

    kept: List[Dict] = []
    dropped: List[str] = []
    for e in entities or []:
        name = str((e or {}).get("name") or "")
        if is_self_reference(name, keys, cores):
            dropped.append(name)
        else:
            kept.append(e)

    if not dropped:
        return kept, list(relations or []), []

    dropped_norm = {_norm(d) for d in dropped}
    kept_rels = []
    for r in relations or []:
        src = _norm(str((r or {}).get("source") or ""))
        tgt = _norm(str((r or {}).get("target") or ""))
        if src in dropped_norm or tgt in dropped_norm:
            continue
        kept_rels.append(r)
    return kept, kept_rels, dropped
