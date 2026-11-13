"""Проверка пакетного разбора словаря на стенде (без изменения данных).

Вызывает настоящие маршруты как функции с подставным администратором:
- /terms/stats → разбивка очереди по подсказке (сколько мусора, сколько решается правилом);
- /terms/review-bulk с несуществующим id → должно вернуть changed=0 (данные не меняются);
- /terms?hint=abbreviation_mismatch → список пар, которые машина считает мусором.
"""
import asyncio
import json

from src.api.routes.terms import BulkReviewBody, terms_list, terms_review_bulk, terms_stats


class U:
    id = "verifier"
    username = "verifier"
    email = "verifier@local"
    is_admin = True
    groups = []


res = {}
res["stats"] = asyncio.run(terms_stats(U()))
res["bulk_noop"] = asyncio.run(terms_review_bulk(
    BulkReviewBody(ids=["00000000-0000-0000-0000-000000000000"], verdict="rejected"), U()))

junk = asyncio.run(terms_list(status="pending", hint="abbreviation_mismatch", limit=2000))
res["mismatch_total"] = junk.get("total")
res["mismatch_sample"] = [(it["alias"], it["canonical_name"]) for it in junk.get("items", [])[:5]]

auto = asyncio.run(terms_list(status="pending", hint="abbreviation_from_full", limit=2000))
res["auto_total"] = auto.get("total")

print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
