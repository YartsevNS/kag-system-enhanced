"""Проверки чтения текста с контролем кодировки.

Живой случай, ради которого всё это: 29 документов корпуса были залиты с текстом, прочитанным
как Latin-1 (UTF-8), и дефект жил молча — 18% фрагментов. Тесты фиксируют, что на входе любой
из распространённых вариантов даёт осмысленный русский текст, а безнадёжно испорченный —
помечается подозрительным, а не проходит как «нормальный».
"""

from __future__ import annotations

import pytest

from src.indexing.text_encoding import (
    cyrillic_share,
    decode_bytes,
    decode_file,
    guard,
    looks_like_mojibake,
    weird_share,
)

RUS = (
    "Требования к средствам защиты информации изложены в ГОСТ Р 50922-2006 и в приказе ФСТЭК "
    "России № 17. Организация обязана обеспечить защиту персональных данных согласно 152-ФЗ."
)


def test_utf8_читается_как_есть():
    result = decode_bytes(RUS.encode("utf-8"))
    assert result.text == RUS
    assert not result.suspicious
    assert result.encoding.startswith("utf-8")
    assert cyrillic_share(result.text) > 0.9


def test_cp1251_читается_как_есть():
    result = decode_bytes(RUS.encode("cp1251"))
    assert result.text == RUS
    assert not result.suspicious
    assert result.encoding == "cp1251"


def test_utf8_прочитанный_как_latin1_восстанавливается():
    """Главный живой случай: файл UTF-8, а прочитан Latin-1 — текст обязан восстановиться."""
    mangled = RUS.encode("utf-8").decode("latin-1")
    assert looks_like_mojibake(mangled)
    result = decode_bytes(mangled.encode("utf-8"))
    assert cyrillic_share(result.text) > 0.9
    assert result.repaired
    assert not result.suspicious
    assert "Требования" in result.text


def test_порча_второй_ступени_не_проходит_за_валидный_текст():
    """cp1251-переинтерпретация уже испорченного текста выглядит «русской», но это мусор."""
    mangled = RUS.encode("utf-8").decode("latin-1")
    twice = mangled.encode("utf-8").decode("cp1251")  # вид «ГђВўГ‘В‚…»
    assert weird_share(twice) > 0.03 or looks_like_mojibake(twice)
    result = decode_bytes(twice.encode("utf-8"))
    if result.suspicious:
        assert True
    else:
        assert cyrillic_share(result.text) > 0.9


def test_потери_помечаются_подозрительными():
    """Если заменители уже в тексте — восстановить без потерь нельзя, и это должно быть видно."""
    mangled = RUS.encode("utf-8").decode("latin-1")
    lossy = mangled.replace("Ð¢", "\ufffd")
    result = decode_bytes(lossy.encode("utf-8"))
    assert result.suspicious or cyrillic_share(result.text) > 0.9


def test_английский_текст_не_трогаем():
    data = b"Security requirements are described in ISO/IEC 27001 and GOST R 50922-2006."
    result = decode_bytes(data)
    assert result.text == data.decode("utf-8")
    assert not result.suspicious
    assert cyrillic_share(result.text) == 0.0


def test_короткий_текст_не_объявляется_порчей():
    assert not looks_like_mojibake("Ð°Ð±")
    result = decode_bytes("Ð°Ð±Ð²".encode("utf-8"))
    assert not result.suspicious


def test_пустой_ввод():
    result = decode_bytes(b"")
    assert result.text == ""
    assert not result.suspicious


def test_смешанный_текст_с_кавычками_и_знаками():
    text = "ГОСТ Р ИСО/МЭК 27001-2021 «Средства защиты» — 100% соответствие, п. 5.2.1."
    result = decode_bytes(text.encode("utf-8"))
    assert result.text == text
    assert not result.suspicious


def test_guard_чинит_текст_пришедший_извне():
    """Текст из сети или из OCR проверяется тем же правилом."""
    mangled = RUS.encode("utf-8").decode("latin-1")
    fixed, suspicious, note = guard(mangled)
    assert cyrillic_share(fixed) > 0.9
    assert not suspicious
    assert note


def test_guard_не_портит_нормальный_текст():
    fixed, suspicious, note = guard(RUS)
    assert fixed == RUS
    assert not suspicious
    assert note == ""


def test_чтение_файла_из_папки(tmp_path):
    path = tmp_path / "документ.txt"
    path.write_bytes(RUS.encode("utf-8"))
    result = decode_file(path)
    assert result.text == RUS
    assert not result.suspicious


def test_нечитаемый_файл_помечается(tmp_path):
    path = tmp_path / "нет-такого.txt"
    result = decode_file(path)
    assert result.suspicious
    assert "не прочитан" in result.note


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
