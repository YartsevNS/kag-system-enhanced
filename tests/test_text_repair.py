"""Битый текстовый слой PDF: определение и восстановление кодировки."""
from pathlib import Path

from src.indexing.text_repair import (
    cyrillic_share,
    looks_like_mojibake,
    needs_ocr,
    repair_mojibake,
)

BAD = ("ÔÅÄÅÐÀËÜÍÎÅ ÀÃÅÍÒÑÒÂÎ ÏÎ ÒÅÕÍÈ×ÅÑÊÎÌÓ ÐÅÃÓËÈÐÎÂÀÍÈÞ È ÌÅÒÐÎËÎÃÈÈ "
       "Ð Å Ê Î Ì Å Í Ä À Ö È È Ï Î Ñ Ò À Í Ä À Ð Ò È Ç À Ö È È")
GOOD = ("ФЕДЕРАЛЬНОЕ АГЕНТСТВО ПО ТЕХНИЧЕСКОМУ РЕГУЛИРОВАНИЮ И МЕТРОЛОГИИ "
        "РЕКОМЕНДАЦИИ ПО СТАНДАРТИЗАЦИИ")


def test_битый_слой_определяется():
    assert looks_like_mojibake(BAD)


def test_нормальный_текст_не_считается_битым():
    assert not looks_like_mojibake(GOOD)
    assert not looks_like_mojibake("Требования к средствам защиты информации. " * 3)


def test_короткий_текст_не_трогаем():
    assert not looks_like_mojibake("ôóíêöèÿ")


def test_восстановление_кодировки():
    fixed = repair_mojibake(BAD)
    assert cyrillic_share(fixed) > 0.8, fixed[:60]
    assert "ФЕДЕРАЛЬНОЕ" in fixed.upper()
    assert cyrillic_share(BAD) < 0.10


def test_восстановление_с_символами_вне_latin1():
    """Тире/кавычки вне latin-1 не должны ломать восстановление (живой случай)."""
    text = "ÔÅÄÅÐÀËÜÍÎÅ — ÀÃÅÍÒÑÒÂÎ «Ð» ÏÎ ¹3 ÌÅÒÐÎËÎÃÈÈ È ÑÒÀÍÄÀÐÒÈÇÀÖÈÈ"
    fixed = repair_mojibake(text)
    assert "ФЕДЕРАЛЬНОЕ" in fixed.upper()
    assert "—" in fixed and "«" in fixed, "символы вне latin-1 сохраняются как есть"


def test_нужен_ли_ocr():
    assert not needs_ocr(BAD), "восстановимая кодировка — OCR не нужен"
    assert not needs_ocr(GOOD)


def test_сброс_кэша_ocr_на_документ():
    src = Path("src/indexing/parsers.py").read_text(encoding="utf-8")
    assert "_ocr_pages_cache" in src, "OCR всего PDF должен вызываться один раз"
    assert "отдаю на OCR" in src, "битый и невосстановимый слой отдаётся на OCR"
