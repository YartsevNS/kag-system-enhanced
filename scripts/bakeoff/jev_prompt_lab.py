"""Лаборатория формулировок для JEV: какая постановка вопроса даёт устойчивый и верный ответ.

Задача (от владельца): подобрать формулировки, найти зависимости для наилучшего распознавания —
на 5 документах, с повторами, разными вариантами вопросов. Это не разовая разметка, а опыт:
дальше по этим зависимостям строятся вопросы для всего корпуса.

Что мерим (честно, без «нравится/не нравится»):
  * СТАБИЛЬНОСТЬ — один и тот же вариант, повторённый N раз: совпал ли ответ (и как менялась уверенность);
  * РАЗЛИЧИМОСТЬ формулировок — совпадают ли варианты между собой (если вопрос про одно, они не должны
    расходиться на пустом месте);
  * УВЕРЕННОСТЬ — средняя и доля уверенных (≥0,9) по варианту;
  * СОГЛАСИЕ С ОЖИДАНИЕМ — где у документа есть внешняя опора (название/тип), смотрим, попадает ли ответ.

Никаких выводов «на глаз»: на выходе таблица «документ × вариант» и сводка по вариантам.

Ключ JEV только из окружения (POLZA_API_KEY / HERMES_CUSTOM_POLZA_API_KEY), в файлы не пишется.

Запуск:
  python scripts/bakeoff/jev_prompt_lab.py --docs eval/label_docs_input.json \
      --docs-ids 8ef6b9c4,… --repeats 3 --out eval/jev_lab.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

URL = "https://polza.ai/api/v1/systemone"
MODEL = "typesafe/jev"


# ── Варианты постановки вопроса про ТЕМУ ─────────────────────────────────────
# Каждый вариант — гипотеза о том, от чего зависит ответ: от вида критериев (определения/названия),
# от явной оговорки про «иное», от разведения «тема» и «нормативность», от объёма текста.

def variants() -> dict:
    from src.indexing import document_topics as dt

    defs = {c: (m.get("definition") or m.get("title", c)) for c, m in dt.RUBRICS.items()}
    titles = {c: m.get("title", c) for c, m in dt.RUBRICS.items()}
    both = {c: f"{m.get('title', c)} — {m.get('definition', '')}" for c, m in dt.RUBRICS.items()}

    return {
        "A_как_сейчас": {
            "questions": {"тема": {"type": "choice",
                                   "instructions": "О чём документ ЦЕЛИКОМ? Выбери одну основную тему.",
                                   "criteria": defs}},
        },
        "B_только_названия": {
            "questions": {"тема": {"type": "choice",
                                   "instructions": "О чём документ ЦЕЛИКОМ? Выбери одну основную тему.",
                                   "criteria": titles}},
        },
        "C_название_и_определение": {
            "questions": {"тема": {"type": "choice",
                                   "instructions": "О чём документ ЦЕЛИКОМ? Выбери одну основную тему.",
                                   "criteria": both}},
        },
        "D_other_только_в_крайнем_случае": {
            "questions": {"тема": {"type": "choice",
                                   "instructions": ("Выбери наиболее подходящую тему из списка. "
                                                    "Значение other ставь ТОЛЬКО если ни одна тема "
                                                    "не подходит по смыслу."),
                                   "criteria": defs}},
        },
        "E_тема_против_нормативности": {
            "questions": {"тема": {"type": "choice",
                                   "instructions": ("Определи ПРЕДМЕТНУЮ ОБЛАСТЬ документа — о чём он "
                                                    "по содержанию. Документ может быть нормативным "
                                                    "(ГОСТ, приказ, положение), но это не делает его "
                                                    "темой юриспруденцию: для ГОСТ по криптографии "
                                                    "тема — информационная безопасность."),
                                   "criteria": defs}},
        },
        "F_короткий_текст": {
            "questions": {"тема": {"type": "choice",
                                   "instructions": "О чём документ ЦЕЛИКОМ? Выбери одну основную тему.",
                                   "criteria": defs}},
            "_state_limit": 900,
        },
        # ── Второй заход: комбинируем то, что выиграло в первом ──────────────
        # E (разведение темы и нормативности) дала устойчивость 5/5 и уверенность 0,90 против 0,66;
        # B (названия без определений) — 5/5 и 0,80 против 0,66/0,71 у определений. Проверяем:
        # G — формулировка E + критерии-названия (лучшее с лучшим);
        # H — то же плюс оговорка про «иное»;
        # I — формулировка E И вопрос про нормативность РЯДОМ (в продукте спрашиваем их вместе:
        #     возможно, часть путаницы была от того, что тема спрашивалась В ОДИНОЧКУ и модель
        #     сама «дотягивала» документ до юриспруденции по нормативному признаку);
        # J — формулировка E + критерии-названия + оговорка «иное» + текст пошире.
        "G_E_и_названия": {
            "questions": {"тема": {"type": "choice",
                                   "instructions": ("Определи ПРЕДМЕТНУЮ ОБЛАСТЬ документа — о чём он "
                                                    "по содержанию. Документ может быть нормативным "
                                                    "(ГОСТ, приказ, положение), но это не делает его "
                                                    "темой юриспруденцию: для ГОСТ по криптографии "
                                                    "тема — информационная безопасность."),
                                   "criteria": titles}},
        },
        "H_E_названия_и_иное": {
            "questions": {"тема": {"type": "choice",
                                   "instructions": ("Определи ПРЕДМЕТНУЮ ОБЛАСТЬ документа — о чём он "
                                                    "по содержанию (не путать с нормативностью: ГОСТ "
                                                    "по криптографии — это информационная "
                                                    "безопасность). Значение other ставь только "
                                                    "если ни одна тема не подходит."),
                                   "criteria": titles}},
        },
        "I_E_вместе_с_нормативностью": {
            "questions": {
                "тема": {"type": "choice",
                         "instructions": ("Определи ПРЕДМЕТНУЮ ОБЛАСТЬ документа — о чём он по "
                                          "содержанию. Нормативность документа (обязательные "
                                          "требования или справка) к теме не относится."),
                         "criteria": titles},
                "нормативность": {"type": "choice",
                                  "instructions": "Это обязательные требования, рекомендации или справка?",
                                  "criteria": dict(__import__("src.indexing.document_facets",
                                                              fromlist=["FACETS"])
                                                   .FACETS["normative_force"]["values"])},
            },
        },
        "J_E_названия_иное_и_шире": {
            "questions": {"тема": {"type": "choice",
                                   "instructions": ("Определи ПРЕДМЕТНУЮ ОБЛАСТЬ документа (о чём он по "
                                                    "содержанию, а не каким документом является). "
                                                    "ГОСТ по криптографии — информационная "
                                                    "безопасность, инструкция по охране труда — "
                                                    "пожарная безопасность и охрана труда, "
                                                    "накладная — экономика и финансы. Значение other "
                                                    "ставь только если ни одна тема не подходит."),
                                   "criteria": titles}},
            "_state_limit": 4000,
        },
        # ── Третий заход: недостающая комбинация ─────────────────────────────
        # В первых двух заходах лучшими оказались РАЗНЫЕ вещи: формулировка с разведением
        # «тема/нормативность» (E: 5/5, уверенность 0,90) и формулировка с примерами и оговоркой про
        # «иное» (J: больше всех уверенных ответов 9/15). Комбинацию «разведение + примеры + иное +
        # ОПРЕДЕЛЕНИЯ» не проверяли — это K. Плюс L: то же, но без примеров, чтобы понять, что даёт
        # вклад — примеры или оговорка.
        "K_всё_вместе_определения": {
            "questions": {"тема": {"type": "choice",
                                   "instructions": ("Определи ПРЕДМЕТНУЮ ОБЛАСТЬ документа — о чём он по "
                                                    "содержанию, а не каким документом является "
                                                    "(нормативность к теме не относится). Примеры: ГОСТ "
                                                    "по криптографии — информационная безопасность; "
                                                    "инструкция по охране труда — пожарная "
                                                    "безопасность и охрана труда; накладная — "
                                                    "экономика и финансы. Значение other ставь только "
                                                    "если ни одна тема не подходит."),
                                   "criteria": defs}},
            "_state_limit": 4000,
        },
        "L_определения_и_иное": {
            "questions": {"тема": {"type": "choice",
                                   "instructions": ("Определи ПРЕДМЕТНУЮ ОБЛАСТЬ документа — о чём он по "
                                                    "содержанию, а не каким документом является. "
                                                    "Нормативность документа (обязательные требования "
                                                    "или справка) к теме не относится. Значение other "
                                                    "ставь только если ни одна тема не подходит."),
                                   "criteria": defs}},
            "_state_limit": 4000,
        },
    }


def ask(key: str, text: str, questions: dict, timeout: int = 180) -> dict:
    body = json.dumps({"model": MODEL, "state": text[:6000], "questions": questions},
                      ensure_ascii=False).encode()
    req = urllib.request.Request(
        URL, data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs", default="eval/label_docs_input.json")
    ap.add_argument("--docs-ids", default="", help="через запятую: первые 8 знаков id достаточно")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--sleep", type=float, default=1.0)
    ap.add_argument("--out", default="eval/jev_lab.jsonl")
    ap.add_argument("--only", default="", help="через запятую: какие варианты брать (по началу имени)")
    args = ap.parse_args()

    key = os.environ.get("POLZA_API_KEY") or os.environ.get("HERMES_CUSTOM_POLZA_API_KEY")
    if not key:
        print("нет ключа: задайте POLZA_API_KEY в окружении")
        return 2

    pool_docs = json.loads(Path(args.docs).read_text(encoding="utf-8"))
    want = [w.strip() for w in args.docs_ids.split(",") if w.strip()]
    if want:
        pool_docs = [d for d in pool_docs if any(d["document_id"].startswith(w) for w in want)]
    if not pool_docs:
        print("документы не выбраны (--docs-ids)")
        return 2
    var = variants()
    if args.only:
        want_v = [w.strip() for w in args.only.split(",") if w.strip()]
        var = {k: v for k, v in var.items() if any(k.startswith(w) for w in want_v)}
        if not var:
            print("варианты не найдены (--only)")
            return 2
    tasks = [(d, name) for d in pool_docs for name in var]
    print(f"документов: {len(pool_docs)}; вариантов: {len(var)}; повторов: {args.repeats}; "
          f"вызовов: {len(tasks) * args.repeats}")

    out_lines, cost, t0 = [], 0.0, time.time()
    lock_rows = []

    def run(job):
        doc, name = job
        qs = dict(var[name]["questions"])
        text = str(doc.get("text") or "")
        limit = var[name].get("_state_limit")
        if limit:
            text = text[:limit]
        rows = []
        for rep in range(1, args.repeats + 1):
            try:
                if args.sleep:
                    time.sleep(args.sleep)
                data = ask(key, text, qs)
                a = (data.get("answers") or {}).get("тема") or {}
                rows.append({"document_id": doc["document_id"], "variant": name, "repeat": rep,
                             "choice": a.get("choice"), "confidence": a.get("confidence"),
                             "usage": data.get("usage") or {}, "ok": True})
            except urllib.error.HTTPError as e:
                rows.append({"document_id": doc["document_id"], "variant": name, "repeat": rep,
                             "ok": False, "error": f"HTTP {e.code}: {e.read().decode('utf-8','replace')[:100]}"})
            except Exception as e:  # noqa: BLE001
                rows.append({"document_id": doc["document_id"], "variant": name, "repeat": rep,
                             "ok": False, "error": f"{type(e).__name__}: {str(e)[:100]}"})
        return rows

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for rows in pool.map(run, tasks):
            out_lines.extend(rows)
            cost += sum(float((r.get("usage") or {}).get("cost_rub") or 0) for r in rows)
            print(f"  готово задач {len(out_lines)//args.repeats - 0}/{len(tasks)} | расход {cost:.3f} ₽ "
                  f"| {time.time()-t0:.0f} с")

    Path(args.out).write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in out_lines),
                              encoding="utf-8")
    ok = [r for r in out_lines if r.get("ok")]
    print(f"\nответов: {len(ok)} из {len(out_lines)}; расход {cost:.3f} ₽; время {time.time()-t0:.0f} с")
    bad = [r for r in out_lines if not r.get("ok")]
    if bad:
        print("ошибки:", Counter((r.get("error") or "")[:40] for r in bad).most_common(3))

    # ── Таблица «документ × вариант» ─────────────────────────────────────────
    by_doc = defaultdict(dict)
    for r in ok:
        by_doc[r["document_id"]][r["variant"]] = by_doc[r["document_id"]].get(r["variant"], [])
        by_doc[r["document_id"]][r["variant"]].append((r["choice"], r["confidence"]))
    print("\nдокумент × вариант (ответ · уверенность, ★ = все повторы совпали):")
    for did, per_var in by_doc.items():
        print(f"\n  {did[:12]}")
        for name in var:
            cells = per_var.get(name) or []
            if not cells:
                print(f"    {name:<34} —")
                continue
            choices = [c for c, _ in cells]
            confs = [f for _, f in cells if isinstance(f, (int, float))]
            stable = "★" if len(set(choices)) == 1 else "≠"
            avg = sum(confs) / len(confs) if confs else 0
            print(f"    {name:<34} {stable} {str(choices[0]):<22} средняя {avg:.2f} "
                  f"[{', '.join(str(c) for c in choices)}]")

    # ── Сводка по вариантам ─────────────────────────────────────────────────
    print("\nсводка по вариантам: доля устойчивых (все повторы совпали), средняя уверенность, "
          "доля уверенных ≥0,9")
    for name in var:
        stable = total = 0
        confs = []
        for per_var in by_doc.values():
            cells = per_var.get(name) or []
            if not cells:
                continue
            total += 1
            if len({c for c, _ in cells}) == 1:
                stable += 1
            confs += [f for _, f in cells if isinstance(f, (int, float))]
        avg = sum(confs) / len(confs) if confs else 0
        sure = sum(1 for f in confs if f >= 0.9)
        print(f"   {name:<34} устойчиво {stable}/{total}   средняя {avg:.2f}   "
              f"уверенных {sure}/{len(confs)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
