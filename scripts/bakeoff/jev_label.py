"""Разметка фрагментов по словарю v0 моделью JEV: виды, рубрики, фасеты.

Зачем. Словарь видов и связей — гипотеза, пока она не проверена на реальных документах. Разметка
даёт три вещи сразу: (1) какие значения вообще встречаются в корпусе, (2) где модель уверена, а где
нет (порог для человека), (3) материал для обучения локального «ученика» — сам JEV в закрытом контуре
неприменим, он облачный.

Что спрашиваем про каждый фрагмент (закрытые вопросы, ответ всегда из списка):
  * вид документа (choice по словарю);
  * рубрика (choice);
  * предмет защиты (choice) и нормативность (choice) — фасеты.

Результат: JSONL с ответами и вероятностями + сводка (распределение, доля уверенных ответов,
расход). Файл со словарём — scripts/bakeoff/ontology_v0.json.

Запуск (ключ только из окружения):
  export POLZA_API_KEY=...
  python scripts/bakeoff/jev_label.py --chunks /tmp/chunks.json --limit 100 --out eval/labels_v0.jsonl
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

URL = "https://polza.ai/api/v1/systemone"
MODEL = "typesafe/jev"
HERE = Path(__file__).resolve().parent


def load_vocab() -> dict:
    return json.loads((HERE / "ontology_v0.json").read_text(encoding="utf-8"))


def build_questions(vocab: dict) -> dict:
    """Вопросы по словарю. Критерии — человеческие описания: их читает модель."""
    return {
        "вид_документа": {"type": "choice", "instructions": "Что это за документ по виду?",
                          "criteria": vocab["kinds"]},
        "рубрика": {"type": "choice", "instructions": "К какой предметной области относится фрагмент?",
                    "criteria": vocab["rubrics"]},
        "предмет_защиты": {"type": "choice", "instructions": "Что защищается по смыслу (если фрагмент про защиту)?",
                           "criteria": vocab["facets"]["защита_предмет"]},
        "нормативность": {"type": "choice", "instructions": "Это обязательные требования, рекомендации или справка?",
                          "criteria": vocab["facets"]["нормативность"]},
    }


def ask(key: str, text: str, questions: dict, timeout: int = 120) -> dict:
    body = json.dumps({"model": MODEL, "state": text[:6000], "questions": questions}, ensure_ascii=False).encode()
    req = urllib.request.Request(URL, data=body,
                                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                                method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunks", required=True, help="JSON: [{\"text\": ..., \"chunk_id\": ..., \"document_id\": ...}]")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--out", default="eval/labels_v0.jsonl")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    key = os.environ.get("POLZA_API_KEY") or os.environ.get("HERMES_CUSTOM_POLZA_API_KEY")
    if not key:
        print("нет ключа: задайте POLZA_API_KEY")
        return 2

    vocab = load_vocab()
    questions = build_questions(vocab)
    chunks = json.loads(Path(args.chunks).read_text(encoding="utf-8"))[: args.limit]
    print(f"фрагментов к разметке: {len(chunks)}; вопросов на фрагмент: {len(questions)}")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    results, cost, t0 = [], 0.0, time.time()

    def one(chunk: dict):
        try:
            data = ask(key, str(chunk.get("text") or ""), questions)
            return {"chunk_id": chunk.get("chunk_id"), "document_id": chunk.get("document_id"),
                    "answers": data.get("answers") or {}, "usage": data.get("usage") or {}, "ok": True}
        except urllib.error.HTTPError as e:
            return {"chunk_id": chunk.get("chunk_id"), "ok": False,
                    "error": f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:120]}"}
        except Exception as e:  # noqa: BLE001
            return {"chunk_id": chunk.get("chunk_id"), "ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for i, res in enumerate(pool.map(one, chunks), 1):
            results.append(res)
            cost += float((res.get("usage") or {}).get("cost_rub") or 0)
            if i % 10 == 0 or i == len(chunks):
                print(f"  {i}/{len(chunks)} | расход {cost:.3f} ₽ | {time.time()-t0:.0f} с")

    with out_path.open("w", encoding="utf-8") as f:
        for res in results:
            f.write(json.dumps(res, ensure_ascii=False) + "\n")

    ok = [r for r in results if r.get("ok")]
    print(f"\nразмечено: {len(ok)} из {len(results)}; расход {cost:.3f} ₽; время {time.time()-t0:.0f} с")

    # сводка по каждому вопросу: распределение и уверенность
    for qname in questions:
        dist, conf, low = {}, [], 0
        for r in ok:
            a = (r.get("answers") or {}).get(qname) or {}
            choice = a.get("choice")
            if choice:
                dist[choice] = dist.get(choice, 0) + 1
            c = a.get("confidence")
            if isinstance(c, (int, float)):
                conf.append(c)
                if c < 0.6:
                    low += 1
        top = sorted(dist.items(), key=lambda x: -x[1])[:6]
        avg = sum(conf) / len(conf) if conf else 0
        sure = sum(1 for c in conf if c >= 0.9)
        print(f"\n{qname}: ответов {len(dist and [1]) and len(conf)}; средняя уверенность {avg:.2f}; "
              f"уверенных (≥0,9) {sure}; слабых (<0,6) {low}")
        for k, v in top:
            print(f"    {k:<34} {v}")
    print(f"\nрезультат сохранён: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
