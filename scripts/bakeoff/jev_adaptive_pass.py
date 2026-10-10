"""Адаптивный прогон JEV по остатку слабых значений: заостряется профиль или документ честно спорный.

Логика (продолжение лаборатории формулировок):
  * берём документы, где по прошлому прогону уверенность профиля низкая (<0,6 по любому вопросу);
  * спрашиваем КАЖДЫЙ такой документ ДВУМЯ формулировками — «чемпионом» (победитель лаборатории) и
    АДАПТИВНОЙ (та же постановка + правило разрешения близких случаев, подобранное под класс
    документа: для темы — «если две темы, выбери ту, которой посвящена большая часть текста»;
    для нормативности и предмета защиты — разведение значений прямо в тексте вопроса);
  * повторяем каждую формулировку N раз, чтобы отделить спорный документ от шаткой формулировки.

Как читается результат:
  * профиль заострился (уверенность выросла) и обе формулировки сошлись — формулировка была виновата,
    значение можно писать;
  * обе формулировки устойчивы, но расходятся — документ спорный по существу, к владельцу списком;
  * адаптивная заострилась, чемпион нет — берём адаптивную как новую рабочую.

Запуск:
  python scripts/bakeoff/jev_adaptive_pass.py --labels eval/labels_docs_v0_labform.jsonl \
      --docs eval/label_docs_input.json --repeats 2 --out eval/jev_adaptive.jsonl
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

TIEBREAK = ("Если документ можно отнести к двум темам, выбери ту, которой посвящена БОЛЬШАЯ ЧАСТЬ "
            "текста.")


def formulations() -> dict:
    """Чемпион (из лаборатории) против адаптивной постановки, отдельно по каждому вопросу."""
    from src.indexing import document_facets as df
    from src.indexing import document_topics as dt

    defs = {c: (m.get("definition") or m.get("title", c)) for c, m in dt.RUBRICS.items()}
    tema_champ = ("Определи ПРЕДМЕТНУЮ ОБЛАСТЬ документа — о чём он по содержанию, а не каким "
                  "документом является. Нормативность документа (обязательные требования или справка) "
                  "к теме не относится. Значение other ставь только если ни одна тема не подходит.")
    ps_champ = ("Что защищается по смыслу документа? Если документ не про защиту — выбери "
                "not_applicable.")
    nf_champ = "Это обязательные требования, рекомендации или справочная информация?"
    return {
        "тема": {
            "чемпион": {"type": "choice", "instructions": tema_champ, "criteria": defs},
            "адаптив": {"type": "choice", "instructions": tema_champ + " " + TIEBREAK,
                        "criteria": defs},
        },
        "предмет_защиты": {
            "чемпион": {"type": "choice", "instructions": ps_champ,
                        "criteria": dict(df.FACETS["protection_subject"]["values"])},
            "адаптив": {"type": "choice",
                        "instructions": ("Что защищается ПО СМЫСЛУ документа: данные — информация, "
                                         "персональные данные, сведения; сети — связь, каналы, сети "
                                         "передачи; люди — сотрудники и физические лица; серверы — "
                                         "оборудование, инфраструктура, помещения; сервисы — услуги, "
                                         "процессы, системы. Документ не про защиту — not_applicable."),
                        "criteria": dict(df.FACETS["protection_subject"]["values"])},
        },
        "нормативность": {
            "чемпион": {"type": "choice", "instructions": nf_champ,
                        "criteria": dict(df.FACETS["normative_force"]["values"])},
            "адаптив": {"type": "choice",
                        "instructions": ("Определи НОРМАТИВНУЮ СИЛУ: mandatory — текст устанавливает "
                                         "обязательные требования и нормы («должен», «обязан», «не "
                                         "допускается»); recommended — рекомендации и методические "
                                         "указания («рекомендуется», «может», «целесообразно»); "
                                         "informational — справка, обзор, статистика, отчёт, новость "
                                         "без предписаний."),
                        "criteria": dict(df.FACETS["normative_force"]["values"])},
        },
    }


def ask(key: str, text: str, questions: dict, timeout: int = 180) -> dict:
    body = json.dumps({"model": MODEL, "state": text[:4000], "questions": questions},
                      ensure_ascii=False).encode()
    req = urllib.request.Request(
        URL, data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default="eval/labels_docs_v0_labform.jsonl")
    ap.add_argument("--docs", default="eval/label_docs_input.json")
    ap.add_argument("--weak-below", type=float, default=0.6,
                    help="порог отбора: берём документы, где топ-вероятность по вопросу ниже порога")
    ap.add_argument("--repeats", type=int, default=2)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--sleep", type=float, default=1.0)
    ap.add_argument("--out", default="eval/jev_adaptive.jsonl")
    args = ap.parse_args()

    key = os.environ.get("POLZA_API_KEY") or os.environ.get("HERMES_CUSTOM_POLZA_API_KEY")
    if not key:
        print("нет ключа")
        return 2

    rows = [json.loads(l) for l in Path(args.labels).read_text(encoding="utf-8").splitlines() if l.strip()]
    best: dict[str, dict] = {}
    for r in rows:
        if r.get("ok"):
            best[r["document_id"]] = r
    pool = {d["document_id"]: d for d in json.loads(Path(args.docs).read_text(encoding="utf-8"))}

    def top_prob(row, q):
        pr = ((row.get("answers") or {}).get(q) or {}).get("probabilities") or {}
        return max(pr.values()) if isinstance(pr, dict) and pr else 0.0

    weak = [did for did, r in best.items()
            if any(top_prob(r, q) < args.weak_below for q in ("тема", "предмет_защиты", "нормативность"))]
    docs = [pool[d] for d in weak if d in pool]
    print(f"слабых документов к адаптивному прогону: {len(docs)} (порог {args.weak_below})")

    forms = formulations()
    tasks = [(d, qname) for d in docs for qname in forms]
    print(f"вопросов × документов: {len(tasks)}; формулировок на вопрос: 2; повторов: {args.repeats}; "
          f"вызовов: {len(tasks) * args.repeats}")

    out, cost, t0 = [], 0.0, time.time()

    def run(job):
        doc, qname = job
        results = []
        for which in ("чемпион", "адаптив"):
            qs = {qname: forms[qname][which]}
            for rep in range(1, args.repeats + 1):
                try:
                    if args.sleep:
                        time.sleep(args.sleep)
                    data = ask(key, str(doc.get("text") or ""), qs)
                    a = (data.get("answers") or {}).get(qname) or {}
                    results.append({"document_id": doc["document_id"], "question": qname,
                                    "formulation": which, "repeat": rep,
                                    "choice": a.get("choice"),
                                    "probabilities": a.get("probabilities"),
                                    "confidence": a.get("confidence"),
                                    "usage": data.get("usage") or {}, "ok": True})
                except urllib.error.HTTPError as e:
                    results.append({"document_id": doc["document_id"], "question": qname,
                                    "formulation": which, "repeat": rep, "ok": False,
                                    "error": f"HTTP {e.code}"})
                except Exception as e:  # noqa: BLE001
                    results.append({"document_id": doc["document_id"], "question": qname,
                                    "formulation": which, "repeat": rep, "ok": False,
                                    "error": f"{type(e).__name__}"})
        return results

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool_ex:
        for n, res in enumerate(pool_ex.map(run, tasks), 1):
            out.extend(res)
            cost += sum(float((r.get("usage") or {}).get("cost_rub") or 0) for r in res)
            if n % 20 == 0:
                print(f"  задач {n}/{len(tasks)} | расход {cost:.3f} ₽ | {time.time()-t0:.0f} с")

    Path(args.out).write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in out), encoding="utf-8")
    ok = [r for r in out if r.get("ok")]
    print(f"\nответов: {len(ok)} из {len(out)}; расход {cost:.3f} ₽; время {time.time()-t0:.0f} с")
    bad = [r for r in out if not r.get("ok")]
    if bad:
        print("ошибки:", Counter((r.get("error") or "")[:30] for r in bad).most_common(3))

    # ── Разбор: чемпион против адаптива по каждому вопросу ───────────────────
    print("\nчемпион против адаптива (средняя топ-вероятность, устойчивость, согласие формулировок):")
    for qname in forms:
        stats = {}
        verdicts = Counter()
        per_doc = defaultdict(dict)
        for r in ok:
            if r["question"] != qname:
                continue
            per_doc[r["document_id"]].setdefault(r["formulation"], []).append(
                (r["choice"], max((r.get("probabilities") or {}).values(), default=0.0)))
        for f in ("чемпион", "адаптив"):
            vals, stable, tot = [], 0, 0
            for cells in per_doc.values():
                c = cells.get(f) or []
                if not c:
                    continue
                tot += 1
                vals += [p for _, p in c]
                if len({ch for ch, _ in c}) == 1:
                    stable += 1
            stats[f] = (sum(vals) / len(vals) if vals else 0.0, stable, tot)
        for did, cells in per_doc.items():
            ch, ad = cells.get("чемпион") or [], cells.get("адаптив") or []
            if not ch or not ad:
                continue
            top_ch = max(p for _, p in ch)
            top_ad = max(p for _, p in ad)
            same = (ch[0][0] == ad[0][0])
            if same and top_ad >= 0.6:
                verdicts["решено (формулировки сошлись, адаптив уверен)"] += 1
            elif not same and top_ch >= 0.6:
                verdicts["спорно (чемпион уверен, адаптив назвал другое)"] += 1
            elif not same and top_ad >= 0.6:
                verdicts["решено адаптивом (чемпион был слаб)"] += 1
            else:
                verdicts["остаётся слабым (обе формулировки неуверенны)"] += 1
        ch_p, ch_st, ch_tot = stats["чемпион"]
        ad_p, ad_st, ad_tot = stats["адаптив"]
        print(f"\n  {qname}: документов {ch_tot}")
        print(f"    чемпион: средняя топ-вероятность {ch_p:.2f}, устойчиво {ch_st}/{ch_tot}")
        print(f"    адаптив: средняя топ-вероятность {ad_p:.2f}, устойчиво {ad_st}/{ad_tot}")
        for k, v in verdicts.most_common():
            print(f"    {k}: {v}")

    # ── Список к владельцу ──────────────────────────────────────────────────
    print("\nк ревью владельца (обе формулировки устойчивы, но расходятся или обе неуверенны):")
    per = defaultdict(dict)
    for r in ok:
        per[(r["document_id"], r["question"])].setdefault(r["formulation"], []).append(
            (r["choice"], max((r.get("probabilities") or {}).values(), default=0.0)))
    shown = 0
    for (did, qname), cells in per.items():
        ch, ad = cells.get("чемпион") or [], cells.get("адаптив") or []
        if not ch or not ad:
            continue
        if ch[0][0] == ad[0][0] and max(p for _, p in ad) >= 0.6:
            continue
        shown += 1
        if shown <= 20:
            title = str(pool.get(did, {}).get("title", ""))[:42]
            print(f"   {did[:8]} {qname:<15} чемпион {ch[0][0]} {max(p for _,p in ch):.2f} | "
                  f"адаптив {ad[0][0]} {max(p for _,p in ad):.2f}   {title}")
    print(f"   всего таких: {shown}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
