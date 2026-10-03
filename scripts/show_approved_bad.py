"""Показать approved-пары, которые классификатор считает ложными (не алиасами)."""
import sys
sys.path.insert(0, '/app/data')

from reject_non_alias_pairs import classify
from src.database.entity_alias_models import EntityAlias
from src.database.session import get_session_local

s = get_session_local()()
rows = s.query(EntityAlias).filter(EntityAlias.verdict == "approved").all()
bad = []
for r in rows:
    v = classify(r.canonical_name, r.alias)
    if v == "rejected":
        bad.append((r.canonical_name, r.alias))
print(f"approved всего: {len(rows)} | из них ложных: {len(bad)}")
for c, a in bad[:40]:
    print(f"  {c[:42]!r} <-> {a[:38]!r}")