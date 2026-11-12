"""Проверка автопилота на стенде: реальные функции, реальные БД, без HTTP-авторизации.

Запускается внутри контейнера kag-api. Печатает только результаты, ничего не меняет
(кроме сухого прогона правил и явного решения по паре — см. ниже).
"""
import json
import sys

out = {}


def safe(name, fn):
    try:
        out[name] = fn()
    except Exception as e:  # noqa: BLE001
        out[name] = {"ERROR": f"{type(e).__name__}: {str(e)[:300]}"}


from src.api.services import answerability, automation, glossary  # noqa: E402

safe("vocab_size", lambda: len(answerability.vocabulary(force=True)))

questions = [
    "какие меры защиты применяет организация",
    "требования к защите персональных данных",
    "какие требования к средствам криптографической защиты информации",
    "сколько потратили на рекламу флешек",
    "какая погода в Антарктиде",
]
safe("coverage", lambda: [
    {"q": q, **{k: v for k, v in answerability.check(q).items() if k in
                ("coverage", "verdict", "missing")}}
    for q in questions
])

safe("glossary", lambda: {"stats": glossary.stats(), "block_head": glossary.build()[:400]})
safe("alias_dry_run", lambda: automation.run_alias_autopilot(2000, dry_run=True))
safe("decisions_stats", lambda: automation.stats())
safe("schema_draft", lambda: {"candidates": len(automation.schema_draft(20)["candidates"]),
                              "tql_head": automation.schema_draft(20)["tql"][:400]})
print(json.dumps(out, ensure_ascii=False, indent=1))
