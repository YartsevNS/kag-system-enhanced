"""Галочка «не строить граф при обработке» и путь «построить позже».

Зачем: граф — самая дорогая часть обработки. Замер 2026-09-13 на документе
«перечень документов по КИИ 2.pdf» (13 чанков): всего 147 с, из них 135.7 с — граф
(13 LLM-извлечений по чанкам, ~1585 векторов сущностей, дедупликация 45 пар),
а разбор PDF + векторы документа — меньше 12 с. Поэтому админ может отключить граф
и построить его позже (кнопка у документа, «Построить граф сейчас» в админке,
«Перестроить граф» на /kg).
"""
from pathlib import Path

from src.indexing import processing_guard

ROOT = Path(__file__).resolve().parents[1]


def test_skip_when_configured(monkeypatch):
    monkeypatch.setattr(processing_guard, "_load_graph_cfg",
                        lambda: {"skip": True, "message": "построю позже"})
    assert processing_guard.graph_skip_requested() == (True, "построю позже")


def test_not_skip_by_default(monkeypatch):
    monkeypatch.setattr(processing_guard, "_load_graph_cfg", lambda: {"skip": False})
    assert processing_guard.graph_skip_requested() == (False, "")


def test_missing_config_does_not_skip(monkeypatch):
    monkeypatch.setattr(processing_guard, "_load_graph_cfg", lambda: None)
    assert processing_guard.graph_skip_requested() == (False, "")


def test_garbage_config_does_not_skip(monkeypatch):
    monkeypatch.setattr(processing_guard, "_load_graph_cfg", lambda: "мусор")
    assert processing_guard.graph_skip_requested() == (False, "")


def test_read_failure_is_fail_open(monkeypatch):
    """Сбой чтения настройки не меняет поведение: граф строится как обычно.

    Обратный выбор (fail-closed) тихо лишал бы документы графа при любой ошибке БД,
    а граф потом никто не пересоберёт — поэтому здесь именно fail-open.
    """
    def boom():
        raise RuntimeError("БД недоступна")

    monkeypatch.setattr(processing_guard, "_load_graph_cfg", boom)
    assert processing_guard.graph_skip_requested() == (False, "")


def test_pipeline_checks_flag_before_building_graph():
    """Структурная проверка: флаг читается ДО вызова построения графа.

    Иначе галочка была бы декоративной: граф всё равно строился бы.
    """
    src = (ROOT / "src/api/services/document_service.py").read_text(encoding="utf-8")
    i_flag = src.index("graph_skip_requested()")
    i_build = src.index("await self._build_knowledge_graph_async(")
    assert i_flag < i_build, "флаг должен проверяться до построения графа"
    assert "graph_skipped" in src, "пропуск должен попадать в журнал обработки"
