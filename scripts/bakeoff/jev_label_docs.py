"""Разметка ДОКУМЕНТОВ моделью JEV: темы (рубрики) и фасеты — запуск на рабочей машине.

Почему на уровне документа, а не фрагмента: пилот разметки фрагментов (10.10.2026) показал, что вид
документа и тема на фрагменте определяются плохо (вид — 0,69), а предмет защиты и нормативность —
хорошо (0,83). Вид у нас уже проставлен; здесь заполняем ТЕМЫ и ФАСЕТЫ, спрашивая про документ целиком.

Критерии (закрытые списки) берутся из ЕДИНЫХ словарей проекта — document_topics и document_facets:
ответ приходит КОДОМ словаря, а не русским словом, поэтому его не надо угадывать и переводить.

Ключ только из окружения (POLZA_API_KEY или HERMES_CUSTOM_POLZA_API_KEY), в файлы не пишется.
Ответы сохраняются сырыми (JSONL): по ним видно уверенность и можно перепроверить вывод.

Запуск:
  python scripts/bakeoff/jev_label_docs.py --docs eval/label_docs_input.json --out eval/labels_docs_v0.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

URL = "https://polza.ai/api/v1/systemone"
MODEL = "typesafe/jev"


def build_questions() -> dict:
    """Вопросы с ЗАКРЫТЫМИ перечнями из словарей проекта (ключ ответа = код словаря).

    Формулировка темы подобрана ЛАБОРАТОРИЕЙ формулировок (`scripts/bakeoff/jev_prompt_lab.py`,
    5 документов × 3 повтора × 12 вариантов, 10.10.2026). Что показали замеры:

    * главный рычаг — ЯВНОЕ разведение «предметная область» и «нормативность»: без него модель
      тянет нормативные документы в юриспруденцию (устойчивость 3/5, уверенность 0,66; с ним —
      5/5 и 0,90);
    * критерии-ОПРЕДЕЛЕНИЯ лучше, чем одни названия (0,90 против 0,80 при той же формулировке), а
      «название + определение» хуже обоих по отдельности (0,71) — не смешивать;
    * оговорка «other только если ни одна тема не подходит» работает ТОЛЬКО в паре с разведением
      (сама по себе 0,68; вместе — 0,93 и больше уверенных ответов);
    * примеры и более длинный текст вклада не дают; спрашивать нормативность РЯДОМ не нужно —
      путаница лечится формулировкой, а не соседним вопросом;
    * контрольные документы (ГОСТ, Р) дают одинаковый ответ при любом варианте, то есть различия
      видны только на спорных документах — лаборатория меряет формулировку, а не шум.

    Оговорка о границах: лаборатория мерит УСТОЙЧИВОСТЬ и УВЕРЕННОСТЬ, а не правильность — на спорных
    документах эталона владельца нет. Правильность подтверждается только там, где есть внешняя опора.
    """
    from src.indexing import document_facets as df
    from src.indexing import document_topics as dt

    return {
        "тема": {
            "type": "choice",
            "instructions": ("Определи ПРЕДМЕТНУЮ ОБЛАСТЬ документа — о чём он по содержанию, а не каким "
                             "документом является. Нормативность документа (обязательные требования или "
                             "справка) к теме не относится. Значение other ставь только если ни одна "
                             "тема не подходит."),
            "criteria": {code: (meta.get("definition") or meta.get("title", code))
                         for code, meta in dt.RUBRICS.items()},
        },
        "предмет_защиты": {
            "type": "choice",
            "instructions": "Что защищается по смыслу документа? Если документ не про защиту — "
                            "выбери not_applicable.",
            "criteria": dict(df.FACETS["protection_subject"]["values"]),
        },
        "нормативность": {
            "type": "choice",
            "instructions": "Это обязательные требования, рекомендации или справочная информация?",
            "criteria": dict(df.FACETS["normative_force"]["values"]),
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
    ap.add_argument("--out", default="eval/labels_docs_v0.jsonl")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--sleep", type=float, default=0.0,
                    help="пауза между запросами (секунд) — при 429 увеличить")
    ap.add_argument("--resume", action="store_true",
                    help="не переделывать документы, по которым ответ уже есть в --out")
    ap.add_argument("--docs-ids", default="",
                    help="через запятую: размечать только эти документы (по началу id) — для переразметки слабых")
    args = ap.parse_args()

    key = os.environ.get("POLZA_API_KEY") or os.environ.get("HERMES_CUSTOM_POLZA_API_KEY")
    if not key:
        print("нет ключа: задайте POLZA_API_KEY в окружении")
        return 2

    questions = build_questions()
    docs = json.loads(Path(args.docs).read_text(encoding="utf-8"))
    out_path = Path(args.out)
    done, previous = set(), []
    if args.resume and out_path.exists():
        with out_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                previous.append(r)
                if r.get("ok"):
                    done.add(r.get("document_id"))
        docs = [d for d in docs if d.get("document_id") not in done]
        print(f"дозапись: уже размечено {len(done)}, к разметке осталось {len(docs)}")
    if args.limit:
        docs = docs[: args.limit]
    if args.docs_ids:
        want = [w.strip() for w in args.docs_ids.split(",") if w.strip()]
        docs = [d for d in docs if any(str(d.get("document_id", "")).startswith(w) for w in want)]
        print(f"ограничение по списку: {len(docs)} документов")
    print(f"документов к разметке: {len(docs)}; вопросов на документ: {len(questions)}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    results, cost, t0 = [], 0.0, time.time()

    def one(doc: dict):
        try:
            if args.sleep:
                time.sleep(args.sleep)
            data = ask(key, str(doc.get("text") or ""), questions)
            return {"document_id": doc["document_id"], "title": doc.get("title"),
                    "answers": data.get("answers") or {}, "usage": data.get("usage") or {}, "ok": True}
        except urllib.error.HTTPError as e:
            return {"document_id": doc["document_id"], "ok": False,
                    "error": f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:140]}"}
        except Exception as e:  # noqa: BLE001
            return {"document_id": doc["document_id"], "ok": False,
                    "error": f"{type(e).__name__}: {str(e)[:140]}"}

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for i, res in enumerate(pool.map(one, docs), 1):
            results.append(res)
            cost += float((res.get("usage") or {}).get("cost_rub") or 0)
            if i % 10 == 0 or i == len(docs):
                print(f"  {i}/{len(docs)} | расход {cost:.3f} ₽ | {time.time()-t0:.0f} с")

    # Пишем ВСЁ вместе: ранее размеченное (при --resume) плюс новое — файл остаётся одним срезом.
    results = previous + results
    with out_path.open("w", encoding="utf-8") as f:
        for res in results:
            f.write(json.dumps(res, ensure_ascii=False) + "\n")

    ok = [r for r in results if r.get("ok")]
    print(f"\nразмечено: {len(ok)} из {len(results)}; расход {cost:.3f} ₽; время {time.time()-t0:.0f} с")
    if results and not ok:
        print("первая ошибка:", (results[0] or {}).get("error"))
    for qname in questions:
        dist, conf, low = {}, [], 0
        for r in ok:
            a = (r.get("answers") or {}).get(qname) or {}
            if a.get("choice"):
                dist[a["choice"]] = dist.get(a["choice"], 0) + 1
            c = a.get("confidence")
            if isinstance(c, (int, float)):
                conf.append(c)
                if c < 0.6:
                    low += 1
        avg = sum(conf) / len(conf) if conf else 0
        sure = sum(1 for c in conf if c >= 0.9)
        print(f"\n{qname}: ответов {len(conf)}; средняя уверенность {avg:.2f}; "
              f"уверенных (≥0,9) {sure}; слабых (<0,6) {low}")
        for k, v in sorted(dist.items(), key=lambda x: -x[1])[:8]:
            print(f"    {k:<26} {v}")
    print(f"\nрезультат: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
