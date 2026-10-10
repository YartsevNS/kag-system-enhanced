"""Разметка на уровне ДОКУМЕНТА из уже собранных меток фрагментов + сверка с типами в базе.

Зачем. Пилот показал: вид документа на уровне фрагмента определяется плохо (средняя уверенность
0,69), потому что кусок текста вне контекста не отличить от публикации. Вид — свойство документа,
поэтому агрегируем ответы по всем его фрагментам и решаем один раз.

Сверка с базой обязательна: иначе мы измеряем согласие модели с самой собой. Типы документов в базе
проставлены другим классификатором — расхождение и есть та мера, по которой видно, чему верить.

Запуск:
  python scripts/bakeoff/doc_level_label.py --labels eval/labels_v0.jsonl --types types.tsv
где types.tsv — две колонки: id<TAB>document_type (выгружается с стенда).
"""
import argparse
import collections
import json
from pathlib import Path


def aggregate(labels_path: str) -> dict:
    """{document_id: {kind: голоса, conf: средняя уверенность, kinds: {вариант: взвешенный счёт}}}"""
    docs: dict = {}
    for line in Path(labels_path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if not rec.get("ok"):
            continue
        did = rec.get("document_id") or ""
        a = (rec.get("answers") or {}).get("вид_документа") or {}
        kind, conf = a.get("choice"), a.get("confidence")
        if not did or not kind:
            continue
        d = docs.setdefault(did, {"votes": collections.Counter(), "conf": [], "weighted": collections.Counter()})
        d["votes"][kind] += 1
        if isinstance(conf, (int, float)):
            d["conf"].append(float(conf))
            d["weighted"][kind] += float(conf)
    return docs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default="eval/labels_v0.jsonl")
    ap.add_argument("--types", default="", help="TSV: id<TAB>document_type (для сверки)")
    ap.add_argument("--min-chunks", type=int, default=2, help="сколько фрагментов документа нужно, чтобы решать")
    args = ap.parse_args()

    docs = aggregate(args.labels)
    print(f"документов в метках: {len(docs)}")

    db_types = {}
    if args.types and Path(args.types).exists():
        for line in Path(args.types).read_text(encoding="utf-8").splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                db_types[parts[0].strip()] = parts[1].strip()

    decided = agree = total_cmp = 0
    rows = []
    for did, d in docs.items():
        n = sum(d["votes"].values())
        if n < args.min_chunks:
            continue
        kind = d["weighted"].most_common(1)[0][0]
        avg_conf = sum(d["conf"]) / len(d["conf"]) if d["conf"] else 0.0
        # согласованность фрагментов: доля голосов за победивший вид
        consistency = d["votes"][kind] / n
        decided += 1
        db_kind = db_types.get(did, "(нет данных)")
        row = {"document_id": did, "chunks": n, "kind": kind, "confidence": round(avg_conf, 2),
               "consistency": round(consistency, 2), "db_kind": db_kind}
        rows.append(row)
        if db_kind != "(нет данных)":
            total_cmp += 1
            # грубое сопоставление: словари разные, поэтому сверяем по смысловым парам
            pairs = {("national_standard", "standard"), ("standardization_recommendation", "standard"),
                     ("law", "legal"), ("subordinate_act", "order"), ("subordinate_act", "legal"),
                     ("regulation", "policy"), ("methodology", "policy"),
                     ("contract", "contract"), ("invoice", "invoice"), ("act", "invoice"),
                     ("official_letter", "letter"), ("publication", "news"), ("analytics", "report"),
                     ("report", "report"), ("reference", "report")}
            if kind == db_kind or (kind, db_kind) in pairs:
                agree += 1

    rows.sort(key=lambda r: -r["chunks"])
    # ВАЖНО: у части документов в базе тип «other» или пустой — это НЕ ответ классификатора,
    # а его отказ (в корпусе таких 44%). Считать это расхождением с моделью нельзя: получится,
    # что модель «ошибается» там, где база просто молчит. Поэтому считаем две цифры.
    no_answer = {"other", "(пусто)", "", "unknown"}
    cmp_known = [r for r in rows if r["db_kind"] not in no_answer]
    agree_known = 0
    for r in cmp_known:
        kind, db_kind = r["kind"], r["db_kind"]
        pairs = {("national_standard", "standard"), ("standardization_recommendation", "standard"),
                 ("org_standard", "standard"), ("specification", "standard"),
                 ("law", "legal"), ("subordinate_act", "order"), ("subordinate_act", "legal"),
                 ("regulation", "policy"), ("methodology", "policy"), ("instruction", "policy"),
                 ("official_letter", "letter"), ("contract", "contract"), ("invoice", "invoice"),
                 ("act", "invoice"), ("report", "report"), ("analytics", "report"),
                 ("publication", "news"), ("reference", "report"), ("reference", "other")}
        if kind == db_kind or (kind, db_kind) in pairs:
            agree_known += 1
        r["agrees"] = (kind == db_kind or (kind, db_kind) in pairs)

    print(f"\nрешено (≥{args.min_chunks} фрагмента): {decided}; "
          f"сверено с базой: {total_cmp}; из них у базы БЫЛ ответ: {len(cmp_known)}")
    if cmp_known:
        print(f"согласие с классификатором базы (только там, где он ответил): "
              f"{agree_known}/{len(cmp_known)} = {agree_known/len(cmp_known):.0%}")
    if total_cmp:
        print(f"для справки, если считать «other» в базе за ответ: {agree}/{total_cmp} = {agree/total_cmp:.0%}")
    model_better = [r for r in rows if r["db_kind"] in no_answer and r["confidence"] >= 0.8
                    and r["consistency"] >= 0.67]
    print(f"документов, где база молчит («other»/пусто), а модель уверенно ответила: {len(model_better)}")
    print("\nпо документам (первые 15):")
    for r in rows[:15]:
        print(f"   {r['document_id'][:12]} фрагментов {r['chunks']:>2} | модель: {r['kind']:<30} "
              f"увер {r['confidence']:.2f} согласованность {r['consistency']:.2f} | в базе: {r['db_kind']}")

    out = Path(args.labels).with_name(Path(args.labels).stem + "_docs.json")
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nсводка по документам: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
