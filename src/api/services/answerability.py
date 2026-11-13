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


_QUESTION_WORDS = frozenset("""
какие какой какая каких каким какую каков какова что чем чего кто где когда
почему зачем сколько скольких как или либо пожалуйста скажи покажи найди
перечисли расскажи дай объясни опиши
""".split())


def _pairs(text: str) -> List[tuple]:
    """Пары (слово как в тексте, стем) для сравнения и показа человеку.

    Стем нужен для сравнения с онтологией, а исходное слово — для очереди
    пробелов и словаря: там «биоме» вместо «биометрической» бессмысленно,
    человек такое не подтвердит и в глоссарий оно попадёт мусором.
    Вопросительные слова отбрасываются отдельно: список стоп-слов BM25 не
    содержит «какие»/«сколько», и без этого каждый вопрос терял покрытие
    на собственном вопросительном слове.
    """
    import re

    from src.indexing.lexical import STOPWORDS_RU, stem_ru

    out: List[tuple] = []
    for w in re.findall(r"[а-яёa-z0-9][а-яёa-z0-9\-]*", (text or "").lower().replace("ё", "е")):
        if len(w) < 3 or w in STOPWORDS_RU or w in _QUESTION_WORDS:
            continue
        out.append((w, stem_ru(w)))
    return out


def tokens(text: str) -> List[str]:
    """Значимые слова вопроса в исходной форме (для показа человеку)."""
    return [w for w, _ in _pairs(text)]


def _stem(word: str) -> str:
    from src.indexing.lexical import stem_ru

    return stem_ru(word)


def _stems_of(text: str) -> List[str]:
    return [stem for _w, stem in _pairs(text)]


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
пользователь пользователя пользователей пользователи пользователям
""".split())

# Глагольные и наречные окончания: в очередь пробелов должны попадать ПОНЯТИЯ
# (термины, названия, аббревиатуры), а не «потратили» и «применяет». Отсутствие
# глагола в онтологии ничего не значит — такой пробел человек не подтвердит.
_VERB_TAILS = (
    "ал", "ала", "али", "ало", "ать", "ять", "ить", "еть", "уть", "ыть",
    "ют", "ут", "ят", "ит", "ат", "ется", "ются", "ится", "атся", "лся",
    "лась", "лись", "лось", "ешь", "ишь", "ите", "йте", "ует", "уют",
    "ает", "ают", "яет", "яют", "ирует", "ируют", "овать", "евать",
    "ывать", "ивать", "ться", "или", "ыли", "ели",
    "применяет", "применяют", "использует", "используют",
)


def _is_term_candidate(word: str) -> bool:
    """Похоже ли слово на понятие, а не на глагол/общее слово.

    Короткие слова НЕ отбрасываются: аббревиатуры из трёх букв (АВЗ, ОПС, ЗНИ)
    — это и есть самые нужные кандидаты в словарь. Отбрасываются только общие
    слова и глагольные формы.
    """
    w = (word or "").strip().lower()
    if len(w) < 3 or w in _GENERIC:
        return False
    if any(w.endswith(t) for t in _VERB_TAILS):
        return False
    return True


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
            if not _is_term_candidate(term):
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
    """Полный прогон для одного вопроса: покрытие + (по желанию) запись пробелов.

    Пробел записывается только если в вопросе есть хотя бы одно слово, известное
    онтологии: иначе вопрос не про нашу предметную область, и это не пробел схемы,
    а посторонний вопрос. Иначе очередь заполняется «потратили», «погода», «флешек» —
    человек такое не подтвердит, а шум прячет настоящие пробелы.
    """
    res = check(question)
    if not save or not res["missing"]:
        return res
    if not res["covered"]:
        res["recorded"] = 0
        res["gap_note"] = "вопрос вне предметной области: ни одно слово не знакомо онтологии"
        return res
    res["recorded"] = record_gap(question, res["missing"])
    return res
