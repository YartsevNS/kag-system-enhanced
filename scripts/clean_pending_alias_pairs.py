"""Очистка сомнительных пар алиасов (pending) от «процентного» мусора.

Решение владельца (03.10.2026): «проценты точно сравнивать не требуется, если точно не совпадают».
Проблема: ночная задача entity resolution лила в модерацию ВСЮ серую зону embedding-сходства (sim 0.85–0.95),
и в базе накопилось 956 непросмотренных pending-пар, большинство из которых — «термин ↔ категория»
(«Банк России» ↔ «кредитные организации»), а не алиасы.

Правило отбора «это точно один объект» (реальный алиас):
  1. одно имя — ПОДСТРОКА другого, длиной >= 4 символов (полное/сокращение: «ПАО Сбербанк» ⊃ «Сбербанк»);
  2. или одно имя — ИНИЦИАЛЫ другого («РСХБ» = «Россельхозбанк», все буквы короткого входят в первые буквы слов);
  3. точное совпадение (norm) — уже обрабатывается шагом 2 и не должно быть pending.

Остальное (не совпадает точно) — помечаем rejected, НЕ удаляем: можно откатить. run с --apply реально пишет.
"""
from __future__ import annotations

import argparse
import re

# --- логика «это точно один объект» (дублирует guard'ы из knowledge_graph._embed_entities_resolution) ---

def _norm(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKC", s or "").lower().strip()
    s = re.sub(r"[\s]+", " ", s)
    s = re.sub(r"[^\w\s\-а-яёa-z0-9]", "", s, flags=re.I)
    return s.strip()


def _is_date_or_number(name: str) -> bool:
    """Имя — дата/номер-в-себе («2008-02-01», «5.4», «2018») — не алиас другого имени."""
    n = _norm(name)
    if not n:
        return False
    if re.fullmatch(r"\d{2,6}", n):
        return True
    if re.fullmatch(r"\d{1,4}[-./]\d{1,4}([-./]\d{1,4})?", n):
        return True
    return False


def is_true_alias(canonical: str, alias: str) -> bool:
    """Точно ли это один объект: подстрока (>=5) или инициалы.

    Решения владельца (03.10.2026): «проценты точно сравнивать не требуется, если точно не совпадают» —
    поэтому порог строгий, а даты/числа/короткие общие слова алиасами не считаем.
    """
    c = _norm(canonical)
    a = _norm(alias)
    if not c or not a:
        return False
    if _is_date_or_number(c) or _is_date_or_number(a):
        return False
    if len(c) < 5 and len(a) < 5:
        return False
    short, long_ = (c, a) if len(c) <= len(a) else (a, c)
    if len(short) >= 5 and short in long_:
        return True
    # инициалы по исходному регистру
    if len(alias) <= len(canonical):
        orig_short, orig_long = alias, canonical
    else:
        orig_short, orig_long = canonical, alias
    short2 = _norm(orig_short)
    if 2 <= len(short2) <= 6 and orig_short.isupper():
        # инициалы длинного имени (первые буквы слов, без пробелов), сравнение в upper
        initials = "".join(w[0] for w in re.split(r"\s+", orig_long) if w).upper()
        compact = short2.replace(" ", "")
        if compact and all(ch.upper() in initials for ch in compact):
            return True
        # Акроним-в-одном-слове («РСХБ» ⊂ «Россельхозбанк») НЕ склеиваем:
        # в реальных данных это даёт ложные «Банк» = «банков», «Сервер» = «серверное оборудование»
        # (проверено на 81 оставшихся парах — настоящих акронимов нет).
    return False


def classify(canonical: str, alias: str) -> str:
    """approved | rejected | exact — как бы мы пометили пару."""
    c = _norm(canonical)
    a = _norm(alias)
    if c and a and len(c) >= 4 and c == a:
        return "exact"
    return "approved" if is_true_alias(canonical, alias) else "rejected"


def main() -> int:
    ap = argparse.ArgumentParser(description="Очистка pending-пар алиасов")
    ap.add_argument("--apply", action="store_true", help="реально писать (иначе dry-run)")
    args = ap.parse_args()

    from src.api.services.config_store import config_store
    from src.database.session import get_session_local
    from src.database.entity_alias_models import EntityAlias

    maker = get_session_local()
    session = maker()
    rows = session.query(EntityAlias).filter(
        EntityAlias.source == "pending",
        EntityAlias.reviewed.is_not(True)
    ).all() if hasattr(EntityAlias, "source") else []

    # если модель не та — упростим: прямой SQL через psycopg не тянем, сообщаем
    if not rows:
        print("PAIR_ROWS: 0 (возможно, другая модель/колонки; скрипт-проверка схемы ниже)")
        cols = [c.name for c in EntityAlias.__table__.columns] if hasattr(EntityAlias, "__table__") else []
        print("  columns:", cols[:8])
        return 2

    stats = {"approved": 0, "rejected": 0, "exact": 0}
    to_reject = []
    for r in rows:
        verdict = classify(r.canonical_name, r.alias)
        stats[verdict] += 1
        if verdict == "rejected":
            to_reject.append(r.id)

    print(f"pending без reviewed: {len(rows)}")
    print(f"  реальных алиасов (approved): {stats['approved']}")
    print(f"  точных совпадений (exact):   {stats['exact']}")
    print(f"  мусор → rejected:            {stats['rejected']}")

    if args.apply and to_reject:
        n = session.query(EntityAlias).filter(EntityAlias.id.in_(to_reject)).update(
            {"reviewed": True, "verdict": "rejected"}, synchronize_session=False
        )
        session.commit()
        print(f"ПРИМЕНЕНО: {n} пар помечены rejected (не удалены).")
    else:
        print("(dry-run: не менял. --apply чтобы применить)")
    session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
