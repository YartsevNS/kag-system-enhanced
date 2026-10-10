"""Пилот типизации связей через JEV с проверкой ДРУГИМ судьёй.

Задача (владелец, 10.10.2026): ограничиться пилотом и выдать результат. Проверяем главный рычаг
точности: наш граф размечает связи плохо (совпадение типа 41%, а 29% связей текстом не подтверждаются,
замер 60 фрагментов). Гипотеза: если тип связи выбирает JEV из НАШЕГО же списка, точность растёт.

Как проверяем честно: JEV и типизирует, и оценивать его тем же JEV нельзя (сам себя подтвердит).
Поэтому типаж от JEV сравнивается с ОТВЕТОМ ДРУГОЙ МОДЕЛИ (обычная чат-модель через Polza), которой
показывают тот же текст и ту же пару сущностей.

Что считаем:
  * совпадение типа с независимым судьёй: у наших связей и у типизации JEV;
  * сколько связей получают определённый тип там, где у нас мусорка («связано с»);
  * сколько связей независимый судья вообще не подтверждает (их надо не писать);
  * расход.

Ключ — только из окружения (POLZA_API_KEY / HERMES_CUSTOM_POLZA_API_KEY), в аргументах не передаём.

Запуск на ноутбуке:
    python scripts/bakeoff/jev_pilot_typing.py reports/_scratch/extraction_sample60.json --limit 40
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

SYSTEMONE = "https://polza.ai/api/v1/systemone"
CHAT = "https://polza.ai/api/v1/chat/completions"

# Типы связей спрашиваем строго из нашей онтологии (согласовано с src/indexing/graph_ontology.py).
TYPES = ["SIGNED_BY", "ISSUED_BY", "DATED", "AMOUNT", "LOCATED_AT", "SUPERSEDES", "AMENDS",
         "REFERENCES", "HAS_CLAUSE", "REQUIRES", "APPLIES_TO", "PART_OF", "DEFINES", "RELATED_TO"]
TYPE_LABEL = {
    "SIGNED_BY": "подписано", "ISSUED_BY": "издано", "DATED": "датировано", "AMOUNT": "на сумму",
    "LOCATED_AT": "расположено", "SUPERSEDES": "отменяет", "AMENDS": "изменяет",
    "REFERENCES": "ссылается на", "HAS_CLAUSE": "содержит пункт",
    "REQUIRES": "устанавливает требование", "APPLIES_TO": "распространяется на",
    "PART_OF": "входит в состав", "DEFINES": "определяет", "RELATED_TO": "связано с (тип не определён)",
}
WEAK = {"BELONGS_TO", "RELATED_TO", ""}


def post(url: str, body: dict, key: str, timeout: int = 180) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def jev_types(text: str, relations: list[dict], key: str) -> tuple[dict[int, str], float]:
    """Тип связи от JEV: закрытый выбор из нашего списка. Возвращает {индекс: код} и расход."""
    questions = {}
    for i, r in enumerate(relations):
        questions[f"связь_{i}"] = {
            "type": "choice",
            "instructions": (f"Отношение между «{r['от']}» и «{r['к']}» по тексту — какое из "
                             f"перечисленных точнее всего? Если ничего не подходит, выбери "
                             f"«связано с (тип не определён)»."),
            "criteria": {f"{code} ({TYPE_LABEL[code]})": "" for code in TYPES},
        }
    if not questions:
        return {}, 0.0
    data = post(SYSTEMONE, {"model": "typesafe/jev", "state": text[:6000], "questions": questions}, key)
    out = {}
    for name, ans in (data.get("answers") or {}).items():
        if not name.startswith("связь_"):
            continue
        idx = int(name.split("_")[1])
        choice = str(ans.get("choice") or "")
        code = choice.split(" ")[0].strip()
        out[idx] = code if code in TYPES else "RELATED_TO"
    return out, float((data.get("usage") or {}).get("cost_rub") or 0)


def chat_types(text: str, relations: list[dict], model: str, key: str) -> tuple[dict[int, str], float]:
    """Независимый судья: обычная чат-модель. Просим строгий JSON, чтобы ответ был разбираемым."""
    if not relations:
        return {}, 0.0
    listing = "\n".join(f"{i}. «{r['от']}» → «{r['к']}»" for i, r in enumerate(relations))
    prompt = (
        "Ниже текст документа и список пар сущностей.\n"
        "Для каждой пары определи по ТЕКСТУ тип отношения и выбери его строго из списка:\n"
        + ", ".join(TYPES) + "\n"
        "Если отношение из текста не следует — верни NONE.\n"
        "Ответь ТОЛЬКО JSON-массивом вида [{\"i\": 0, \"type\": \"APPLIES_TO\"}, ...].\n\n"
        f"ТЕКСТ:\n{text[:5000]}\n\nПАРЫ:\n{listing}"
    )
    data = post(CHAT, {"model": model, "temperature": 0,
                       "messages": [{"role": "user", "content": prompt}]}, key)
    content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    out: dict[int, str] = {}
    try:
        start = content.index("[")
        end = content.rindex("]") + 1
        for item in json.loads(content[start:end]):
            i = int(item.get("i", -1))
            code = str(item.get("type") or "").strip().upper()
            out[i] = code if code in TYPES or code == "NONE" else "RELATED_TO"
    except Exception:
        pass
    usage = data.get("usage") or {}
    return out, float(usage.get("cost_rub") or 0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sample", help="образец извлечения (dump_extraction_sample.py)")
    ap.add_argument("--limit", type=int, default=40, help="сколько фрагментов обработать")
    ap.add_argument("--chat-model", default="openai/gpt-5-nano", help="независимый судья")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    key = os.environ.get("POLZA_API_KEY") or os.environ.get("HERMES_CUSTOM_POLZA_API_KEY")
    if not key:
        print("нет ключа: задайте POLZA_API_KEY в окружении")
        return 2

    data = json.loads(Path(args.sample).read_text(encoding="utf-8"))
    items = (data.get("данные") or [])[: args.limit]

    stats = Counter()
    per_doc = []
    cost_jev = cost_chat = 0.0
    type_pairs = Counter()
    t0 = time.monotonic()

    for n, item in enumerate(items, 1):
        rels = item.get("связи") or []
        if not rels:
            continue
        text = item.get("текст") or ""
        try:
            j_types, c1 = jev_types(text, rels, key)
            cost_jev += c1
        except urllib.error.HTTPError as e:
            print(f"  JEV отказ {e.code} на фрагменте {n}")
            continue
        try:
            ch_types, c2 = chat_types(text, rels, args.chat_model, key)
            cost_chat += c2
        except Exception as exc:  # noqa: BLE001
            print(f"  независимый судья не ответил на фрагменте {n}: {type(exc).__name__}")
            ch_types = {}

        for i, r in enumerate(rels):
            ours = str(r.get("тип") or "").upper()
            jev = j_types.get(i, "")
            judge = ch_types.get(i, "")
            stats["связей"] += 1
            if ours in WEAK:
                stats["наших_слабых"] += 1
                if jev and jev != "RELATED_TO":
                    stats["jev_дал_тип"] += 1
            if judge == "NONE":
                stats["не_подтверждено_судьёй"] += 1
            elif judge:
                if ours == judge:
                    stats["совпало_наше"] += 1
                if jev == judge:
                    stats["совпало_jev"] += 1
                stats["сравнимо"] += 1
            if jev and judge and jev != judge:
                type_pairs[(jev, judge)] += 1
        per_doc.append({"документ": item.get("документ"), "связей": len(rels)})
        print(f"  {n:3d}/{len(items)} связей {len(rels):2d} | JEV-тип {sum(1 for i in rels and j_types)} "
              f"| судья {len(ch_types)}")

    print("\n" + "=" * 88)
    print("ПИЛОТ: ТИПИЗАЦИЯ СВЯЗЕЙ JEV И ПРОВЕРКА ДРУГИМ СУДЬЁЙ")
    print("=" * 88)
    n = stats["связей"] or 1
    cmp_n = stats["сравнимо"] or 1
    print(f"фрагментов: {len(per_doc)}; связей проверено: {stats['связей']}")
    print(f"  наших связей с неопределённым типом: {stats['наших_слабых']} "
          f"({100 * stats['наших_слабых'] / n:.0f}%)")
    print(f"  из них JEV дал ОПРЕДЕЛЁННЫЙ тип:    {stats['jev_дал_тип']} "
          f"({100 * stats['jev_дал_тип'] / max(stats['наших_слабых'], 1):.0f}% слабых)")
    print(f"  независимый судья НЕ подтвердил связь: {stats['не_подтверждено_судьёй']} "
          f"({100 * stats['не_подтверждено_судьёй'] / n:.0f}%)")
    print(f"  совпадение типа с судьёй: наше {stats['совпало_наше']} "
          f"({100 * stats['совпало_наше'] / cmp_n:.1f}%), JEV {stats['совпало_jev']} "
          f"({100 * stats['совпало_jev'] / cmp_n:.1f}%)")
    print(f"расход: JEV {round(cost_jev, 3)} руб., судья {round(cost_chat, 3)} руб., "
          f"всего {round(cost_jev + cost_chat, 3)} руб.; время {round(time.monotonic() - t0, 1)} с")
    if type_pairs:
        print("\nгде JEV и независимый судья разошлись (JEV → судья):")
        for (j, c), k in type_pairs.most_common(8):
            print(f"  {k:3d}×  {j:14s} → {c}")

    out = args.out or str(Path(args.sample).with_name("jev_pilot_typing_report.json"))
    Path(out).write_text(json.dumps({
        "связей": stats["связей"], "слабых_наших": stats["наших_слабых"],
        "jev_дал_тип": stats["jev_дал_тип"], "не_подтверждено": stats["не_подтверждено_судьёй"],
        "совпало_наше": stats["совпало_наше"], "совпало_jev": stats["совпало_jev"],
        "сравнимо": stats["сравнимо"], "расход_jev_руб": round(cost_jev, 3),
        "расход_судья_руб": round(cost_chat, 3), "расхождения": [f"{j}→{c}: {k}"
                                                               for (j, c), k in type_pairs.most_common()],
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nразбор сохранён: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
