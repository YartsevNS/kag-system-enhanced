"""Запись разметки тем и фасетов в базу (запуск В КОНТЕЙНЕРЕ api).

Правило: пишем ТОЛЬКО там, где модель уверена (≥ порога), и всегда через админский эндпоинт правки
разметки — он обновляет запись и payload и оставляет след в журнале действий (кто/что/зачем).
Ответы с уверенностью ниже порога не выбрасываются: они попадают в отчёт «на ревью», чтобы решение
принимал человек, а не порог.

Запуск:
  docker exec kag-api python /app/data/apply_rubric_labels.py --labels /app/data/labels_docs_v0.jsonl
  (по умолчанию сухой прогон; для записи — --apply)
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.request

API = os.environ.get("KAG_API", "http://127.0.0.1:8000/api/v1")
REPORT = os.environ.get("REPORT", "/app/data/labels_docs_v0_report.json")


def login() -> str:
    req = urllib.request.Request(
        API + "/auth/login",
        data=json.dumps({"username": os.environ.get("ADMIN_USERNAME", "admin"),
                         "password": os.environ["ADMIN_PASSWORD"]}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    return urllib.request.urlopen(req, timeout=30).headers.get("Set-Cookie", "").split(";")[0]


def post(path: str, body: dict, ck: str) -> dict:
    req = urllib.request.Request(
        API + path, data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"Cookie": ck, "Content-Type": "application/json"}, method="POST")
    return json.load(urllib.request.urlopen(req, timeout=60))


def main() -> int:
    from src.api.services.document_repository import get_doc_repo
    from src.indexing import document_facets as df
    from src.indexing import document_topics as dt

    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default="/app/data/labels_docs_v0.jsonl")
    ap.add_argument("--min-confidence", type=float, default=0.6,
                    help="порог для ОСНОВНОЙ темы (argmax вероятностей)")
    ap.add_argument("--extra-prob", type=float, default=0.4,
                    help="порог для ДОПОЛНИТЕЛЬНЫХ тем: тема многозначная, берём все выше порога")
    ap.add_argument("--review-band", type=float, default=0.3,
                    help="нижняя граница «на ревью»: ниже — не трогаем")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    rows = []
    with open(args.labels, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    # В файле могут быть дубли: дозапись (--resume) дописывает повторные попытки, а неудачные строки
    # остаются. Оставляем по документу ОДНУ удачную строку — иначе один и тот же документ попадёт
    # в запись дважды и в отчёте будет двойной счёт.
    by_doc = {}
    for r in rows:
        did = r.get("document_id")
        if not did:
            continue
        if r.get("ok") or did not in by_doc:
            by_doc[did] = r if r.get("ok") else by_doc.get(did, r)
    rows = [r for r in by_doc.values() if r.get("ok")]
    print(f"строк после свёртки дублей: {len(rows)}")

    docs = get_doc_repo().get_all()
    to_write, review, skipped, unchanged = [], [], 0, 0
    for r in rows:
        if not r.get("ok"):
            continue
        did = r.get("document_id")
        d = docs.get(did)
        if not d:
            skipped += 1
            continue
        a = r.get("answers") or {}

        def pick(qname):
            aq = a.get(qname) or {}
            choice, conf = aq.get("choice"), aq.get("confidence")
            if not choice or not isinstance(conf, (int, float)):
                return None, 0.0
            return str(choice), float(conf)

        def profile(qname) -> dict:
            """Профиль вероятностей по критериям: JEV отдаёт его В ТОМ ЖЕ ответе choice.

            Благодаря этому тему можно брать МНОГОЗНАЧНОЙ без дополнительных вызовов: темы с
            вероятностью выше порога — в список, основная (argmax) — как главная.
            """
            aq = a.get(qname) or {}
            probs = aq.get("probabilities")
            if not isinstance(probs, dict):
                return {}
            out = {}
            for k, v in probs.items():
                try:
                    out[str(k)] = float(v)
                except (TypeError, ValueError):
                    continue
            return out

        rubrics, facets, notes = [], {}, []
        tema_probs = profile("тема")
        if tema_probs:
            # Основная тема — самая вероятная; дополнительные — все, что выше порога (многозначность).
            ranked = sorted(tema_probs.items(), key=lambda kv: -kv[1])
            main_code, main_p = ranked[0]
            second_p = ranked[1][1] if len(ranked) > 1 else 0.0
            # БЛИЗКАЯ ПАРА — это и есть мультитема: если две темы обе выше порога дополнительных и
            # отрыв маленький, документ действительно про обе (пример из замера: статистика ЦБ —
            # «банковское регулирование» 0,59 и «экономика» 0,41). Требовать от основной 0,6 в таком
            # случае неверно: получалось «на ревью» там, где модель уверенно назвала ДВЕ темы.
            near_tie = (second_p >= args.extra_prob and (main_p - second_p) <= 0.2)
            if dt.is_valid(main_code) and (main_p >= args.min_confidence or near_tie):
                rubrics = [main_code]
                extras = [c for c, p in ranked
                          if c != main_code and dt.is_valid(c) and p >= args.extra_prob]
                rubrics += extras[:2]      # не больше трёх тем на документ
                notes.append(f"тема {main_code} ({main_p:.2f})" +
                             (f"; ещё {', '.join(f'{c} {tema_probs[c]:.2f}' for c in extras[:2])}"
                              if extras else ""))
            elif main_p >= args.review_band:
                review.append({"document_id": did, "field": "rubrics", "value": main_code,
                               "confidence": main_p, "title": r.get("title")})
        else:
            r_code, r_conf = pick("тема")
            if r_code and dt.is_valid(r_code):
                if r_conf >= args.min_confidence:
                    rubrics = [r_code]
                    notes.append(f"тема {r_code} ({r_conf:.2f})")
                else:
                    review.append({"document_id": did, "field": "rubrics", "value": r_code,
                                   "confidence": r_conf, "title": r.get("title")})

        ps_probs = profile("предмет_защиты")
        if ps_probs:
            vals = [c for c, p in sorted(ps_probs.items(), key=lambda kv: -kv[1])
                    if df.is_valid_value("protection_subject", c) and p >= args.extra_prob]
            if vals:
                facets["protection_subject"] = vals[:3]
                notes.append("предмет защиты " + ", ".join(f"{v} {ps_probs[v]:.2f}" for v in vals[:3]))
            elif max(ps_probs.values() or [0]) >= args.review_band:
                top = max(ps_probs.items(), key=lambda kv: kv[1])
                review.append({"document_id": did, "field": "protection_subject", "value": top[0],
                               "confidence": top[1], "title": r.get("title")})
        else:
            ps, ps_conf = pick("предмет_защиты")
            if ps and df.is_valid_value("protection_subject", ps) and ps_conf >= args.min_confidence:
                facets["protection_subject"] = [ps]
                notes.append(f"предмет защиты {ps} ({ps_conf:.2f})")
            elif ps:
                review.append({"document_id": did, "field": "protection_subject", "value": ps,
                               "confidence": ps_conf, "title": r.get("title")})

        nf_probs = profile("нормативность")
        if nf_probs:
            top_v, top_p = max(nf_probs.items(), key=lambda kv: kv[1])
            if df.is_valid_value("normative_force", top_v) and top_p >= args.min_confidence:
                facets["normative_force"] = [top_v]
                notes.append(f"нормативность {top_v} ({top_p:.2f})")
            elif top_p >= args.review_band:
                review.append({"document_id": did, "field": "normative_force", "value": top_v,
                               "confidence": top_p, "title": r.get("title")})
        else:
            nf, nf_conf = pick("нормативность")
            if nf and df.is_valid_value("normative_force", nf) and nf_conf >= args.min_confidence:
                facets["normative_force"] = [nf]
                notes.append(f"нормативность {nf} ({nf_conf:.2f})")
            elif nf:
                review.append({"document_id": did, "field": "normative_force", "value": nf,
                               "confidence": nf_conf, "title": r.get("title")})

        if not rubrics and not facets:
            continue
        # Идемпотентность: если в базе уже ровно то, что мы собираемся записать, — не трогаем
        # документ (иначе повторный прогон плодит одинаковые записи в журнале действий).
        cur_rubrics = dt.normalize(d.get("rubrics"))
        cur_facets = d.get("facets")
        if isinstance(cur_facets, str):
            try:
                cur_facets = json.loads(cur_facets) if cur_facets else {}
            except Exception:  # noqa: BLE001
                cur_facets = {}
        cur_facets = df.normalize(cur_facets)
        want_facets = df.normalize(facets)
        if rubrics and rubrics == cur_rubrics and (not want_facets or want_facets == cur_facets):
            unchanged += 1
            continue
        to_write.append({"document_id": did, "rubrics": rubrics, "facets": facets,
                         "note": "разметка моделью (JEV, уровень документа): " + "; ".join(notes),
                         "before": {"rubrics": d.get("rubrics"), "facets": d.get("facets")}})

    print(f"строк разметки: {len(rows)}; к записи: {len(to_write)}; без изменений: {unchanged}; "
          f"на ревью: {len(review)}; нет в реестре: {skipped}")
    for w in to_write[:5]:
        print(f"   {w['document_id'][:12]} {w['note'][:90]}")
    with open(REPORT, "w", encoding="utf-8") as f:
        json.dump({"to_write": to_write, "review": review}, f, ensure_ascii=False, indent=2)
    print(f"отчёт: {REPORT}")

    if not args.apply:
        print("СУХОЙ ПРОГОН: ничего не записано (для применения — флаг --apply)")
        return 0

    ck = login()
    ok = fail = 0
    for w in to_write:
        body = {"document_id": w["document_id"], "note": w["note"]}
        if w["rubrics"]:
            body["rubrics"] = w["rubrics"]
        if w["facets"]:
            body["facets"] = w["facets"]
        try:
            res = post("/admin/models/document-classification", body, ck)
            if res.get("status") == "ok":
                ok += 1
            else:
                fail += 1
                print(f"   отказ {w['document_id'][:12]}: {str(res.get('message'))[:90]}")
        except Exception as e:  # noqa: BLE001
            fail += 1
            print(f"   ошибка {w['document_id'][:12]}: {type(e).__name__}: {str(e)[:90]}")
    print(f"\nзаписано: {ok}, ошибок: {fail}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
