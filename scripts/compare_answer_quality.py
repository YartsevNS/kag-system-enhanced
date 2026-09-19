"""Сравнение текущего замера (19.09.2026) с базой 17.09.2026.

База: reports/answer_eval_prompt_completeness_base.txt — там строки «N. score=X.XX | вопрос...»
в порядке id 8..27 (тот же набор из 20 вопросов).
Текущий: reports/_scratch/answers_scores_polza.json (судья polza, thinking off).

Печатает таблицу по вопросам, средние и счёт «лучше/хуже/без изменений».
"""
import ast
import json
import pathlib
import re

BASE_LOG = pathlib.Path("reports/answer_eval_prompt_completeness_base.txt")
NEW_SCORES = pathlib.Path("reports/_scratch/answers_scores_polza.json")
QUESTIONS_SRC = pathlib.Path("reports/_scratch/pc_eval_20q.py")

# порядок вопросов в наборе = порядок строк в логе базы
src = QUESTIONS_SRC.read_text(encoding="utf-8")
questions = None
for node in ast.parse(src).body:
    if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "QUESTIONS" for t in node.targets):
        questions = ast.literal_eval(node.value)
        break
assert questions, "не нашёл QUESTIONS"
ids = [q["id"] for q in questions]

old_scores = [float(m) for m in re.findall(r"score=([0-9.]+)", BASE_LOG.read_text(encoding="utf-8"))]
assert len(old_scores) == len(ids), f"в базовом логе {len(old_scores)} оценок, вопросов {len(ids)}"
old = dict(zip(ids, old_scores))

new_rows = json.loads(NEW_SCORES.read_text(encoding="utf-8"))
new = {r["id"]: r for r in new_rows}

print(f"{'id':>3} | {'17.09':>6} | {'19.09':>6} | дельта | комментарий судьи / отказ")
print("-" * 100)
deltas = []
for qid in ids:
    o = old.get(qid)
    r = new.get(qid) or {}
    n = r.get("score")
    d = (n - o) if isinstance(n, (int, float)) and o is not None else None
    if d is not None:
        deltas.append((qid, d))
    flag = "ОТКАЗ" if r.get("refused") else ""
    comment = (r.get("comment") or "")[:52]
    print(f"{qid:>3} | {o:>6.2f} | {('  —  ' if n is None else f'{n:>6.2f}')} | "
          f"{('  —  ' if d is None else f'{d:>+6.2f}')} | {flag} {comment}")

scored_old = [old[q] for q in ids]
scored_new = [new[q]["score"] for q in ids if isinstance(new.get(q, {}).get("score"), (int, float))]
print("-" * 100)
print(f"средний балл 17.09 (все 20): {sum(scored_old)/len(scored_old):.4f}")
print(f"средний балл 19.09 (оценено {len(scored_new)}): {sum(scored_new)/len(scored_new):.4f}")
if deltas:
    better = [q for q, d in deltas if d > 0.01]
    worse = [q for q, d in deltas if d < -0.01]
    same = [q for q, d in deltas if abs(d) <= 0.01]
    print(f"по {len(deltas)} сравнимым вопросам: лучше {len(better)} ({better}), "
          f"хуже {len(worse)} ({worse}), без изменений {len(same)}")
    print(f"средний балл по сравнимым: 17.09 {sum(old[q] for q,_ in deltas)/len(deltas):.4f} → "
          f"19.09 {sum(new[q]['score'] for q,_ in deltas)/len(deltas):.4f}")
print("отказы «информация не найдена»:", [q for q in ids if new.get(q, {}).get("refused")])
