"""Сравнение двух замеров: точность (баллы судьи) и скорость (секунды, токены, длина).

    python reports/_scratch/compare_runs.py <answers_A.json> <scores_A.json> <answers_B.json> <scores_B.json> [подпись_A] [подпись_B]

Печатает сводку по обоим прогонам, парное сравнение вопросов и дельты.
"""
import json
import pathlib
import statistics
import sys

a_ans, a_sc, b_ans, b_sc = (pathlib.Path(p) for p in sys.argv[1:5])
label_a = sys.argv[5] if len(sys.argv) > 5 else "A"
label_b = sys.argv[6] if len(sys.argv) > 6 else "B"


def load(p):
    return json.loads(p.read_text(encoding="utf-8"))


def by_id(rows, key="id"):
    return {r[key]: r for r in rows}


A_ans, B_ans = by_id(load(a_ans)), by_id(load(b_ans))
A_sc, B_sc = by_id(load(a_sc)), by_id(load(b_sc))


def summary(answers, scores, label):
    secs = [r["seconds"] for r in answers.values() if r.get("seconds")]
    lens = [r["answer_len"] for r in answers.values() if r.get("answer_len")]
    scored = [r["score"] for r in scores.values() if isinstance(r.get("score"), (int, float))]
    refused = sum(1 for r in answers.values() if r.get("refused"))
    pt = [r["prompt_tokens"] for r in answers.values() if r.get("prompt_tokens")]
    ct = [r["completion_tokens"] for r in answers.values() if r.get("completion_tokens")]
    print(f"\n{label}")
    print(f"  вопросов: {len(answers)} | оценено судьёй: {len(scored)} | средний балл: "
          f"{sum(scored)/len(scored):.4f}" if scored else f"{label}: нет оценок")
    print(f"  время ответа: среднее {statistics.mean(secs):.1f} с | медиана {statistics.median(secs):.1f} с | "
          f"всего {sum(secs):.0f} с")
    print(f"  длина ответа: среднее {statistics.mean(lens):.0f} симв. | отказов: {refused}")
    if pt and ct:
        print(f"  токены: промпт {statistics.mean(pt):.0f} | ответ {statistics.mean(ct):.0f}")


summary(A_ans, A_sc, label_a)
summary(B_ans, B_sc, label_b)

print("\nпо вопросам (id | балл A → балл B | время A → B | длина A → B):")
better = worse = same = 0
d_scores, d_secs = [], []
for qid in sorted(set(A_ans) | set(B_ans)):
    a, b = A_sc.get(qid, {}), B_sc.get(qid, {})
    aa, bb = A_ans.get(qid, {}), B_ans.get(qid, {})
    sa, sb = a.get("score"), b.get("score")
    ta, tb = aa.get("seconds"), bb.get("seconds")
    la, lb = aa.get("answer_len"), bb.get("answer_len")
    mark = ""
    if isinstance(sa, (int, float)) and isinstance(sb, (int, float)):
        d = sb - sa
        d_scores.append(d)
        mark = "лучше" if d > 0.01 else ("хуже" if d < -0.01 else "=")
        better += d > 0.01
        worse += d < -0.01
        same += abs(d) <= 0.01
    if isinstance(ta, (int, float)) and isinstance(tb, (int, float)):
        d_secs.append(tb - ta)
    print(f"  {qid:>3} | {sa if sa is not None else '—':>5} → {sb if sb is not None else '—':>5} "
          f"{mark:<5} | {ta} → {tb} с | {la} → {lb} симв.")

if d_scores:
    print(f"\nпарный счёт по {len(d_scores)} вопросам: лучше {better} | хуже {worse} | без изменений {same}")
    print(f"средняя дельта балла: {statistics.mean(d_scores):+.4f}")
if d_secs:
    print(f"средняя дельта времени: {statistics.mean(d_secs):+.1f} с на вопрос")
