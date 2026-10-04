"""Скан в PDF — это документ-изображение: проверяем определение слоя и рендер страницы.

Ошибка, которую эти тесты закрывают: табличная ветка принимала только .png/.jpg, поэтому смета,
отсканированная в PDF, оставалась без таблиц (найдено 04.10.2026).
"""
import fitz
from PIL import Image, ImageDraw

from src.indexing.table_strategy import has_text_layer, render_document_pages


def _scanned_pdf(tmp_path):
    """PDF с картинкой-таблицей и БЕЗ текстового слоя (как скан из МФУ)."""
    img = Image.new("RGB", (600, 400), "white")
    d = ImageDraw.Draw(img)
    for x in (50, 250, 400, 550):
        d.line([(x, 50), (x, 350)], fill="black", width=2)
    for y in (50, 110, 170, 230, 290, 350):
        d.line([(50, y), (550, y)], fill="black", width=2)
    png = tmp_path / "scan.png"
    img.save(png)

    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_image(fitz.Rect(60, 100, 535, 420), filename=str(png))
    out = tmp_path / "scan.pdf"
    doc.save(out)
    doc.close()
    return out


def _text_pdf(tmp_path):
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 100), "Text layer document", fontsize=12)
    out = tmp_path / "text.pdf"
    doc.save(out)
    doc.close()
    return out


def test_scanned_pdf_has_no_text_layer(tmp_path):
    assert has_text_layer(str(_scanned_pdf(tmp_path))) is False


def test_text_pdf_has_text_layer(tmp_path):
    # в текстовом PDF мало символов на страницу — проверяем порог явно
    assert has_text_layer(str(_text_pdf(tmp_path)), min_chars_per_page=1) is True


def test_render_pdf_page_returns_image_bytes(tmp_path):
    pages = render_document_pages(str(_scanned_pdf(tmp_path)))
    assert len(pages) == 1
    assert pages[0][:4] == b"\x89PNG"
    img = Image.open(__import__("io").BytesIO(pages[0]))
    assert img.size[0] > 500


def test_render_image_file_reads_as_is(tmp_path):
    png = tmp_path / "plain.png"
    Image.new("RGB", (10, 10), "white").save(png)
    pages = render_document_pages(str(png))
    assert len(pages) == 1 and pages[0][:4] == b"\x89PNG"
