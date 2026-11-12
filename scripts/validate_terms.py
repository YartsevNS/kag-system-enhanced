"""Автоматическая валидация словаря: правило «одинаковые буквы → это один и тот же термин».

Правило владельца: если вариант и канон совпадают по буквам (различия только в регистре,
пробелах, знаках препинания и ё/е), то это один и тот же термин — подтверждаем без человека.

Что НЕ подтверждается этим прогоном (остаётся человеку): сокращения (АВЗ → Антивирусная
защита), пары с разными словами, спорные определения.

Идемпотентно: повторный прогон ничего не меняет и печатает те же счётчики.
"""
import argparse
import logging
import re
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("validate-terms")


def _norm(value: str) -> str:
    """Нормализация для сравнения «по буквам»: регистр, ё, пунктуация, пробелы."""
    text = (value or "").lower().replace("ё", "е")
    text = re.sub(r"[«»\"'`.,;:!?()\[\]{}<>*—–−―‐‑/\\]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from src.database.entity_alias_models import EntityAlias
    from src.database.session import get_session_local

    session = get_session_local()()
    try:
        rows = session.query(EntityAlias).all()
        identical = []
        for row in rows:
            if _norm(row.alias) == _norm(row.canonical_name) and _norm(row.alias):
                identical.append(row)

        already = [r for r in identical
                   if r.reviewed and (r.verdict or "") == "approved"]
        to_approve = [r for r in identical if r not in already]
        logger.info("всего пар: %s | совпадают по буквам: %s | из них уже подтверждено: %s",
                    len(rows), len(identical), len(already))
        for row in to_approve[:10]:
            print(f"   подтверждаю: «{row.alias}» = «{row.canonical_name}»")
        if args.dry_run:
            print(f"dry-run: подтвердили бы {len(to_approve)} пар")
            return 0

        for row in to_approve:
            row.reviewed = True
            row.verdict = "approved"
            row.comment = (row.comment or "")
            if "авто" not in row.comment.lower():
                row.comment = (row.comment + " | авто-подтверждено: совпадение по буквам").strip(" |")
        session.commit()
        print(f"подтверждено пар: {len(to_approve)} (совпадают по буквам)")

        pending = (session.query(EntityAlias)
                   .filter(EntityAlias.reviewed.is_(False)).count())
        approved = (session.query(EntityAlias)
                    .filter(EntityAlias.verdict == "approved").count())
        print(f"итог словаря: всего {session.query(EntityAlias).count()}, "
              f"подтверждено {approved}, ждут решения {pending}")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
