"""Пересчёт правила пар на УЖЕ сохранённых связях документа (без вызовов модели).

Берём отчёт аудита (reports/bakeoff/audit_v2_doc.json), прогоняем каждую связь через текущую
онтологию: нормализация типа + определение типов концов правилами формы + validate_triple.
Так видно, что даёт правка правила, не тратя токены на повторный прогон.
"""
import json
import sys
from collections import Counter

sys.path.insert(0, ".")
from src.indexing.entity_typing import refine_type
from src.indexing.graph_ontology import ONTOLOGY_EPOCH, normalize_relation, validate_triple

path = sys.argv[1] if len(sys.argv) > 1 else "reports/bakeoff/audit_v2_doc.json"
d = json.load(open(path, encoding="utf-8"))
ok = bad = untyped = 0
reasons: Counter = Counter()
by_type: dict[str, Counter] = {}
for fr in d["ответы_модели"]:
    # Типы концов: сначала из ответа модели (поле entities того же вызова), при промахе — правилом
    # формы записи. В старых отчётах entities нет — тогда работает только правило формы.
    ent_types = {str(e.get("name") or ""): str(e.get("type") or "") for e in fr.get("entities") or []}
    for r in fr["relations"]:
        code = normalize_relation(str(r.get("type") or ""))
        src_name = str(r.get("source") or "")
        dst_name = str(r.get("target") or "")
        src = ent_types.get(src_name) or refine_type(src_name)
        dst = ent_types.get(dst_name) or refine_type(dst_name)
        c = by_type.setdefault(code, Counter())
        if not src or not dst:
            untyped += 1
            c["без типа конца"] += 1
            continue
        good, note = validate_triple(src, code, dst)
        if good:
            ok += 1
            c["проходят"] += 1
        else:
            bad += 1
            c["нарушают"] += 1
            reasons[note[:80]] += 1

total = ok + bad + untyped
print(f"ОНТОЛОГИЯ: epoch {ONTOLOGY_EPOCH} | отчёт: {path}")
print(f"связей в отчёте: {total}")
print(f"  проходят схему:     {ok}")
print(f"  нарушают схему:     {bad}")
print(f"  без типа конца:     {untyped}  (это отдельная задача — v2 не отдаёт типы сущностей)")
print("  причины нарушений:")
for r, n in reasons.most_common(10):
    print(f"    {n:4d}  {r}")
print("\nпо типам связей (проходят / нарушают / без типа):")
for code, c in sorted(by_type.items(), key=lambda kv: -sum(kv[1].values())):
    print(f"  {code:24s} {c.get('проходят', 0):4d} / {c.get('нарушают', 0):4d} / {c.get('без типа конца', 0):4d}")
