"""Тесты сборки PDF с невидимым текстовым слоем (searchable PDF).

Проверяем главное, ради чего это делалось: собранный PDF потом отдаётся просмотрщику, и текст из него
должен извлекаться — иначе выделения на странице не будет. Плюс поведение без шрифта: PDF всё равно
собирается (со страницей), просто без слоя.
"""
from __future__ import annotations

import io

import pytest

from src.indexing import searchable_pdf


def _png(width: int = 200, height: int = 100) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), (255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()


PAGE = [{
    "image": _png(),
    "width": 200,
    "height": 100,
    "lines": [
        {"text": "Счет-фактура №", "quad": [[10, 10], [90, 10], [90, 24], [10, 24]]},
        {"text": "13 959,92", "quad": [[10, 40], [70, 40], [70, 54], [10, 54]]},
    ],
}]


def _normalize(text: str) -> str:
    """Убрать особенности извлечения PDF: мягкий перенос и неразрывный пробел.

    При вставке текста шрифтом без Unicode-маппинга в поток могут попасть U+00AD (±) и U+00A0 вместо
    обычного дефиса и пробела. Для проверки «текст извлекается» это не важно, но поиск по документу
    сравнивает строки буквально, поэтому такие символы важно видеть и учитывать.
    """
    return text.replace("\u00ad", "").replace("\u00a0", " ")


def _is_prod_font() -> bool:
    """Проверки текста слоя имеют смысл на прод-шрифте (DejaVu): Windows-шрифты дают мягкие переносы."""
    font = searchable_pdf.find_cyrillic_font() or ""
    return "DejaVu" in font


def test_builds_pdf_with_extractable_text():
    """Главное требование: текст из собранного PDF извлекается (значит выделение на странице работает).

    Проверяем на прод-шрифте: он даёт точный Unicode, а подменные шрифты тестовой среды (Windows)
    возвращают мягкий перенос вместо дефиса — это их особенность, а не свойство нашего PDF.
    """
    if not _is_prod_font():
        pytest.skip("проверка текста требует прод-шрифта DejaVu (в тестовой среде его нет)")
    import fitz

    payload = searchable_pdf.build_searchable_pdf(PAGE)
    assert payload and payload[:4] == b"%PDF", "должен получиться PDF"

    doc = fitz.open(stream=payload, filetype="pdf")
    try:
        assert doc.page_count == 1
        text = _normalize(doc[0].get_text("text"))
    finally:
        doc.close()
    assert "Счет-фактура" in text, f"текст слоя: {text!r}"
    assert "13 959,92" in text, f"текст слоя: {text!r}"


def test_positions_match_source_coordinates():
    """Позиции строк в PDF должны совпадать с координатами распознавания, иначе выделение «съедет»."""
    if searchable_pdf.find_cyrillic_font() is None:
        pytest.skip("нет шрифта с кириллицей")
    import fitz

    payload = searchable_pdf.build_searchable_pdf(PAGE)
    doc = fitz.open(stream=payload, filetype="pdf")
    try:
        words = doc[0].get_text("words")
    finally:
        doc.close()
    assert words, "слой пуст"
    # Первое слово должно стоять примерно там, где в исходной рамке (x≈10, y≈10..24).
    x0, y0 = words[0][0], words[0][1]
    assert 8 <= x0 <= 14, f"смещение по x: {x0}"
    assert 8 <= y0 <= 26, f"смещение по y: {y0}"


def test_no_lines_returns_none():
    assert searchable_pdf.build_searchable_pdf([]) is None


def test_page_without_image_is_skipped():
    """Страница без изображения не должна ломать сборку — она просто пропускается."""
    payload = searchable_pdf.build_searchable_pdf([{"image": None, "width": 100, "height": 100, "lines": []}])
    assert payload is None


def test_quad_box_accepts_four_points_and_flat_form():
    box = searchable_pdf._quad_box([[1, 2], [5, 2], [5, 8], [1, 8]])
    assert box == (1.0, 2.0, 5.0, 8.0)
    assert searchable_pdf._quad_box([1, 2, 5, 8]) == (1.0, 2.0, 5.0, 8.0)
    assert searchable_pdf._quad_box([]) is None


def test_viewer_opens_searchable_pdf_and_has_fallback():
    """Регрессия: просмотрщик обязан узнавать про searchable PDF из карточки документа.

    Ошибка, которую это защищает (04.10.2026): просмотрщик определял тип по HEAD-запросу к /preview,
    HEAD отвечал 405, и скан открывался как картинка — а сервер отдавал PDF, поэтому на экране был
    пустой (чёрный) экран. Теперь признак приходит в карточке, и есть запасной путь «показать картинку».
    """
    from pathlib import Path

    html = Path("src/api/static/viewer.html").read_text(encoding="utf-8")
    assert "currentDoc.searchable_pdf" in html, "признак PDF со слоем должен браться из карточки документа"
    assert "initImageViewer" in html, "должен оставаться запасной путь — показать картинку"
    # Гадание по HEAD-запросу недопустимо: этот способ уже один раз сломал просмотрщик.
    assert "method: 'HEAD'" not in html
    # Скан открываем 1:1: при 150% браузер растягивает небольшой исходник, и картинка выглядит мыльной.
    assert "scale = 1.0;" in html, "для сканов масштаб по умолчанию должен быть 100%"
    # Подсказка горячих клавиш не должна исчезать и должна быть слегка прозрачной (просьба владельца).
    assert "opacity: 0.65" in html, "плотность подсказки — 0.65 (просьба владельца)"
    assert "font-size: 12px" in html, "шрифт подсказки на пункт крупнее (было 11 px)"
    assert "hint.style.opacity = '0'" not in html, "подсказка не должна исчезать сама"
