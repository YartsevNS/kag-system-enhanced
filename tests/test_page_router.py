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
    sig = PageSignals.from_text(3, SCAN_TABLE_TEXT, tables_found=0, doc_is_scan=True)
    assert sig.text_chars >= 200            # текст есть, но он от OCR
    assert sig.table_like > 0.3 and sig.table_like_lines >= 3
    d = decide_route(sig)
    assert d["route"] == ROUTE_VLM, d
    assert "таблиц" in d["reason"]


def test_scan_without_tables_goes_to_our_ocr():
    """Скан без признаков таблицы — модель не нужна, хватит нашего OCR."""
    sig = PageSignals.from_text(2, "короткий текст", tables_found=0, doc_is_scan=True)
    d = decide_route(sig)
    assert d["route"] == ROUTE_OCR, d
    assert "без признаков" in d["reason"]


def test_low_quality_table_in_scan_goes_to_model():
    """Скан с плохо разобранной таблицей — восстанавливаем моделью."""
    sig = PageSignals.from_text(5, SCAN_TABLE_TEXT, tables_found=1, worst_quality=0.41,
                                doc_tables_count=1, doc_tables_quality=0.41, doc_is_scan=True)
    assert decide_route(sig)["route"] == ROUTE_VLM


def test_low_quality_table_in_digital_pdf_is_not_sent_to_model():
    """У цифрового PDF низкое качество разметки — НЕ повод гнать страницу в модель.

    Прогон по корпусу показал: если считать это поводом, в модель уходит 35% фрагментов (3455 из 9931),
    потому что большинство «таблиц» в цифровых документах — шум, который нам не нужен.
    """
    sig = PageSignals.from_text(5, DIGITAL_PAGE, tables_found=1, worst_quality=0.41,
                                doc_tables_count=3, doc_tables_quality=0.41)
    assert decide_route(sig)["route"] == ROUTE_PARSER


def test_damaged_text_layer_goes_to_ocr_not_to_model():
    """Побитая кодировка лечится повторным распознаванием, а не табличной моделью.

    Реальный случай 2026-09-14: у пяти документов текст был записан неверной кодировкой — проблема
    в тексте, а не в структуре таблицы, поэтому маршрут «наш OCR», не VLM.
    """
    sig = PageSignals.from_text(7, MOJIBAKE, tables_found=0, doc_tables_count=0)
    assert sig.garbage > 0.05, sig.garbage
    d = decide_route(sig)
    assert d["route"] == ROUTE_OCR, d
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
        PageSignals.from_text(2, SCAN_TABLE_TEXT, tables_found=0, doc_is_scan=True),
        PageSignals.from_text(3, "короткий", tables_found=0),
    ]
    summary = route_document(pages)
    assert summary["pages"] == 3
    assert summary["by_route"].get(ROUTE_PARSER) == 1
    assert summary["by_route"].get(ROUTE_VLM) == 1
    assert summary["by_route"].get(ROUTE_OCR) == 1
    assert summary["vlm_pages"] == [2]
    assert len(summary["decisions"]) == 3


def test_document_with_parsed_tables_skips_model_for_all_pages():
    """Если у документа таблицы разобраны с приемлемым качеством — модель не нужна ни одной странице.

    Это правило появилось после прогона по корпусу: без него в модель уходило 22% фрагментов (2206
    из 9931), потому что короткие числовые строки есть почти в любом документе (колонтитулы, номера
    страниц, списки). Сигнал уровня документа убирает основную массу таких срабатываний.
    """
    sig = PageSignals.from_text(1, SCAN_TABLE_TEXT, tables_found=0,
                                doc_tables_count=5, doc_tables_quality=0.85)
    d = decide_route(sig)
    assert d["route"] == ROUTE_PARSER, d
    assert "модель не нужна" in d["reason"]


def test_scan_page_with_table_goes_to_model_even_if_doc_has_poor_tables():
    """Скан с таблицей: маршрут в модель (документные таблицы при этом разобраны плохо)."""
    sig = PageSignals.from_text(1, SCAN_TABLE_TEXT, tables_found=0, worst_quality=0.35,
                                doc_tables_count=1, doc_tables_quality=0.35, doc_is_scan=True)
    d = decide_route(sig)
    assert d["route"] == ROUTE_VLM, d
    assert "таблиц" in d["reason"]


def test_few_numeric_lines_are_not_a_table():
    """Три числовые строки на страницу — это колонтитулы и номера, а не таблица."""
    text = ("1\n" + "Обычный абзац документа с текстом и объяснением требований.\n" * 8 + "2\n3\n")
    sig = PageSignals.from_text(1, text)
    assert sig.table_like_lines == 3
    assert decide_route(sig)["route"] == ROUTE_PARSER, decide_route(sig)
