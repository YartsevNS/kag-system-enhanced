"""Тесты паспорта страницы и маршрутизации.

Что защищаем тестами:
  * цифровые PDF с разметкой НЕ гоняем через модель (иначе платим временем там, где не нужно);
  * сканы и картинки с таблицами — наоборот, отправляем модели;
  * повреждённый текстовый слой (побитая кодировка) распознаётся как «мусор» и уходит на OCR/модель;
  * у каждого решения есть причина — она показывается админу, поэтому её наличие проверяем.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.indexing.page_router import (  # noqa: E402
    ROUTE_OCR, ROUTE_PARSER, ROUTE_SKIP, ROUTE_VLM,
    PageSignals, decide_route, garbage_ratio, route_document, table_like_ratio,
)

DIGITAL_PAGE = (
    "1. Общие положения\n"
    "Настоящий документ устанавливает требования к системе радиационного контроля.\n"
    "2. Состав системы\n"
    "В состав входят блоки детектирования, устройства обработки и программное обеспечение.\n"
    "3. Требования к монтажу\n"
    "Монтаж выполняется в соответствии с проектной документацией.\n"
)

# Реалистичный фрагмент OCR-текста таблицы: названия и числа идут каждая на своей строке —
# именно так их отдаёт распознавание (проверено на «Сложном тесте.png» и квитанциях).
SCAN_TABLE_TEXT = (
    "Перечень устройств и блоков системы радиационного контроля\n"
    "Блок детектирования Контроль мощности поглощенной дозы гамма-излучения от трубопроводов\n"
    "4\n"
    "Блок детектирования Контроль мощности поглощенной дозы гамма-излучения второго контура\n"
    "28\n"
    "Блок детектирования Плотность потока промежуточных нейтронов\n"
    "12\n"
    "13 8\n"
    "Блок детектирования Объемная активность N-16\n"
    "1\n"
)

MOJIBAKE = "ÔÅÄÅÐÀËÜÍÎÅ àãåíòñòâî ïî òåõíè÷åñêîìó ðåãóëèðîâàíèþ è ìåòðîëîãèè\n" * 4


def test_digital_page_with_tables_is_not_sent_to_model():
    """Цифровая страница с размеченными таблицами — обычный парсер (модель дорогая по времени)."""
    sig = PageSignals.from_text(1, DIGITAL_PAGE, tables_found=2, worst_quality=0.92)
    d = decide_route(sig)
    assert d["route"] == ROUTE_PARSER
    assert "приемлемо" in d["reason"]


def test_scan_with_table_goes_to_model():
    """Картинка/скан без текстового слоя, но с табличным текстом — это работа для модели."""
    sig = PageSignals.from_text(3, SCAN_TABLE_TEXT, tables_found=0)
    assert sig.text_chars >= 200            # текст есть, но он от OCR
    assert sig.table_like > 0.3
    d = decide_route(sig)
    assert d["route"] == ROUTE_VLM, d
    assert "без разметки" in d["reason"]


def test_scan_without_tables_goes_to_our_ocr():
    """Скан без признаков таблицы — модель не нужна, хватит нашего OCR."""
    sig = PageSignals.from_text(2, "короткий текст", tables_found=0)
    d = decide_route(sig)
    assert d["route"] == ROUTE_OCR, d
    assert "таблиц не видно" in d["reason"]


def test_low_quality_table_goes_to_model():
    """Разметка есть, но качество низкое (объединённые ячейки, потерянные границы) — восстанавливаем моделью."""
    sig = PageSignals.from_text(5, DIGITAL_PAGE, tables_found=1, worst_quality=0.41)
    d = decide_route(sig)
    assert d["route"] == ROUTE_VLM, d
    assert "низкого качества" in d["reason"]


def test_damaged_text_layer_goes_to_model():
    """Побитая кодировка: текстовому слою доверять нельзя (реальный случай с пятью документами)."""
    sig = PageSignals.from_text(7, MOJIBAKE, tables_found=0)
    assert sig.garbage > 0.05, sig.garbage
    d = decide_route(sig)
    assert d["route"] == ROUTE_VLM, d
    assert "повреждён" in d["reason"]


def test_already_processed_page_is_skipped():
    """Страницу, уже восстановленную моделью, повторно не гоняем."""
    sig = PageSignals.from_text(4, SCAN_TABLE_TEXT, tables_found=0, already_vlm=True)
    assert decide_route(sig)["route"] == ROUTE_SKIP


def test_table_like_ratio_detects_tabular_lines():
    """Число отдельной строкой — признак ячейки таблицы; длинная строка с одним числом — обычный текст."""
    assert table_like_ratio("Блок детектирования\n4\n28\n") > 0.3
    assert table_like_ratio("Блок детектирования Контроль дозы 4 В кожухе") == 0.0
    assert table_like_ratio(DIGITAL_PAGE) < 0.3
    assert table_like_ratio("") == 0.0


def test_garbage_ratio_separates_normal_from_mojibake():
    assert garbage_ratio(DIGITAL_PAGE) < 0.05
    assert garbage_ratio(MOJIBAKE) > 0.3


def test_document_summary_counts_pages_by_route():
    pages = [
        PageSignals.from_text(1, DIGITAL_PAGE, tables_found=1, worst_quality=0.9),
        PageSignals.from_text(2, SCAN_TABLE_TEXT, tables_found=0),
        PageSignals.from_text(3, "короткий", tables_found=0),
    ]
    summary = route_document(pages)
    assert summary["pages"] == 3
    assert summary["by_route"].get(ROUTE_PARSER) == 1
    assert summary["by_route"].get(ROUTE_VLM) == 1
    assert summary["by_route"].get(ROUTE_OCR) == 1
    assert summary["vlm_pages"] == [2]
    assert len(summary["decisions"]) == 3
