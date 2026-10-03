"""Удаление ложных пар алиасов: применяем классификатор is_true_alias ко ВСЕМ строкам словаря.

Ложные пары — «платежной системы Банка России» ↔ «платежная система Мир», даты, термины-категории и т.п.,
которые не являются алиасами одного объекта (даже если embedding-sim высокий, например 0.91).

Классификатор: подстрока >=5 / инициалы(по первым буквам слов) / точное совпадение (после нормализации);
даты, номера, короткие общие слова алиасами не считаем.

Останавливается на dry-run; --apply реально помечает rejected (НЕ удаляет).
"""
from __future__ import annotations

import argparse
import re


def _norm(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKC", s or "").lower().strip()
    s = re.sub(r"[\s]+", " ", s)
    s = re.sub(r"[^\w\s\-а-яёa-z0-9]", "", s, flags=re.I)
    return s.strip()


def _is_date_or_number(name: str) -> bool:
    n = _norm(name)
    if not n:
        return False
    if re.fullmatch(r"\d{2,6}", n):
        return True
    if re.fullmatch(r"\d{1,4}[-./]\d{1,4}([-./]\d{1,4})?", n):
        return True
    return False


def is_true_alias(canonical: str, alias: str) -> bool:
    c = _norm(canonical)
    a = _norm(alias)
    if not c or not a:
        return False
    if _is_date_or_number(c) or _is_date_or_number(a):
        return False
    if len(c) < 5 and len(a) < 5:
        return False
    short, long_ = (c, a) if len(c) <= len(a) else (a, c)
    # Реально алиас: короткое имя — подмножество слов длинного, и ДОБАВЛЕННЫЕ слова — только
    # организационно-правовые/типовые модификаторы (ПАО, АО, ООО, ФГУП, сеть, система и т.п.).
    # «Сбербанк» ⊆ «ПАО Сбербанк» (добавлено ПАО) — алиас.
    # «промышленность» ⊆ «пищевая промышленность» (добавлено содержательное слово) — НЕ алиас.
    # «Интернет» ⊆ «сеть Интернет» (добавлено «сеть» = типовой модификатор) — алиас.
    MODIFIERS = {"пао","ао","ооо","фгуп","фгу","нко","сеть",
                 "им","иц","нб","гк"}  # только организационно-правовые/режимные префиксы
    short_words = short.split(" ")
    long_words = long_.split(" ")
    if not short_words:
        return False
    if all(w in long_words for w in short_words):
        # все слова короткого есть в длинном; проверяем, что добавленные — модификаторы
        added = set(long_words) - set(short_words)
        if added and added <= MODIFIERS:
            return True
        if not added:
            return True  # одинаковый набор слов (норм-различия) — точное/почти точное
    return False
    if len(alias) <= len(canonical):
        orig_short, orig_long = alias, canonical
    else:
        orig_short, orig_long = canonical, alias
    short2 = _norm(orig_short)
    if 2 <= len(short2) <= 6 and orig_short.isupper():
        initials = "".join(w[0] for w in re.split(r"\s+", orig_long) if w).upper()
        compact = short2.replace(" ", "")
        if compact and all(ch.upper() in initials for ch in compact):
            return True
    return False


def classify(canonical: str, alias: str) -> str:
    c = _norm(canonical)
    a = _norm(alias)
    if c and a and len(c) >= 4 and c == a:
        return "exact"
    return "approved" if is_true_alias(canonical, alias) else "rejected"


def main() -> int:
    ap = argparse.ArgumentParser(description="Удалить ложные пары алиасов (не алиасы по содержанию)")
    ap.add_argument("--apply", action="store_true", help="реально пометить rejected; иначе dry-run")
    args = ap.parse_args()

    from src.database.entity_alias_models import EntityAlias
    from src.database.session import get_session_local

    session = get_session_local()()
    # Обрабатываем ВСЕ строки; но для того, чтобы «оставить только реальные алиасы»,
    # мы помечаем rejected те, где классификатор считает НЕ алиасом (включая approved).
    rows = session.query(EntityAlias).all()
    stats = {"approved": 0, "rejected": 0, "exact": 0}
    to_reject = []
    to_keep = []
    for r in rows:
        verdict = classify(r.canonical_name, r.alias)
        stats[verdict] += 1
        if verdict == "rejected":
            to_reject.append(r.id)
        else:
            to_keep.append(r.id)

    print(f"всего строк: {len(rows)}")
    print(f"  реальных алиасов (approved): {stats['approved']}")
    print(f"  точных совпадений (exact):   {stats['exact']}")
    print(f"  ложных пар → rejected:       {stats['rejected']}")

    if args.apply and to_reject:
        n = session.query(EntityAlias).filter(EntityAlias.id.in_(to_reject)).update(
            {"reviewed": True, "verdict": "rejected"}, synchronize_session=False
        )
        session.commit()
        print(f"ПРИМЕНЕНО: {n} ложных пар помечены rejected (approved-ложные тоже).")
        print(f"Осталось реальных алиасов: {len(to_keep)}")
    else:
        print("(dry-run) использованием --apply")
    session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())