"""Автопилот онтологии: правила, при которых решение принимается без человека.

Зачем: ручные очереди (словарь, дубли, схема) — это узкое место продукта, но
«полностью без человека» в регулируемом домене нельзя: каждое авто-решение
должно быть записано, объяснено правилом и отзываемо.

Три уровня доверия (по образцу NVIDIA auto-ontology: generate → certify):
  1) детерминированное правило (одинаковые буквы, выводимая аббревиатура,
     реквизиты) — решение принимается, человеку не выносится;
  2) модель + цитата + порог покрытия — решение принимается, но попадает
     в журнал авто-решений с кнопкой «отозвать» (выборочный контроль);
  3) всё остальное — человеку, пачками по исключениям.

Модуль не меняет схему TypeDB и не пишет в рабочую онтологию мимо flow:
он распоряжается только очередями проверок (entity_aliases, duplicate_candidates,
ontology_schema_gaps). Каждое решение пишется в ontology_auto_decisions —
это и есть доказательство «что система решила сама».
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

_ALNUM = re.compile(r"[^0-9a-zа-яё]+")


# ── нормализация и правила ───────────────────────────────────────────────────
def norm(value: str) -> str:
    """Сравнимый вид строки: нижний регистр, ё→е, без знаков и лишних пробелов."""
    v = (value or "").lower().replace("ё", "е")
    v = _ALNUM.sub(" ", v)
    return " ".join(v.split())


def letters(value: str) -> str:
    """Только буквы и цифры — для правила «одинаковые буквы»."""
    return _ALNUM.sub("", (value or "").lower().replace("ё", "е"))


def initials_match(short: str, full: str) -> bool:
    """Короткое имя выводится из полного по первым буквам слов.

    «ФСБ» ← «Федеральная служба безопасности» (ФСБ), «Банк России» ← «БР»?
    Нет: правило одностороннее и строгое — аббревиатура собирается из ПОЛНОГО
    названия, и её надо сравнить с коротким. Служебные слова («и», «по», «в»)
    в аббревиатуру не входят: иначе из «Федеральная служба по техническому и
    экспортному контролю» получилось бы ФСПТИЭК вместо ФСТЭК.
    """
    words = [w for w in norm(full).split() if w not in _SERVICE_WORDS]
    if len(words) < 2:
        return False
    letters_only = letters(short)
    if len(letters_only) < 2:
        return False
    # первый вариант: только первые буквы
    firsts = "".join(w[0] for w in words)
    if letters_only in (firsts, firsts[: len(letters_only)]):
        return True
    # второй вариант: аббревиатура собрана из первых букв значимых слов,
    # где часть слов пропущена (ФСТЭК: Федеральная служба по техническому
    # и экспортному контролю → Ф-С-Т-Э-К) — проверяем подпоследовательность.
    it = iter(firsts)
    return all(ch in it for ch in letters_only)


_SERVICE_WORDS = {
    "и", "в", "во", "на", "по", "с", "со", "для", "при", "от", "о", "об",
    "the", "of", "and",
}

# Реквизиты организации: если в названии есть ИНН/ОГРН — это уже полноценная
# запись, а не вариант написания, подтверждать её человеком незачем.
_REQUISITES = re.compile(r"\b(инн|огрн|кпп)\b\s*[№:]?\s*\d{9,}", re.IGNORECASE)


def alias_decision(alias: str, canonical: str, entity_type: str = "") -> Tuple[str, str, float]:
    """Решение по паре словаря. Возвращает (auto|review, правило, уверенность).

    Правила только те, у которых есть текстовое объяснение: если решение
    нельзя объяснить одной строкой, человек должен его увидеть сам.
    """
    a, c = (alias or "").strip(), (canonical or "").strip()
    if not a:
        return "review", "", 0.0
    # 1. вариант и канон совпадают по буквам (регистр, пробелы, знаки, ё/е)
    if c and letters(a) == letters(c):
        return "auto", "same_letters", 0.99
    # 2. канона нет — это реестровая запись, а не синоним
    if not c and len(letters(a)) >= 3:
        return "auto", "registry_entry", 0.9
    # 3. реквизиты в названии
    if _REQUISITES.search(a) or _REQUISITES.search(c):
        return "auto", "requisites", 0.95
    # 4. короткое имя выводится из полного по первым буквам
    if c:
        short, full = (a, c) if len(letters(a)) < len(letters(c)) else (c, a)
        if initials_match(short, full):
            return "auto", "abbreviation_from_full", 0.92
    return "review", "", 0.0


# ── журнал авто-решений ─────────────────────────────────────────────────────
_DDL = [
    """
    CREATE TABLE IF NOT EXISTS ontology_auto_decisions (
        id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        kind        text NOT NULL,              -- alias | duplicate | proposal | gap
        subject     text NOT NULL DEFAULT '',   -- id записи, по которой решение
        decision    text NOT NULL,              -- approved | rejected | new_version | distinct | ...
        rule        text NOT NULL DEFAULT '',   -- правило, которым решено
        confidence  numeric NOT NULL DEFAULT 0,
        payload     jsonb NOT NULL DEFAULT '{}'::jsonb,
        status      text NOT NULL DEFAULT 'active',   -- active | revoked
        created_by  text NOT NULL DEFAULT 'autopilot',
        created_at  timestamptz NOT NULL DEFAULT now(),
        revoked_by  text,
        revoked_at  timestamptz,
        note        text NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS ontology_auto_decisions_idx "
    "ON ontology_auto_decisions (kind, status, created_at DESC)",
]


def _engine():
    from src.database.session import get_session_local

    return get_session_local()


def ensure_table() -> None:
    from sqlalchemy import text

    db = _engine()()
    try:
        for stmt in _DDL:
            db.execute(text(stmt))
        db.commit()
    finally:
        db.close()


def record_decision(kind: str, subject: str, decision: str, rule: str,
                    confidence: float = 0.0, payload: Optional[Dict[str, Any]] = None,
                    created_by: str = "autopilot", note: str = "") -> str:
    from sqlalchemy import text

    ensure_table()
    db = _engine()()
    try:
        row = db.execute(
            text("""INSERT INTO ontology_auto_decisions
                    (kind, subject, decision, rule, confidence, payload, created_by, note)
                    VALUES (:k, :s, :d, :r, :c, cast(:p as jsonb), :by, :n)
                    RETURNING id::text"""),
            {"k": kind, "s": subject, "d": decision, "r": rule, "c": float(confidence),
             "p": json.dumps(payload or {}, ensure_ascii=False), "by": created_by, "n": note},
        ).scalar()
        db.commit()
        return row or ""
    finally:
        db.close()


def list_decisions(kind: str = "", status: str = "active", limit: int = 200) -> List[Dict[str, Any]]:
    from sqlalchemy import text

    ensure_table()
    where, params = [], {}
    if kind:
        where.append("kind = :k")
        params["k"] = kind
    if status:
        where.append("status = :st")
        params["st"] = status
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    db = _engine()()
    try:
        rows = db.execute(
            text("SELECT id::text, kind, subject, decision, rule, confidence, payload, status, "
                 "created_by, created_at, revoked_by, revoked_at, note "
                 f"FROM ontology_auto_decisions{clause} "
                 "ORDER BY created_at DESC LIMIT :lim"),
            {**params, "lim": max(1, min(int(limit or 200), 1000))},
        ).mappings().all()
    finally:
        db.close()
    out = []
    for r in rows:
        it = dict(r)
        for k in ("created_at", "revoked_at"):
            it[k] = it[k].isoformat() if it.get(k) else None
        out.append(it)
    return out


def stats() -> Dict[str, Any]:
    from sqlalchemy import text

    ensure_table()
    db = _engine()()
    try:
        by = db.execute(text("SELECT kind, status, count(*) FROM ontology_auto_decisions "
                             "GROUP BY 1, 2 ORDER BY 1")).all()
    finally:
        db.close()
    return {"rows": [{"kind": k, "status": s, "n": int(n)} for k, s, n in by]}


def _counts(db, sql: str, params: Optional[Dict[str, Any]] = None) -> int:
    from sqlalchemy import text

    return int(db.execute(text(sql), params or {}).scalar() or 0)


def advice(alias: str, canonical: str, entity_type: str = "") -> Dict[str, str]:
    """Подсказка человеку по паре: что сделал бы автопилот и на что смотреть.

    Это не решение, а сортировка: «похоже, расшифровка не выводится» ставит
    мусорные пары из чужих словарей вперёд, чтобы человек отклонял их пачкой,
    а не вычитывал список подряд.
    """
    verdict, rule, _conf = alias_decision(alias, canonical, entity_type)
    if verdict == "auto":
        return {"verdict": "auto", "rule": rule, "hint": rule}
    a, c = letters(alias), letters(canonical)
    looks_abbr = 2 <= len(a) <= 8 and len(c) > len(a) and len(norm(canonical).split()) >= 2
    if looks_abbr:
        return {"verdict": "review", "rule": "", "hint": "abbreviation_mismatch"}
    return {"verdict": "review", "rule": "", "hint": ""}


def run_alias_autopilot(limit: int = 2000, dry_run: bool = False) -> Dict[str, Any]:
    """Разобрать очередь словаря правилами. Остаток остаётся человеку.

    Правило «одинаковые буквы» уже применялось при извлечении (scripts/validate_terms.py);
    здесь добираются те пары, что пришли позже и не проходили через скрипт, плюс
    две новые детерминированные проверки: реквизиты и выводимая аббревиатура.
    """
    from sqlalchemy import text

    from src.database.entity_alias_models import EntityAlias

    db = _engine()()
    decided = {"auto": 0, "review": 0, "by_rule": {}}
    try:
        rows = (db.query(EntityAlias)
                  .filter((EntityAlias.verdict.is_(None)) | (EntityAlias.verdict == ""))
                  .order_by(EntityAlias.entity_type, EntityAlias.alias)
                  .limit(max(1, int(limit))).all())
        for row in rows:
            verdict, rule, conf = alias_decision(row.alias, row.canonical_name or "", row.entity_type or "")
            if verdict != "auto":
                decided["review"] += 1
                continue
            decided["auto"] += 1
            decided["by_rule"][rule] = decided["by_rule"].get(rule, 0) + 1
            if dry_run:
                continue
            row.verdict = "approved"
            row.reviewed = True
            row.source = f"autopilot:{rule}"
            db.add(row)
        if not dry_run and decided["auto"]:
            db.commit()
        pending = _counts(db, "SELECT count(*) FROM entity_aliases "
                              "WHERE verdict IS NULL OR verdict = ''")
    finally:
        db.close()

    if not dry_run and decided["auto"]:
        # журнал пишем пачкой одной записью-сводкой: построчные записи по сотням
        # пар утопили бы журнал и не дали бы ничего сверх правила.
        record_decision("alias", f"batch:{decided['auto']}", "approved", "batch_rules",
                        confidence=0.95, payload=decided,
                        note="авто-подтверждение пар словаря детерминированными правилами")
    decided["pending_after"] = pending
    decided["dry_run"] = dry_run
    return decided


def run_duplicate_autopilot(limit: int = 200, dry_run: bool = True) -> Dict[str, Any]:
    """Дубли: авто-решение только там, где совпало всё, что вообще может совпасть.

    Условие слияния: один и тот же ключ (издатель+вид+номер) И одинаковое название
    И score ≥ 0.95. Всё остальное — человеку. По умолчанию dry_run: слияние
    меняет документы, поэтому сначала показываем, что было бы.
    """
    from src.api.services import duplicates as dup_service
    from src.database.duplicate_models import DuplicateCandidate

    db = _engine()()
    out = {"auto": 0, "review": 0, "items": []}
    try:
        rows = (db.query(DuplicateCandidate)
                  .filter(DuplicateCandidate.status == "pending")
                  .order_by(DuplicateCandidate.created_at)
                  .limit(max(1, int(limit))).all())
        for row in rows:
            details = dup_service._json(row.details)
            same_key = (details.get("key") or "") != ""
            title_new = norm(details.get("new_title") or "")
            title_old = norm(details.get("existing_title") or "")
            same_title = bool(title_new) and title_new == title_old
            score = float(row.score or 0.0)
            if same_key and same_title and score >= 0.95:
                out["auto"] += 1
                out["items"].append({"id": row.id, "rule": "same_key_same_title",
                                     "score": score})
                if not dry_run:
                    res = dup_service.decide(db, row.id, "new_version", user="autopilot")
                    record_decision("duplicate", row.id, "new_version", "same_key_same_title",
                                    confidence=score, payload={"key": details.get("key"),
                                                               "result": res.get("status")})
            else:
                out["review"] += 1
    finally:
        db.close()
    out["dry_run"] = dry_run
    return out


def revoke(decision_id: str, who: str = "", note: str = "") -> Dict[str, Any]:
    """Отозвать авто-решение: вернуть запись в очередь и закрыть решение.

    Отзыв — часть контракта автоматизации, а не украшение: без него «система
    решила сама» превращается в «никто не отвечает».
    """
    from sqlalchemy import text

    from src.database.entity_alias_models import EntityAlias

    ensure_table()
    db = _engine()()
    try:
        row = db.execute(
            text("SELECT id::text, kind, subject, decision, status FROM ontology_auto_decisions "
                 "WHERE id = cast(:id as uuid)"), {"id": decision_id},
        ).mappings().first()
        if not row:
            return {"ok": False, "error": "решение не найдено"}
        if row["status"] == "revoked":
            return {"ok": False, "error": "решение уже отозвано"}

        restored = 0
        if row["kind"] == "alias":
            subject = row["subject"] or ""
            if subject.startswith("batch:"):
                # пачечная запись: возвращаем ВСЕ пары, подтверждённые автопилотом
                n = db.execute(text("UPDATE entity_aliases SET verdict = '', reviewed = false, "
                                    "source = 'autopilot:revoked' "
                                    "WHERE source LIKE 'autopilot:%' AND verdict = 'approved'")).rowcount
                restored = int(n or 0)
            else:
                n = db.execute(text("UPDATE entity_aliases SET verdict = '', reviewed = false, "
                                    "source = 'autopilot:revoked' WHERE id = :id"),
                               {"id": subject}).rowcount
                restored = int(n or 0)
        elif row["kind"] == "duplicate":
            n = db.execute(text("UPDATE duplicate_candidates SET status = 'pending', "
                                "decided_by = NULL, decided_at = NULL WHERE id = :id"),
                           {"id": row["subject"]}).rowcount
            restored = int(n or 0)
        elif row["kind"] == "gap":
            n = db.execute(text("UPDATE ontology_schema_gaps SET status = 'proposed', "
                                "reviewer = NULL, reviewed_at = NULL WHERE id = cast(:id as uuid)"),
                           {"id": row["subject"]}).rowcount
            restored = int(n or 0)

        db.execute(
            text("UPDATE ontology_auto_decisions SET status = 'revoked', revoked_by = :who, "
                 "revoked_at = now(), note = :note WHERE id = cast(:id as uuid)"),
            {"who": who or "", "note": (note or "")[:1000], "id": decision_id},
        )
        db.commit()
    finally:
        db.close()
    logger.info("автопилот: решение %s отозвано (%s объектов)", decision_id, restored)
    return {"ok": True, "restored": restored}


# ── P5: черновик расширения схемы из пробелов ───────────────────────────────
def schema_draft(limit: int = 60) -> Dict[str, Any]:
    """Черновик TypeQL из очереди пробелов: человек правит, а не сочиняет.

    Группируем не покрытые онтологией термины по общему корню и отдаём
    готовый каркас `define` с комментариями, частотой и примерами. Это тот же
    приём, что Generate в auto-ontology: сначала черновик из источника,
    потом сертификация человеком.
    """
    from sqlalchemy import text

    from src.indexing.graph_ontology import ENTITY_TYPES

    db = _engine()()
    try:
        rows = db.execute(
            text("SELECT term, hits, examples, status FROM ontology_schema_gaps "
                 "ORDER BY hits DESC, term LIMIT :lim"),
            {"lim": max(1, min(int(limit), 500))},
        ).mappings().all()
    finally:
        db.close()

    existing = {norm(t).replace(" ", "-") for t in ENTITY_TYPES}
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        term = (r["term"] or "").strip()
        if not term:
            continue
        key = norm(term).split()[0][:6] if norm(term) else ""
        groups.setdefault(key or "_", []).append(
            {"term": term, "hits": int(r["hits"] or 0),
             "examples": list(r["examples"] or [])[:3], "status": r["status"]}
        )

    lines = [
        "# ЧЕРНОВИК расширения онтологии (сгенерирован автопилотом)",
        "# Источник: вопросы пользователей, не покрытые онтологией (ontology_schema_gaps).",
        "# Порядок работы: убрать лишнее, переименовать под домен, применить через /ontology.",
        "",
        "define",
    ]
    candidates = []
    for key, items in sorted(groups.items(), key=lambda kv: -sum(i["hits"] for i in kv[1])):
        top = items[0]
        slug = re.sub(r"[^a-zа-я0-9]+", "-", norm(top["term"])).strip("-")[:40] or key
        if slug in existing:
            continue
        candidates.append({"slug": slug, "terms": items})
        lines.append(f"# {slug} — {sum(i['hits'] for i in items)} упоминаний, "
                     f"примеры: {', '.join(i['term'] for i in items[:3])}")
        lines.append(f"entity {slug}")
        lines.append("    owns ent-key @key,")
        lines.append(f"    owns name @card(0..1);")
        lines.append("")
    return {"ok": True, "candidates": candidates, "tql": "\n".join(lines)}
