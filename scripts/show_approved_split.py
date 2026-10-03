"""Показать финальный расклад approved: OK vs BAD по строгому классификатору."""
import sys
sys.path.insert(0, '/app/data')

from reject_non_alias_pairs import classify
from src.database.entity_alias_models import EntityAlias
from src.database.session import get_session_local

s = get_session_local()()
rows = s.query(EntityAlias).filter(EntityAlias.verdict == "approved").all()
ok = []
bad = []
for r in rows:
    v = classify(r.canonical_name, r.alias)
    (ok if v != "rejected" else bad).append((r.canonical_name, r.alias))

print(f"approved всего: {len(rows)} | OK: {len(ok)} | BAD: {len(bad)}")
print("\n=== BAD (ложные, предлагаю rejected):")
for c, a in bad:
    print(f"  ✗ {c[:46]!r} <-> {a[:40]!r}")
print("\n=== OK (реальные, оставить):")
for c, a in ok:
    print(f"  ✓ {c[:46]!r} <-> {a[:40]!r}")