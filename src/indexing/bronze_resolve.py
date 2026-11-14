"""Silver-слой: нормализация, блокировка и разрешение сырых извлечений Bronze.

Зачем (решение владельца 14.11.2026, п.2): в Bronze лежит сырьё «как пришла модель». Прежде чем
что-то решать про слияние, надо привести записи к сравнимому виду и сгруппировать их так,
чтобы не сравнивать «каждый с каждым».

Что здесь делается (только детерминированно, без модели):
1. Нормализация строк: Unicode NFKC, нижний регистр, схлопывание пробелов, снятие краевой
   пунктуации. Для кодов мер — «АВЗ.1», «АВЗ-1», «авз 1» → «авз.1». Для пунктов — «п. 7.7.1.5»
   и «7.7.1.5» → «7.7.1.5».
2. Ключ кластера по типу сущности: для требования — SHA-256 от нормализованного ТЕКСТА описания
   (замер 14.11.2026: номер пункта модель пишет только у 10 строк из 508, поэтому номер —
   атрибут, а не ключ); для меры — код; для документа — document_key; для структурного
   элемента — document_key|struct_index; для регулятора — код органа из `issuers.canonical`,
   иначе нормализованное имя.
3. Блокировка: кластеры считаются внутри типа, сравнение не выходит за пределы типа.
4. Разрешение (`resolve`): один текст, но разные идентификаторы — не слияние по умолчанию,
   такие записи остаются раздельными и уходят в очередь разбора (`bronze_queue`).
"""

from __future__ import annotations

import hashlib
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
    # пробел внутри кода — тоже разделитель: «авз 1» и «иаф 2 1» → «авз.1», «иаф.2.1»
    s = re.sub(r"\s+", ".", s)
    return s.strip(".")


def norm_clause(value: Any) -> str:
    """Номер пункта: «п. 7.7.1.5», «7.7.1.5» → «7.7.1.5»."""
    return norm_code(value)


_ID_CLAUSE = re.compile(r"^\d+(\.\d+)+$")
_ID_CLAUSE_LIT = re.compile(r"^\d+(\.\d+)+\s*[-–—]\s*[а-яё]$")
_ID_APPENDIX = re.compile(r"^(приложение|прил\.?)\s*[а-яё]$")
_ID_BARE = re.compile(r"^\d+$")
_ID_MEASURE = re.compile(r"^[а-яё]{2,6}\.\d+([-–—]\d+)?$")


def text_hash(value: Any) -> str:
    """SHA-256 от нормализованного текста: ключ требования, а не сам текст."""
    s = norm_text(value)
    return "sha256:" + hashlib.sha256(s.encode("utf-8")).hexdigest()[:40] if s else ""


def requirement_text(payload: Dict[str, Any]) -> str:
    p = payload or {}
    return norm_text(p.get("description") or p.get("text") or p.get("requirement") or "")


def requirement_key(payload: Dict[str, Any]) -> str:
    """Ключ требования — хэш текста описания (номер пункта ключом не является)."""
    return text_hash(requirement_text(payload))


def requirement_id_kind(value: Any) -> str:
    """Вид идентификатора требования: clause / measure_ref / bare_number / word / empty.

    Замер 14.11.2026 на fstek-2014: clause 10 строк, measure_ref 363, bare 39, word 96 —
    номер пункта модель пишет редко, поэтому вид — атрибут, а не признак годности записи.
    """
    s = norm_text(value)
    if not s:
        return "empty"
    if _ID_CLAUSE.match(s) or _ID_CLAUSE_LIT.match(s) or _ID_APPENDIX.match(s):
        return "clause"
    if _ID_BARE.match(s):
        return "bare_number"
    if _ID_MEASURE.match(s):
        return "measure_ref"
    return "word"


def id_compare_key(value: Any) -> str:
    """Идентификатор для сравнения: без регистра, дефисов, подчёркиваний и пробелов.

    «УПД.10-1-а» и «УПД.10-1а» дают один ключ; «ИАФ.2-5» и «ИАФ.2-орг» — разные.
    """
    return re.sub(r"[-–—_\s]", "", norm_text(value))


def requirement_measure_code(payload: Dict[str, Any]) -> str:
    """Код меры, на которую ссылается требование (ИАФ.2-1 → «иаф.2-1»), иначе ""."""
    p = payload or {}
    rid = p.get("requirement-id") or p.get("requirement_id")
    return norm_code(rid) if requirement_id_kind(rid) == "measure_ref" else ""


def regulator_code(payload: Dict[str, Any]) -> str:
    """Код органа из словаря `issuers.canonical` (ФСТЭК России → fstek), иначе ""."""
    p = payload or {}
    name = p.get("name") or p.get("short_name") or p.get("canonical_name") or ""
    if not name:
        return ""
    try:
        from src.indexing import issuers

        code, _canon, _short = issuers.canonical(name)
        return (code or "").strip()
    except Exception:  # noqa: BLE001 - словарь не должен ломать разрешение
        return ""


def entity_branch(type_: str) -> str:
    """К какому правилу относится тип сущности.

    Важно для `regulatory_document`: это ДОКУМЕНТ, а не регулятор — проверка по подстроке
    «regulator» ошибочно уводила 305 строк в ветку регулятора и в очередь `no_issuer_code`.
    """
    t = str(type_ or "")
    if "requirement" in t:
        return "requirement"
    if "measure" in t:
        return "measure"
    if "document" in t or "version" in t:
        return "document"
    if t == "regulator":
        return "regulator"
    return "other"


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
        h = requirement_key(p)
        if h:
            return f"{t}|{h}"
        rid = norm_clause(p.get("requirement-id") or p.get("requirement_id") or "")
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
        code = regulator_code(p)
        if code:
            return f"{t}|code:{code}"
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
            "SELECT id, document_id, clause, kind, type, payload, participants, raw_ent_key "
            "FROM extraction_bronze WHERE run_id = :r"), {"r": run_id}).mappings().all()
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
        out.append({"id": r["id"], "document_id": r["document_id"] or "",
                    "clause": r["clause"] or "", "kind": r["kind"],
                    "type": r["type"] or "", "payload": payload or {},
                    "participants": r["participants"], "raw_key": r["raw_ent_key"] or ""})
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


def resolve(run_id: str, persist: bool = True) -> Dict[str, Any]:
    """Разрешить сырьё прогона в объекты и очередь разбора.

    Правила (решение владельца 14.11.2026):
    - требование опознаётся по хэшу текста; номер пункта и код меры — атрибуты;
    - один текст с РАЗНЫМИ идентификаторами не сливается: объекты остаются раздельными,
      строки уходят в очередь с причиной `identical_text_diff_id`;
    - мера опознаётся по коду; один код с разными названиями — конфликт в очередь;
    - регулятор опознаётся по коду органа, иначе по имени (без кода — в очередь).

    Ничего не удаляется: Bronze остаётся как есть, решения — в `bronze_queue`.
    """
    rows = _rows(run_id)
    ents = [r for r in rows if r["kind"] == "entity"]

    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in ents:
        groups[entity_cluster_key(r["type"], r["payload"], r["raw_key"])].append(r)

    # Все коды мер прогона — нужны, чтобы отличить подмеру с родителем от сироты
    # без автолинковки по префиксу (решение владельца 14.11.2026).
    all_measure_codes: set = set()
    for r in ents:
        if entity_branch(r["type"]) == "measure":
            c = norm_code((r["payload"] or {}).get("code")
                          or (r["payload"] or {}).get("measure_key") or "")
            if c:
                all_measure_codes.add(c)

    objects: List[Dict[str, Any]] = []
    queue: List[Dict[str, Any]] = []
    per_type: Dict[str, Dict[str, int]] = defaultdict(lambda: {"строк": 0, "объектов": 0})
    reasons: Counter = Counter()
    id_kinds: Counter = Counter()
    measures_by_code: Dict[str, set] = defaultdict(set)
    regulators: Counter = Counter()
    merged_groups = 0
    split_groups = 0
    measure_links = 0

    def add_queue(row: Dict[str, Any], key: str, reason: str, detail: str = "") -> None:
        reasons[reason] += 1
        queue.append({
            "bronze_id": row["id"], "document_id": row["document_id"] or "",
            "entity_type": row["type"], "cluster_key": key[:200], "reason": reason,
            "detail": detail[:300],
        })

    for key, items in groups.items():
        t = items[0]["type"]
        branch = entity_branch(t)
        per_type[t]["строк"] += len(items)

        if branch == "requirement":
            ids = {id_compare_key((r["payload"] or {}).get("requirement-id")) for r in items}
            if len(ids) > 1:
                split_groups += 1
                by_id: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
                for r in items:
                    by_id[id_compare_key((r["payload"] or {}).get("requirement-id"))].append(r)
                for cid, sub in by_id.items():
                    objects.append({"key": f"{key}|id:{cid}", "type": t,
                                    "rows": [r["id"] for r in sub],
                                    "text": requirement_text(sub[0]["payload"])})
                    per_type[t]["объектов"] += 1
                for r in items:
                    add_queue(r, key, "identical_text_diff_id",
                              f"id={str((r['payload'] or {}).get('requirement-id'))[:40]}")
            else:
                if len(items) > 1:
                    merged_groups += 1
                objects.append({"key": key, "type": t, "rows": [r["id"] for r in items],
                                "text": requirement_text(items[0]["payload"])})
                per_type[t]["объектов"] += 1

            for r in items:
                p = r["payload"] or {}
                rid = p.get("requirement-id") or p.get("requirement_id")
                kind = requirement_id_kind(rid)
                id_kinds[kind] += 1
                text = requirement_text(p)
                if not text:
                    add_queue(r, key, "empty_description", "")
                elif len(text) < 40:
                    add_queue(r, key, "reference_only", text[:80])
                elif kind == "bare_number":
                    add_queue(r, key, "bare_number", f"id={str(rid)[:40]}")
                elif kind == "word":
                    add_queue(r, key, "word_as_key", f"id={str(rid)[:40]}")
                if requirement_measure_code(p):
                    measure_links += 1

        elif branch == "measure":
            names = {norm_text((r["payload"] or {}).get("name")
                               or (r["payload"] or {}).get("measure_name") or "")
                     for r in items}
            names.discard("")
            code = key.split("|", 1)[1] if "|" in key else key
            measures_by_code[code].update(names)
            objects.append({"key": key, "type": t, "rows": [r["id"] for r in items]})
            per_type[t]["объектов"] += 1
            if len(names) > 1:
                for r in items:
                    add_queue(r, key, "code_name_conflict",
                              "; ".join(sorted(names))[:200])
            # Подмера (АВЗ.1.1) без родительской меры (АВЗ.1) в этом прогоне — в очередь:
            # авто-линковку по префиксу кода НЕ делаем (решение владельца 14.11.2026).
            _parts = code.split(".")
            if len(_parts) >= 3:
                _parent = ".".join(_parts[:-1])
                if _parent not in all_measure_codes:
                    for r in items:
                        add_queue(r, key, "orphan_submeasure", f"нет меры {_parent}")

        elif branch == "document":
            # regulatory_document и document_version: ключ уже посчитан entity_cluster_key
            objects.append({"key": key, "type": t, "rows": [r["id"] for r in items]})
            per_type[t]["объектов"] += 1

        elif branch == "regulator":
            code = regulator_code(items[0]["payload"] or {})
            regulators[code or "—"] += len(items)
            objects.append({"key": key, "type": t, "rows": [r["id"] for r in items]})
            per_type[t]["объектов"] += 1
            if not code:
                for r in items:
                    add_queue(r, key, "no_issuer_code",
                              str((r["payload"] or {}).get("name"))[:80])

        else:
            objects.append({"key": key, "type": t, "rows": [r["id"] for r in items]})
            per_type[t]["объектов"] += 1

    if persist and queue:
        try:
            from src.indexing import bronze

            bronze.queue_add(run_id, queue)
        except Exception:  # noqa: BLE001 - очередь не должна ломать расчёт
            pass

    result: Dict[str, Any] = {
        "run_id": run_id,
        "строк_сущностей": len(ents),
        "объектов": len(objects),
        "сжатие": round(len(ents) / max(1, len(objects)), 2),
        "по_типам": dict(sorted(per_type.items(), key=lambda kv: -kv[1]["строк"])),
        "требования": {
            "групп_по_тексту": sum(1 for k in groups if "requirement" in k.split("|", 1)[0]),
            "слито_групп": merged_groups,
            "разделено_групп": split_groups,
            "по_видам_id": dict(id_kinds),
            "с_кодом_меры": measure_links,
        },
        "меры_с_конфликтом": sum(1 for v in measures_by_code.values() if len(v) > 1),
        "регуляторы": dict(regulators),
        "очередь": dict(reasons),
        "очередь_всего": len(queue),
        "объекты": objects,
        "очередь_строки": queue,
    }
    return result


def report(run_id: str) -> Dict[str, Any]:
    """Только числа: без объектов и строк очереди (для журнала и быстрых проверок)."""
    res = resolve(run_id, persist=False)
    res.pop("объекты", None)
    res.pop("очередь_строки", None)
    return res


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
