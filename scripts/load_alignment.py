"""Загрузчик маппингов выравнивания доменов (alignment).

Читает YAML из config/alignment/<a>_<b>.yaml и пишет связи alignment в TypeDB.
Идемпотентен: существующая пара (те же два объекта) повторно не создаётся.
Пары, у которых объект не найден или нет автора, пропускаются и выводятся отчётом.

Запуск:  python scripts/load_alignment.py [--file config/alignment/sec_it.yaml] [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ENT_QUERY = ('match $x isa {etype}, has {attr} $v; $v like "^{code}"; '
             'fetch {{ "k": $x.ent-key }};')
LINK_EXISTS = ('match $link isa alignment (alignment_source: $a, alignment_target: $b); '
               '$a has ent-key "{ka}"; $b has ent-key "{kb}"; fetch {{ "k": $a.ent-key }};')
INSERT = ('match $a isa {sa_type}, has ent-key "{ka}"; $b isa {ta_type}, has ent-key "{kb}"; '
          'insert $link isa alignment (alignment_source: $a, alignment_target: $b), '
          'has alignment-kind "{kind}", has confidence {conf}, has authored-by "{author}";')

KEY_ATTR = {"security_measure": "code", "it_control": "code", "cii_object": "name"}


def _db() -> Tuple[Any, Any, str]:
    from src.indexing import typedb_facts

    cfg = typedb_facts._cfg()
    return typedb_facts._driver(), cfg, cfg["db"]


_CODES_CACHE: Dict[str, List[Tuple[str, str]]] = {}


def _codes_of(driver, db: str, etype: str) -> List[Tuple[str, str]]:
    """Все (код, ent-key) типа — ищем в Python, потому что TypeDB `like`
    чувствителен к регистру, а коды в базе пишутся то «АВЗ.1», то «авз.1»."""
    from typedb.driver import TransactionType

    if etype in _CODES_CACHE:
        return _CODES_CACHE[etype]
    attr = KEY_ATTR.get(etype, "code")
    q = f'match $x isa {etype}, has {attr} $v; fetch {{ "v": $v, "k": $x.ent-key }};'
    with driver.transaction(db, TransactionType.READ) as tx:
        rows = list(tx.query(q).resolve())
    out: List[Tuple[str, str]] = []
    for r in rows:
        v = r.get("v")
        v = v.get("@value") if isinstance(v, dict) else v
        k = r.get("k")
        k = k.get("@value") if isinstance(k, dict) else k
        if v and k:
            out.append((str(v), str(k)))
    _CODES_CACHE[etype] = out
    return out


def _find(driver, db: str, etype: str, code: str) -> List[str]:
    want = str(code or "").strip().lower()
    if not want:
        return []
    keys = [k for v, k in _codes_of(driver, db, etype) if str(v).strip().lower() == want]
    if not keys:
        # вхождение как префикс: код меры может иметь суффикс (АВЗ.1.1) — точное совпадение
        # уже проверено, здесь только начало строки
        keys = [k for v, k in _codes_of(driver, db, etype)
                if str(v).strip().lower().startswith(want)]
    return sorted(set(keys))[:1]


def run(file: Path, dry_run: bool) -> Dict[str, Any]:
    data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    pairs = data.get("pairs") or []
    driver, cfg, db = _db()
    from typedb.driver import TransactionType

    report = {"total": len(pairs), "created": 0, "exists": 0, "skipped": [],
              "missing_source": 0, "missing_target": 0}
    try:
        for i, p in enumerate(pairs):
            src, tgt = p.get("source") or {}, p.get("target") or {}
            author = str(p.get("authored_by") or "").strip()
            kind = str(p.get("kind") or "close")
            conf = float(p.get("confidence") or 0.5)
            if not author:
                report["skipped"].append(f"{i}: нет автора")
                continue
            s_keys = _find(driver, db, src.get("type"), str(src.get("code") or ""))
            t_keys = _find(driver, db, tgt.get("type"), str(tgt.get("code") or ""))
            if not s_keys:
                report["missing_source"] += 1
                report["skipped"].append(f"{i}: нет источника {src.get('type')}/{src.get('code')}")
                continue
            if not t_keys:
                report["missing_target"] += 1
                report["skipped"].append(f"{i}: нет цели {tgt.get('type')}/{tgt.get('code')}")
                continue
            for ka in s_keys[:1]:
                for kb in t_keys[:1]:
                    q = LINK_EXISTS.format(ka=ka, kb=kb)
                    with driver.transaction(db, TransactionType.READ) as tx:
                        exists = len(list(tx.query(q).resolve())) > 0
                    if exists:
                        report["exists"] += 1
                        continue
                    if dry_run:
                        report["created"] += 1
                        continue
                    iq = INSERT.format(sa_type=src.get("type"), ta_type=tgt.get("type"),
                                       ka=ka, kb=kb, kind=kind, conf=conf, author=author[:120])
                    try:
                        with driver.transaction(db, TransactionType.WRITE) as tx:
                            tx.query(iq).resolve()
                            tx.commit()
                        report["created"] += 1
                    except Exception as exc:  # noqa: BLE001
                        report["skipped"].append(f"{i}: {str(exc).splitlines()[0][:100]}")
    finally:
        driver.close()
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="Загрузить маппинги выравнивания доменов")
    ap.add_argument("--file", default=str(ROOT / "config" / "alignment" / "sec_it.yaml"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    rep = run(Path(a.file), a.dry_run)
    print(f"пар: {rep['total']} | создано: {rep['created']} | уже есть: {rep['exists']} | "
          f"нет источника: {rep['missing_source']} | нет цели: {rep['missing_target']}")
    for s in rep["skipped"]:
        print("  пропущено:", s)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
