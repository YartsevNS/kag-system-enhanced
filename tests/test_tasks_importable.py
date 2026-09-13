"""Страховка: модуль Celery-задач обязан импортироваться.

Живой случай 2026-09-13: в tasks.py осталась синтаксическая ошибка (запятая перед `for`
в dict comprehension). API при этом работал и отвечал 200 на /reindex-all, но постановка
документов падала с «invalid syntax (tasks.py, line 517)» — в ответе было «Поставлено 0 из 61»,
а разделы и крошки в графе не появились. Такое ловится одним импортом модуля.
"""
import importlib


def test_модуль_задач_импортируется():
    m = importlib.import_module("src.indexing.tasks")
    assert hasattr(m, "process_document"), "задача обработки документа не найдена"
    assert hasattr(m, "rebuild_graph_task"), "задача перестроения графа не найдена"


def test_модули_конвейера_импортируются():
    """Те же грабли в других модулях конвейера ловятся так же дёшево."""
    for name in (
        "src.api.services.document_service",
        "src.api.services.document_analyzer",
        "src.indexing.section_parser",
        "src.indexing.document_card",
        "src.indexing.entity_selfref",
        "src.indexing.knowledge_graph",
        "src.indexing.embeddings_service",
        "src.indexing.queue_guard",
    ):
        importlib.import_module(name)
