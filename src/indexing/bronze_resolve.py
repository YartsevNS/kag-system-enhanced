"""Silver-слой: нормализация и блокировка сырых извлечений Bronze.

Зачем (решение владельца 14.11.2026, п.2): в Bronze лежит сырьё «как пришла модель» — 2204
сущности на один документ, где одна и та же мера встречается по разу на фрагмент. Прежде чем
что-то решать про слияние, надо привести записи к сравнимому виду и сгруппировать их так,
чтобы не сравнивать «каждый с каждым».

Что здесь делается (только детерминированно, без модели):
1. Нормализация строк: Unicode NFKC, нижний регистр, схлопывание пробелов, снятие краевой
   пунктуации. Для кодов мер — «АВЗ.1», «АВЗ-1», «авз 1» → «авз.1». Для пунктов — «п. 7.7.1.5»
   и «7.7.1.5» → «7.7.1.5».
2. Ключ кластера по типу сущности: для меры — код, для требования — requirement_key,
   для документа — document_key, для структурного элемента — document_key|struct_index,
   для остальных — нормализованное имя.
3. Блокировка: кластеры считаются внутри типа (и внутри structural_key, если он есть),
   поэтому сравнение не выходит за пределы документа/пункта.

Слияние НЕ делается: это следующий шаг (нужны пороги, conflict guard и human-in-the-loop).
Здесь только ответ на вопрос «сколько у нас на самом деле разных объектов в этом сырье».
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List

_WS = re.compile(r"\s+")
_EDGES = re.compile(r"^[«»\"'(\[\s]+|[»\"')\]\s.,;:]+$")
_CODE_SEP = re.compile(r"\s*[.\-\u2013\u2014]\s*")
_CODE_HEADS = re.compile(r"^(п\.?|пп\.?|пункт|раздел|ст\.?|статья|табл\.?|таблица)\s*", re.I)


def norm_text(value: Any) -> str:
    """Сравнимый вид строки: NFKC, нижний регистр, без лишних пробелов и краёв."""
    s = unicodedata.normalize("NFKC", str(value or ""))
    s = s.replace("ё", "е").lower()
    s = _EDGES.sub("", _WS.sub(" ", s.strip()))
    return s[:300]


def norm_code(value: Any) -> str:
    """Код меры/обозначение: «АВЗ.1», «АВЗ-1», «авз 1» → «авз.1»."""
    s = norm_text(value)
    s = _CODE_HEADS.sub("", s)
    s = _CODE_SEP.sub(".", s)
    return s.strip(".")


def norm_clause(value: Any) -> str:
    """Номер пункта: «п. 7.7.1.5», «7.7.1.5» → «7.7.1.5»."""
    return norm_code(value)


def entity_cluster_key(type_: str, payload: Dict[str, Any], raw_key: str = "") -> str:
    """Ключ, по которому записи считаются одним объектом (без fuzzy).

    Тип входит в ключ всегда: «проверка» как требование и как мера — разные объекты.
    """
    p = payload or {}
    t = str(type_ or "")
    doc = norm_text(p.get("document_key") or "")

    if "measure" in t:
        code = norm_code(p.get("code") or p.get("measure_key") or "")
        return f"{t}|{code}" if code else f"{t}|{norm_text(raw_key)}"
    if "requirement" in t:
        rk = norm_text(p.get("requirement_key") or "")
        rid = norm_clause(p.get("requirement-id") or p.get("requirement_id") or "")
        if rk:
            return f"{t}|{rk}"
        if rid:
            return f"{t}|{doc}|{rid}"
        return f"{t}|{norm_text(raw_key)}"
    if "document" in t or "version" in t:
        dk = norm_text(p.get("document_key") or p.get("kag_document_id") or "")
        vs = norm_text(p.get("version_string") or "")
        return f"{t}|{dk}|{vs}" if vs else f"{t}|{dk}"
    if "structural_element" in t:
        idx = norm_clause(p.get("struct_index") or p.get("index") or "")
        return f"{t}|{doc}|{idx}" if idx else f"{t}|{norm_text(raw_key)}"
    if "regulator" in t:
        return f"{t}|{norm_text(p.get('name') or p.get('short_name') or raw_key)}"
    return f"{t}|{norm_text(raw_key) or norm_text(p.get('name') or '')}"


def relation_cluster_key(type_: str, participants: Dict[str, Any]) -> str:
    """Ключ связи: тип + отсортированные роли с ключами участников (нормализованными)."""
    parts = participants or {}
    if isinstance(parts, str):
        try:
            parts = json.loads(parts)
        except Exception:  # noqa: BLE001
            parts = {}
    items = sorted((str(r), norm_text(k)) for r, k in dict(parts).items())
    return f"{type_}|" + "|".join(f"{r}={k}" for r, k in items)


def _rows(run_id: str) -> List[Dict[str, Any]]:
    from sqlalchemy import text

    from src.database.session import get_session_local

    db = get_session_local()()
    try:
        rows = db.execute(text(
            "SELECT kind, type, payload, participants, raw_ent_key FROM extraction_bronze "
            "WHERE run_id = :r"), {"r": run_id}).mappings().all()
    finally:
        db.close()
    out = []
    for r in rows:
        payload = r["payload"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:  # noqa: BLE001
                payload = {}
        out.append({"kind": r["kind"], "type": r["type"] or "",
                    "payload": payload or {}, "participants": r["participants"],
                    "raw_key": r["raw_ent_key"] or ""})
    return out


def clusters(run_id: str, top: int = 10) -> Dict[str, Any]:
    """Сколько разных объектов в сырье прогона: по типам, с примерами крупных кластеров."""
    rows = _rows(run_id)
    by_kind: Dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        if r["kind"] == "entity":
            key = entity_cluster_key(r["type"], r["payload"], r["raw_key"])
        else:
            key = relation_cluster_key(r["type"], r["participants"])
        by_kind[r["kind"]][key] += 1

    out: Dict[str, Any] = {"run_id": run_id, "kinds": {}}
    for kind, counter in by_kind.items():
        per_type: Dict[str, Dict[str, int]] = {}
        for key, n in counter.items():
            t = key.split("|", 1)[0]
            d = per_type.setdefault(t, {"строк": 0, "кластеров": 0})
            d["строк"] += n
            d["кластеров"] += 1
        out["kinds"][kind] = {
            "строк_всего": sum(counter.values()),
            "кластеров": len(counter),
            "по_типам": dict(sorted(per_type.items(), key=lambda kv: -kv[1]["строк"])),
            "крупнейшие": [{"ключ": k[:120], "строк": n} for k, n in counter.most_common(top)],
        }

    ents = by_kind.get("entity") or Counter()
    if ents:
        singles = sum(1 for n in ents.values() if n == 1)
        out["сущности"] = {
            "строк": sum(ents.values()),
            "кластеров": len(ents),
            "одиночных_кластеров": singles,
            "средний_размер": round(sum(ents.values()) / len(ents), 2),
            "сжатие": round(sum(ents.values()) / len(ents), 2),
        }
    return out


def by_prompt(run_ids: Iterable[str]) -> Dict[str, Any]:
    """Эффективность по версиям промпта: для сравнения версий между собой.

    Считается по Bronze: сколько сырья дала каждая версия промпта на прогон.
    """
    from sqlalchemy import text

    from src.database.session import get_session_local

    ids = [r for r in run_ids if r]
    if not ids:
        return {"versions": []}
    db = get_session_local()()
    try:
        rows = db.execute(text("""
            SELECT b.prompt_version, b.run_id, count(e.id) AS raw
            FROM bronze_runs b LEFT JOIN extraction_bronze e ON e.run_id = b.run_id
            WHERE b.run_id = ANY(:ids)
            GROUP BY b.prompt_version, b.run_id
        """), {"ids": ids}).mappings().all()
    finally:
        db.close()

    agg: Dict[int, Dict[str, Any]] = {}
    for r in rows:
        v = int(r["prompt_version"] or 0)
        d = agg.setdefault(v, {"версия": v, "прогонов": 0, "сырья_всего": 0})
        d["прогонов"] += 1
        d["сырья_всего"] += int(r["raw"] or 0)
    for v, d in agg.items():
        d["сырья_на_прогон"] = round(d["сырья_всего"] / max(1, d["прогонов"]), 1)
    return {"versions": sorted(agg.values(), key=lambda x: x["версия"])}
