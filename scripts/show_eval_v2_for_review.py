"""Показать набор v2 для просмотра владельцем + собрать читаемую версию в markdown."""
import json
from pathlib import Path

SRC = Path("scripts/eval_questions_v2.json")
OUT = Path("reports/eval_v2_review.md")

d = json.loads(SRC.read_text(encoding="utf-8"))
qs = d["questions"]
legacy = int(d.get("legacy_count") or 7)

singles = [q for q in qs[legacy:] if not q.get("control") and len(q.get("relevant_document_ids") or []) <= 1]
compares = [q for q in qs if len(q.get("relevant_document_ids") or []) > 1 and not q.get("control")]
controls = [q for q in qs if q.get("control")]

print("ИСТОРИЧЕСКИЕ (7, не трогаем — по ним сравнимы прошлые прогоны):")
for i, q in enumerate(qs[:legacy], 1):
    print(f"  {i:2d}. {q['query']}")

print()
print("НОВЫЕ ОДИНОЧНЫЕ (20): проверь формулировку и что ответ именно в этом документе")
for i, q in enumerate(singles, legacy + 1):
    note = str(q.get("notes") or "").replace("черновик v2: ", "")
    print(f"  {i:2d}. {q['query'][:66]:68s} → {note}")

print()
print("СРАВНИТЕЛЬНЫЕ (3): ответ = оба документа")
for q in compares:
    print(f"   - {q['query'][:80]}")

print()
print("КОНТРОЛЬНЫЕ (3): ответа в корпусе нет")
for q in controls:
    print(f"   - {q['query']}")

lines = [
    "# Набор вопросов v2 — что проверить",
    "",
    f"Всего {len(qs)}: 7 исторических + {len(singles)} новых одиночных + "
    f"{len(compares)} сравнительных + {len(controls)} контрольных.",
    f"Источник (правит машина): `{SRC}`",
    "",
    "## Новые одиночные",
    "",
    "Проверь по каждому: (а) ты бы спросил так же? (б) ответ действительно в этом документе?",
    "",
]
for i, q in enumerate(singles, legacy + 1):
    lines.append(f"{i}. `{q['query']}` — {q['notes']}")
lines += ["", "## Сравнительные (ответ = оба документа)", ""]
for q in compares:
    lines.append(f"- `{q['query']}` — {q['notes']}")
lines += ["", "## Контрольные (ответа нет — смотрим уверенность системы)", ""]
for q in controls:
    lines.append(f"- `{q['query']}`")
lines += [
    "",
    "## Как сообщить правки",
    "",
    "Скажи номерами: «3, 11 — переформулировать; у 7 ответ в другом документе; 15 убрать».",
]
OUT.write_text("\n".join(lines), encoding="utf-8")
print()
print(f"  читаемая версия: {OUT}")
