"""Покрытие вопроса онтологией: ответ «мы этого не знаем» до генерации, а не после.

Зачем: агент, который молча отвечает на вопрос, которого нет в данных, — главный
источник недоверия. В auto-ontology (NVIDIA) для этого есть check_answerable:
сначала считается, покрыт ли вопрос определениями онтологии, и только потом
строится ответ. Здесь то же самое, но источник смысла другой — не колонки БД,
а словарь (entity_aliases) + типы и связи доменной схемы.

Второй эффект — цикл Learn: непокрытые термины не теряются, а копятся в
ontology_schema_gaps с частотой и примером вопроса. Принятый пробел становится
термином словаря (см. routes/ontology.py::schema_gap_review) и в следующий раз
считается покрытым. Так онтология растёт от реальных вопросов заказчика,
а не от того, что кто-то вспомнил добавить тип.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# Порог: ниже него ответ считаем непокрытым и говорим об этом честно.
# 0.6 выбран так, чтобы обычный вопрос «какие меры защиты применяет организация»
# (меры/защита/организация — известны) проходил, а «сколько потратили на рекламу»
# (потратили/рекламу — неизвестны) — нет.
COVERAGE_THRESHOLD = 0.6

_vocab_cache: Dict[str, Any] = {"at": 0.0, "stems": set(), "size": 0}
_VOCAB_TTL = 300.0


def tokens(text: str) -> List[str]:
    """Значимые слова вопроса.

    Токенизация и стоп-слова берутся из src/indexing/lexical.py — того же модуля,
    что работает в BM25-ветке поиска. Свой список стоп-слов уже расходился с ним:
    «меры» в вопросе и «мера» в схеме считались разными словами.
    """
    from src.indexing.lexical import tokenize_lexical

    return tokenize_lexical(text or "")


def _stem(word: str) -> str:
    from src.indexing.lexical import stem_ru

    return stem_ru(word)


def _stems_of(text: str) -> List[str]:
    try:
        from src.indexing.lexical import stem_ru, tokenize_lexical

        return [stem_ru(w) for w in tokenize_lexical(text or "") if len(w) >= 3]
    except Exception:  # noqa: BLE001
        return []


def _schema_words() -> List[str]:
    from src.indexing.graph_ontology import ENTITY_TYPES, RELATIONS

    out: List[str] = []
    for code, meaning in ENTITY_TYPES.items():
        out.append(str(code).replace("_", " "))
        out.append(str(meaning))
    for spec in RELATIONS:
        out.append(spec.title)
        out.append(spec.meaning)
    return out


def vocabulary(force: bool = False) -> set:
    """Стемы слов, которые онтология «знает».

    Источники: подтверждённые пары словаря (вариант и канон), названия типов и
    связей доменной схемы, коды мер. Кеш 5 минут: словарь меняется человеком,
    но не чаще.
    """
    now = time.time()
    if not force and _vocab_cache["stems"] and now - _vocab_cache["at"] < _VOCAB_TTL:
        return _vocab_cache["stems"]

    stems: set = set()
    try:
        from sqlalchemy import text

        from src.database.session import get_session_local

        db = get_session_local()()
        try:
            rows = db.execute(text(
                "SELECT alias, canonical_name FROM entity_aliases "
                "WHERE verdict = 'approved' LIMIT 5000"
            )).all()
        finally:
            db.close()
        for alias, canonical in rows:
            for part in (alias, canonical):
                stems.update(_stems_of(part or ""))
    except Exception as exc:  # noqa: BLE001 - без словаря считаем по схеме, чат важнее
        logger.debug("покрытие: словарь недоступен: %s", str(exc)[:160])

    try:
        for text in _schema_words():
            stems.update(_stems_of(text))
    except Exception as exc:  # noqa: BLE001
        logger.debug("покрытие: схема недоступна: %s", str(exc)[:160])

    _vocab_cache.update({"at": now, "stems": stems, "size": len(stems)})
    return stems


def check(question: str) -> Dict[str, Any]:
    """Покрытие вопроса: доля значимых слов, известных онтологии."""
    words = tokens(question)
    vocab = vocabulary()
    if not words:
        return {"coverage": 0.0, "verdict": "empty", "covered": [], "missing": [],
                "threshold": COVERAGE_THRESHOLD, "vocab_size": len(vocab)}
    covered, missing = [], []
    for w in words:
        (covered if _stem(w) in vocab else missing).append(w)
    coverage = round(len(covered) / len(words), 3)
    verdict = "covered" if coverage >= COVERAGE_THRESHOLD else (
        "partial" if coverage >= 0.3 else "uncovered")
    return {"coverage": coverage, "verdict": verdict, "covered": covered,
            "missing": missing, "threshold": COVERAGE_THRESHOLD,
            "vocab_size": len(vocab)}


_GAPS_DDL = """
    CREATE TABLE IF NOT EXISTS ontology_schema_gaps (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        term text NOT NULL UNIQUE,
        hits integer NOT NULL DEFAULT 0,
        examples jsonb NOT NULL DEFAULT '[]'::jsonb,
        status text NOT NULL DEFAULT 'proposed',
        reviewer text, reviewed_at timestamptz, note text,
        created_at timestamptz NOT NULL DEFAULT now()
    )
"""


def _ensure_gaps(db) -> None:
    """Таблица пробелов создаётся здесь же: чат может спросить раньше админки."""
    from sqlalchemy import text

    db.execute(text(_GAPS_DDL))
    db.commit()


_GENERIC = frozenset("""
безопасность информация информационный защита требование мера документ документы
система системы контроль обеспечение порядок случай основание соответствие помощь
пример данные данные сведения лицо лица организация организации российской
федерации россия году года число цель который которая установленный настоящий
пункт пункты раздел статья действие применение применяет является должен можно
нужно также более менее самый очень через между после перед кроме всего
""".split())


def record_gap(question: str, missing: List[str], source: str = "chat") -> int:
    """Дописать непокрытые термины в очередь пробелов схемы (идемпотентно)."""
    from sqlalchemy import text

    from src.database.session import get_session_local

    if not missing:
        return 0
    db = get_session_local()()
    added = 0
    _ensure_gaps(db)
    example = {"q": (question or "")[:300], "src": source}
    try:
        for term in dict.fromkeys(w.lower() for w in missing):
            if len(term) < 3 or term in _GENERIC:
                continue
            term = term[:200]
            row = db.execute(
                text("SELECT id::text, examples FROM ontology_schema_gaps WHERE term = :t"),
                {"t": term},
            ).mappings().first()
            if row is None:
                db.execute(text(
                    "INSERT INTO ontology_schema_gaps (term, hits, examples) "
                    "VALUES (:t, 1, cast(:ex as jsonb))"
                ), {"t": term, "ex": json.dumps([example], ensure_ascii=False)})
            else:
                old = row["examples"] or []
                if isinstance(old, str):
                    try:
                        old = json.loads(old)
                    except Exception:  # noqa: BLE001
                        old = []
                # частота — честный счётчик; примеры — только последние 5, чтобы
                # строка не разрослась до всего корпуса вопросов.
                merged = ([example] + [e for e in old if e != example])[:5]
                db.execute(text(
                    "UPDATE ontology_schema_gaps SET hits = hits + 1, examples = cast(:ex as jsonb) "
                    "WHERE term = :t"
                ), {"t": term, "ex": json.dumps(merged, ensure_ascii=False)})
            added += 1
        db.commit()
    except Exception as exc:  # noqa: BLE001 - сбой учёта не должен ронять чат
        db.rollback()
        logger.warning("покрытие: пробел не записан: %s", str(exc)[:200])
    finally:
        db.close()
    return added


def analyze(question: str, save: bool = True) -> Dict[str, Any]:
    """Полный прогон для одного вопроса: покрытие + (по желанию) запись пробелов."""
    res = check(question)
    if save and res["missing"]:
        res["recorded"] = record_gap(question, res["missing"])
    return res
