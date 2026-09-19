"""Локальное судейство ответов стенда через polza.ai.

Почему так, а не судьёй на стенде: deepseek-flash на длинных промптах возвращает пустой
ответ, и судейство срывалось. Здесь ответы уже собраны (файл со стенда), а судит
deешёвая модель из ползы с моей машины — ключ остаётся локальным, на стенде его нет.

    python reports/_scratch/judge_answers_polza.py reports/_scratch/answers_eval.json
"""
import ast
import importlib.util
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path("scripts").resolve()))
spec = importlib.util.spec_from_file_location("polza_helper", "scripts/polza_helper.py")
ph = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ph)

ANSWERS = Path(sys.argv[1] if len(sys.argv) > 1 else "reports/_scratch/answers_eval.json")
OUT = ANSWERS.with_name("answers_scores_polza.json")

# эталоны — из того же набора, что использовался для базы
src = Path("reports/_scratch/pc_eval_20q.py").read_text(encoding="utf-8")
QUESTIONS = None
for node in ast.parse(src).body:
    if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "QUESTIONS" for t in node.targets):
        QUESTIONS = ast.literal_eval(node.value)
        break
expected = {q["id"]: q for q in QUESTIONS}

rows = json.loads(ANSWERS.read_text(encoding="utf-8"))
print(f"ответов в файле: {len(rows)} | эталонов: {len(expected)}")
print(f"модель судьи: {ph.DEFAULT_JUDGE_MODEL}\n")

# Второй аргумент — список id через запятую: судить только их (для добивки пропущенных).
ONLY = {int(x) for x in sys.argv[2].split(",")} if len(sys.argv) > 2 else None
# Прошлые оценки подхватываем, чтобы «добивка» не теряла уже посчитанное.
results = []
if OUT.exists() and ONLY:
    try:
        results = [r for r in json.loads(OUT.read_text(encoding="utf-8")) if r["id"] not in ONLY]
    except Exception:
        results = []


def dump():
    OUT.write_text(json.dumps(sorted(results, key=lambda r: r["id"]), ensure_ascii=False, indent=1),
                   encoding="utf-8")


for i, r in enumerate(sorted(rows, key=lambda x: x["id"]), 1):
    if ONLY and r["id"] not in ONLY:
        continue
    q = expected.get(r["id"])
    if not q or not r.get("answer"):
        print(f"  {i}. id={r['id']}: нет ответа или эталона — пропуск")
        continue
    prompt = (ph.JUDGE_PROMPT.replace("{question}", q["query"])
              .replace("{expected}", q["expected_answer"])
              .replace("{actual}", r["answer"][:4000]))
    cost = 0.0
    text = ""
    # Сбой судьи (таймаут, пустой ответ) не должен терять уже посчитанное и останавливать
    # прогон целиком: пишем результат после каждого вопроса, ошибку фиксируем в комментарии.
    try:
        # thinking.type=disabled: судья — reasoning-модель, и на длинных промптах (вопрос +
        # эталон + ответ системы) она тратила весь лимит токенов на размышления и возвращала
        # пустой content → score=None у 15 из 20 ответов (прогон 19.09.2026).
        raw, cost = ph.chat(prompt, ph.DEFAULT_JUDGE_MODEL, 1000, "judge",
                            extra={"thinking": {"type": "disabled"}})
        text = re.sub(r"```[a-zA-Z]*", "", raw or "").replace("```", "").strip()
        if not text:
            # polza не всегда слушает thinking.type=disabled — повтор с reasoning_effort=none
            # и запасом токенов, чтобы размышления и JSON поместились вместе.
            raw, cost2 = ph.chat(prompt, ph.DEFAULT_JUDGE_MODEL, 3000, "judge-retry",
                                 extra={"reasoning_effort": "none"})
            cost = (cost or 0) + (cost2 or 0)
            text = re.sub(r"```[a-zA-Z]*", "", raw or "").replace("```", "").strip()
    except Exception as e:  # таймаут и прочее
        text = ""
        results.append({"id": r["id"], "score": None, "comment": f"сбой судьи: {type(e).__name__}",
                        "missing": [], "answer_len": r.get("answer_len"),
                        "refused": r.get("refused"), "cost_rub": cost})
        dump()
        print(f"  {i:>2}. id={r['id']:<3} сбой судьи: {type(e).__name__}")
        continue
    score, comment, missing = None, "", []
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(text[start:end + 1])
            score = parsed.get("score")
            comment = str(parsed.get("comment") or "")[:200]
            missing = parsed.get("missing") or []
        except Exception as e:
            comment = f"JSON не разобран: {e}"
    else:
        comment = f"ответ судьи без JSON: {text[:120]!r}"
    results.append({"id": r["id"], "score": score, "comment": comment, "missing": missing,
                    "answer_len": r.get("answer_len"), "refused": r.get("refused"),
                    "cost_rub": cost})
    dump()  # пишем после каждого вопроса: падение судьи не должно терять прогресс
    print(f"  {i:>2}. id={r['id']:<3} score={score} | отказ: {'да' if r.get('refused') else 'нет'} "
          f"| {len(r.get('answer') or '')} симв. | {comment[:60]}")

scores = [x["score"] for x in results if isinstance(x["score"], (int, float))]
if scores:
    avg = sum(scores) / len(scores)
    print(f"\nИТОГ: средний балл {avg:.4f} по {len(scores)} вопросам")
    print(f"  >=0.8: {sum(1 for s in scores if s >= 0.8)} | "
          f"0.6-0.8: {sum(1 for s in scores if 0.6 <= s < 0.8)} | "
          f"<0.6: {sum(1 for s in scores if s < 0.6)}")
    print(f"  ответов с фразой отказа: {sum(1 for x in results if x.get('refused'))}")
print(f"  стоимость судейства: {sum((x.get('cost_rub') or 0) for x in results):.3f} ₽")

dump()
print(f"  записано: {OUT}")
