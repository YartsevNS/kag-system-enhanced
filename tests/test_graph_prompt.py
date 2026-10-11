"""Компактный список типов связей для задания модели (graph_ontology.relations_compact).

Зачем стражи: в задании модели раньше лежал обрезанный по знакам текст правил
(`prompt_rules()[:3000]`) — обрезка рубила список ПОСРЕДИНЕ описания типа, и часть типов модель
не видела вовсе (9943 знака прозы → 3000). Теперь в задание уходит компактный список: строка на
тип, <код> — <смысл>, около 1,2 тысячи знаков. Правило простое и его надо держать: ЕДИНИЦА
РАБОТЫ — фрагмент из Qdrant, а не простыня инструкций. Тесты стерегут, что список не обрезан,
не разъехался с онтологией и что новый тип нельзя молча забыть.
"""
import re
from pathlib import Path

from src.indexing.graph_ontology import (
    RELATIONS,
    SEMANTIC_CODES,
    SHORT_MEANING,
    _WITHHELD,
    prompt_rules,
    relations_compact,
    validate_triple,
)

ROOT = Path(__file__).resolve().parents[1]


def test_links_from_clause_are_allowed():
    """Ссылка ИЗ ПУНКТА — норма, а не нарушение схемы.

    Замер 11.10.2026 по ГОСТ 57580 (180 фрагментов): все 59 отказов схемы были ссылками из пункта
    («КЗИ.1 — КЗИ.7 настоящей таблицы», «приведен в таблице 51»). Правило браковало живой текст.
    """
    for dst in ("clause", "standard", "document_ref"):
        ok, note = validate_triple("clause", "references", dst)
        assert ok, f"пункт ссылается на {dst}: {note}"
    ok, note = validate_triple("clause", "references", "person")
    assert not ok, "ссылка пункта на человека остаётся недопустимой"


def test_every_type_has_short_meaning():
    """Новый тип связи нельзя добавить, не описав его одной строкой."""
    missing = [s.code for s in RELATIONS if not SHORT_MEANING.get(s.code)]
    assert not missing, f"нет короткого смысла для типов: {missing}"


def test_short_meaning_has_no_unknown_codes():
    """В словаре нет кодов, которых нет в онтологии (переименование кода ловится здесь)."""
    extra = [code for code in SHORT_MEANING if code not in SEMANTIC_CODES]
    assert not extra, f"короткий смысл для неизвестных типов: {extra}"


def test_short_meaning_is_one_short_line():
    """Одна строка, без обрывов: смысл укладывается в 40 знаков и не заканчивается многоточием."""
    for code, meaning in SHORT_MEANING.items():
        assert len(meaning) <= 40, f"{code}: смысл длиннее строки ({len(meaning)} знаков)"
        assert "…" not in meaning, f"{code}: смысл обрезан многоточием"
        assert "\n" not in meaning, f"{code}: смысл в несколько строк"


def test_compact_list_has_every_type():
    """Ни один тип из онтологии не потерян (кроме снятых замером, см. _WITHHELD)."""
    text = relations_compact()
    for spec in RELATIONS:
        if spec.code in _WITHHELD:
            assert spec.code not in text, f"{spec.code} снят с предложения модели, но в списке есть"
        else:
            assert re.search(rf"^{re.escape(spec.code)} — \S", text, re.M), \
                f"{spec.code} выпал из компактного списка"


def test_compact_list_is_short_and_without_rules_junk():
    """Список короткий и в нём нет ни пар, ни примеров, ни правил — только типы."""
    text = relations_compact()
    assert len(text) < 1600, f"список разросся: {len(text)} знаков вместо ~1200"
    assert len(text) * 4 < len(prompt_rules()), "список должен быть в разы короче полного текста правил"
    for junk in ("допустимо:", "верно:", "НЕВЕРНО:", "ЗАДАЧА:", "ЗАПРЕТ"):
        assert junk not in text, f"в компактный список попало из правил: {junk}"
    assert "…" not in text, "в списке есть обрывки фраз"
    for line in text.splitlines():
        assert re.match(r"^[a-z][a-z-]* — .+[а-яё0-9)]$", line), f"строка не по форме: {line}"


def test_v2_takes_compact_list_not_cut_rules():
    """Структурная проверка: v2 берёт компактный список, а не обрезку правил по знакам.

    Иначе правка тихо откатится к обрезке и половина типов снова не дойдёт до модели.
    Смотрим КОД без комментариев: в пояснении к правке обрезка упоминается по имени.
    """
    src = (ROOT / "src/indexing/extraction_v2.py").read_text(encoding="utf-8")
    code = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))
    assert "relations_compact()" in code, "v2 больше не берёт компактный список типов"
    assert not re.search(r"prompt_rules\(\)\s*\[", code), "в v2 вернулась обрезка правил по знакам"
    assert not re.search(r"prompt_rules\(\)", code), "в задание модели снова уходит полный текст правил"
