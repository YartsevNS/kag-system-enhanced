"""Маршрут коллекции при загрузке: явный выбор главнее ссылки на источник.

Инцидент 07.11.2026: 187-ФЗ скачан со страницы мониторинга (regulhub.kaspersky.ru)
и загружен с source_url/source_name в метаданных. Документ ушёл в `news`:
48 фрагментов записались в kag_news, в kag_documents осталась одна карточка,
поиск по корпусу документ не видел.

Причина: `collection_for_source` считает ЛЮБОЙ источник новостью. Теперь загрузчик
может сказать явно `collection=documents`, и это значение пишется в БД словом,
а не пустой строкой (пустое значение снова уходит в эвристику по источнику).
"""
from src.indexing.embeddings_service import collection_for_source, is_news_document

SOURCE = {
    "source_name": "RegulHub (Kaspersky)",
    "source_url": "https://regulhub.kaspersky.ru/upload/iblock/ebe/s6mpl36q8phws8pz53kpdi5tyfyp4gut.pdf",
}


def test_source_alone_routes_to_news():
    """Поведение web_monitor: источник задан, маршрут не выбран → новости."""
    assert collection_for_source(SOURCE) == "news"


def test_explicit_documents_beats_source():
    """Явный выбор основной коллекции перебивает наличие источника."""
    doc = {"collection": "documents", "source_metadata": SOURCE}
    assert is_news_document(doc) is False


def test_explicit_news_is_news():
    assert is_news_document({"collection": "news", "source_metadata": SOURCE}) is True


def test_empty_route_falls_back_to_source():
    """Пустое значение — не выбор: маршрут снова определяется источником."""
    assert is_news_document({"collection": "", "source_metadata": SOURCE}) is True


def test_document_without_source_stays_in_corpus():
    assert is_news_document({"collection": None, "source_metadata": None}) is False
