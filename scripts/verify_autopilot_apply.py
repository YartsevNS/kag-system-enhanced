"""Применить автопилот на стенде и проверить цикл Learn сквозным прогоном.

Шаги:
 1. сухой прогон правил по очереди словаря (что решается без человека);
 2. применение (dry_run=false) — пары подтверждаются, решение пишется в журнал;
 3. журнал авто-решений: что записано;
 4. Learn: непокрытый вопрос -> пробел -> решение человека «принять» ->
    повторный замер покрытия того же вопроса (должно вырасти);
 5. уборка тестовых записей, чтобы стенд не засорять.

Запускается внутри контейнера kag-api (там есть БД и зависимости).
"""
import asyncio
import json

res = {}

from src.api.services import answerability, automation  # noqa: E402

# ── 1. сухой прогон ─────────────────────────────────────────────────────────
res["dry_run"] = automation.run_alias_autopilot(2000, dry_run=True)

# ── 2. применение ───────────────────────────────────────────────────────────
res["applied"] = automation.run_alias_autopilot(2000, dry_run=False)

# ── 3. журнал ───────────────────────────────────────────────────────────────
res["decisions"] = automation.list_decisions(status="active", limit=3)

# ── 4. Learn: вопрос -> пробел -> принятие -> повторный замер ───────────────
QUESTION = "требования к биометрической аутентификации пользователей"

before = answerability.analyze(QUESTION, save=True)
res["learn_before"] = before

gap_id = None
try:
    from sqlalchemy import text

    from src.database.session import get_session_local

    db = get_session_local()()
    try:
        gap_id = db.execute(text(
            "SELECT id::text FROM ontology_schema_gaps WHERE term = ANY(:terms) "
            "ORDER BY hits DESC LIMIT 1"
        ), {"terms": [w.lower() for w in before.get("missing", [])]}).scalar()
    finally:
        db.close()

    if gap_id:
        from src.api.routes.ontology import GapReview, schema_gap_review

        class _U:
            email = "verifier@local"
            username = "verifier"
            is_admin = True

        out = asyncio.run(schema_gap_review(gap_id, GapReview(status="accepted", note=""), _U()))
        res["review"] = out
        answerability.vocabulary(force=True)
        res["learn_after"] = answerability.check(QUESTION)
finally:
    # уборка тестовых записей: принятый пробел и добавленная им пара
    from sqlalchemy import text

    from src.database.session import get_session_local

    db = get_session_local()()
    try:
        if gap_id:
            db.execute(text("DELETE FROM ontology_schema_gaps WHERE id = cast(:id as uuid)"),
                       {"id": gap_id})
        db.execute(text("DELETE FROM entity_aliases WHERE source = 'gap_accepted' "
                        "AND alias = ANY(:terms)"),
                   {"terms": [w.lower() for w in before.get("missing", [])]})
        db.commit()
    finally:
        db.close()
    answerability.vocabulary(force=True)
    res["cleanup"] = "тестовые записи удалены"

print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
