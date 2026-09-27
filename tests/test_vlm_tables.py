"""Тесты подключаемой опции «таблицы через VL-модель» и общей схемы распознавания таблиц.

Проверяем три вещи, которые легко сломать незаметно:
  1. выключенная опция НИЧЕГО не вызывает по сети и не считается сбоем;
  2. настройки вызова остаются теми, что подобраны замером (без штрафа за повторы, с ограничением длины,
     со стоп-последовательностями) — иначе вернётся зацикливание или испортится текст;
  3. схема «Occular → VL» правильно выбирает путь и всегда объясняет причину.
"""
from __future__ import annotations

import json
import urllib.error

import pytest

from src.indexing import vlm_tables
from src.indexing.table_recovery import OcrLine
from src.indexing.table_strategy import recover_tables, recognize_occular_tables

TABLE_MD = (
    "| Наименование | Кол | Примечание |\n"
    "|---|---|---|\n"
    "| Блок детектирования | 4 | В защитном кожухе |\n"
    "| Блок детектирования | 28 | В защитном кожухе |\n"
    "| Устройство питания | 12 | |\n"
)


class _FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _enabled_config(**over):
    cfg = dict(vlm_tables.get_vlm_tables_config())
    cfg.update({"enabled": True, "endpoint": "http://models:11434", "model": "kag-qwen2vl:2b"})
    cfg.update(over)
    return cfg


def test_option_is_disabled_by_default():
    """Правило владельца: таких файлов мало, по умолчанию опцию не включаем."""
    assert vlm_tables.get_vlm_tables_config()["enabled"] is False


def test_disabled_option_makes_no_call(monkeypatch):
    def explode(*a, **k):
        raise AssertionError("при выключенной опции сетевой вызов недопустим")

    monkeypatch.setattr(vlm_tables.urllib.request, "urlopen", explode)
    table, reason = vlm_tables.recognize_table(b"image", config={"enabled": False})
    assert table is None
    assert "выключена" in reason


def test_call_params_keep_the_measured_recipe(monkeypatch):
    """Замер 27.09.2026: без штрафа за повторы, num_predict ограничен, есть стоп-последовательности."""
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["payload"] = json.loads(request.data)
        captured["timeout"] = timeout
        return _FakeResponse({"response": TABLE_MD})

    monkeypatch.setattr(vlm_tables.urllib.request, "urlopen", fake_urlopen)
    table, reason = vlm_tables.recognize_table(b"image", config=_enabled_config(num_predict=2048))
    payload = captured["payload"]
    assert payload["options"]["num_predict"] == 2048
    assert "repeat_penalty" not in payload["options"], "штраф за повторы портит текст у этой модели"
    assert payload["stop"] == vlm_tables.STOP_SEQUENCES
    assert payload["options"]["temperature"] == 0
    assert captured["timeout"] == pytest.approx(300.0)
    assert table is not None and table.n_rows == 4


def test_repeated_rows_are_dropped(monkeypatch):
    """Зацикливание модели (91 строка вместо 14) не должно доезжать до табличного слоя."""
    repeated = TABLE_MD + "| Блок детектирования | 4 | В защитном кожухе |\n" * 5
    monkeypatch.setattr(vlm_tables.urllib.request, "urlopen",
                        lambda *a, **k: _FakeResponse({"response": repeated}))
    table, _ = vlm_tables.recognize_table(b"image", config=_enabled_config())
    assert table.n_rows == 4, f"повторы должны быть выброшены, получили {table.n_rows} строк"
    assert any("выброшено повторяющихся строк" in n for n in table.notes)


def test_short_result_is_not_a_table(monkeypatch):
    monkeypatch.setattr(vlm_tables.urllib.request, "urlopen",
                        lambda *a, **k: _FakeResponse({"response": "| только шапка |\n|---|\n"}))
    table, reason = vlm_tables.recognize_table(b"image", config=_enabled_config(min_rows=2))
    assert table is None and "слишком короткая" in reason


def test_service_errors_are_explained_not_raised(monkeypatch):
    def raise_url_error(*a, **k):
        raise urllib.error.URLError("соединение отклонено")

    monkeypatch.setattr(vlm_tables.urllib.request, "urlopen", raise_url_error)
    table, reason = vlm_tables.recognize_table(b"image", config=_enabled_config())
    assert table is None and "недоступен" in reason


def test_status_without_check_reports_option_state():
    status = vlm_tables.vlm_tables_status()
    assert status["reachable"] is None
    assert "опция" in status["detail"]


def test_status_check_reports_model_presence(monkeypatch):
    monkeypatch.setattr(vlm_tables.urllib.request, "urlopen",
                        lambda *a, **k: _FakeResponse({"models": [{"name": "kag-qwen2vl:2b"},
                                                                   {"name": "qwen3-vl:2b"}]}))
    status = vlm_tables.vlm_tables_status(check_service=True)
    assert status["reachable"] is True and status["model_present"] is True


# ── Общая схема ──────────────────────────────────────────────────────────────────────────────

class _FakeRecognizer:
    def __init__(self, tables):
        self._tables = tables

    def __call__(self, array):
        return self._tables


OCCULAR_TABLE = {
    "bbox": (0.0, 0.0, 100.0, 60.0, 0.9),
    "rows": [(0.0, 20.0), (20.0, 40.0), (40.0, 60.0)],
    "cols": [(0.0, 50.0), (50.0, 100.0)],
    "cells": [],
}
LINES = [
    OcrLine("Наименование", (5, 5, 45, 15)),
    OcrLine("Кол", (55, 5, 95, 15)),
    OcrLine("Блок детектирования", (5, 25, 45, 35)),
    OcrLine("4", (55, 25, 65, 35)),
]


def test_strategy_prefers_occular_when_tables_found():
    report = recover_tables(object(), ocr_lines=LINES, recognizer=_FakeRecognizer([OCCULAR_TABLE]))
    assert report["technique"] == "occular-grid"
    assert report["tables"][0].rows[1] == ["Блок детектирования", "4"]
    assert "Occular" in report["reason"]


def test_strategy_skips_vlm_when_option_disabled():
    """Ключевое требование: выключено — страница пропускается, а не «сбой распознавания»."""
    report = recover_tables(object(), recognizer=_FakeRecognizer([]), config={"enabled": False})
    assert report["tables"] == [] and report["technique"] == "none"
    assert "модель зрения выключена" in report["reason"]


def test_strategy_uses_vlm_when_enabled():
    from src.indexing.table_recovery import RecoveredTable, SOURCE_VLM

    def fake_vlm(image, page=0, config=None):
        return RecoveredTable(rows=[["a", "b"], ["1", "2"]], source=SOURCE_VLM, page=page), "распознано"

    report = recover_tables(object(), recognizer=_FakeRecognizer([]), vlm_caller=fake_vlm,
                            config={"enabled": True, "model": "kag-qwen2vl:2b"})
    assert report["technique"] == "vlm" and report["tables"]
    assert "распознано" in report["reason"]


def test_strategy_reports_when_vlm_fails():
    report = recover_tables(object(), recognizer=_FakeRecognizer([]),
                            vlm_caller=lambda image, page=0, config=None: (None, "сервис недоступен"),
                            config={"enabled": True})
    assert report["tables"] == [] and "сервис недоступен" in report["reason"]


def test_occular_unavailable_is_a_reason_not_an_error():
    """Если Occular нет на машине — это причина в отчёте, а не исключение."""
    tables, reason = recognize_occular_tables(b"not-an-image")
    assert tables == []
    assert reason


def test_settings_update_keeps_other_table_keys():
    """Сохранение VL-опции не должно выключать табличный стек: чужие ключи остаются как были."""
    existing = {"enabled": True, "row_vectors": True, "sql_enabled": True, "sql_max_rows": 500}
    cfg = vlm_tables.apply_settings_update(existing, {"enabled": True, "endpoint": "http://x:11434",
                                                     "model": "kag-qwen2vl:2b", "timeout_ms": "120000"})
    assert cfg["enabled"] is True and cfg["row_vectors"] is True and cfg["sql_enabled"] is True
    assert cfg["vlm_tables_enabled"] is True
    assert cfg["vlm_tables_endpoint"] == "http://x:11434"
    assert cfg["vlm_tables_timeout_ms"] == 120000          # строка приведена к числу


def test_settings_update_ignores_empty_changes():
    cfg = vlm_tables.apply_settings_update({"vlm_tables_enabled": True}, {"enabled": None, "model": None})
    assert cfg == {"vlm_tables_enabled": True}
