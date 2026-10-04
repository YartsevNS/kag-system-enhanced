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
    cfg.update({"enabled": True, "endpoint": "http://models:11434", "model": "kag-qwen2vl:2b",
                "format": "markdown"})   # тесты разбора markdown: формат задаём явно
    cfg.update(over)
    return cfg


def test_option_is_disabled_by_default():
    """Правило владельца: таких файлов мало, по умолчанию опцию не включаем."""
    assert vlm_tables.get_vlm_tables_config()["enabled"] is False


def test_looks_like_table_distinguishes_screenshot_from_invoice():
    """Признак «похоже на таблицу»: у скриншота выдачи чисел-значений нет, у накладной — есть."""
    screenshot = [{"text": t} for t in ["Режим ИИ", "Все", "Видео", "Картинки", "Покупки", "Новости", "Ещё"]]
    invoice = [{"text": t} for t in [
        "531299000202 | 1 | Балка MS Pro 120 | 796 | шт | 12 | 560,18 | 6722.10",
        "531299060402 | 2 | Балка MS Pro 150 | 716 | шт | 22 | 634.56 | 13 959,92",
        "831296060602 | 3 | Балка MS Pro 100 | 7316 | шт | 62 | 730,73 | 45 305,47",
        "531299000212 | 4 | Балка MS Pro 90 | 786 | шт | 12 | 474,49 | 5693.90",
        "531299000222 | 5 | Балка MS Pro 60 | 796 | шт | 14 | 380,10 | 5321.40",
        "Итого",
    ]]
    assert vlm_tables.looks_like_table(screenshot) is False
    assert vlm_tables.looks_like_table(invoice) is True
    assert vlm_tables.looks_like_table([]) is False


def test_vlm_not_called_when_page_is_not_a_table(monkeypatch):
    """Главное: на странице без признаков таблицы модель зрения НЕ вызывается (иначе 72 с впустую)."""
    from src.indexing.table_strategy import recover_tables

    calls = []

    def fake_caller(image, page=0, config=None):
        calls.append(1)
        return None, "модель зрения вызвана"

    cfg = dict(vlm_tables.get_vlm_tables_config())
    cfg["enabled"] = True
    lines = [{"text": t} for t in ["Режим ИИ", "Все", "Видео", "Картинки", "Покупки", "Новости"]]
    report = recover_tables(b"not-an-image", config=cfg, vlm_caller=fake_caller, raw_lines=lines)

    assert not calls, "модель зрения не должна вызываться на странице без признаков таблицы"
    assert "не похожа на таблицу" in report["reason"]


def test_vlm_called_when_page_looks_like_table(monkeypatch):
    """Обратная сторона: если числа в строках есть, предохранитель модель не блокирует."""
    from src.indexing.table_strategy import recover_tables

    calls = []

    def fake_caller(image, page=0, config=None):
        calls.append(1)
        return None, "модель зрения вызвана (таблица не распознана)"

    cfg = dict(vlm_tables.get_vlm_tables_config())
    cfg["enabled"] = True
    lines = [{"text": "531299000202 1 Балка 796 шт 12 560,18 6722.10"},
             {"text": "531299060402 2 Балка 716 шт 22 634.56 13 959,92"},
             {"text": "831296060602 3 Балка 7316 шт 62 730,73 45 305,47"},
             {"text": "531299000212 4 Балка 786 шт 12 474,49 5693.90"},
             {"text": "531299000222 5 Балка 796 шт 14 380,10 5321.40"},
             {"text": "Итого"}]
    recover_tables(b"not-an-image", config=cfg, vlm_caller=fake_caller, raw_lines=lines)

    assert calls, "при признаках таблицы модель зрения должна вызываться"


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


class _FakePage:
    def __init__(self, page_num=1):
        self.page_num = page_num
        self.tables = []


class _FakeParsed:
    def __init__(self):
        self.pages = [_FakePage()]


def test_recovered_table_is_written_into_document_parse(tmp_path):
    """Восстановленные таблицы кладём в разбор документа: дальше их сохраняет штатный путь."""
    from src.indexing import table_strategy
    from src.indexing.table_recovery import RecoveredTable

    image = tmp_path / "page.png"
    image.write_bytes(b"PNG")            # содержимое неважно: схему подменяем ниже
    parsed = _FakeParsed()
    table = RecoveredTable(rows=[["Наименование", "Кол"], ["Блок", "4"]],
                           source="vlm", source_model="kag-qwen2vl:2b")

    orig = table_strategy.recover_tables
    table_strategy.recover_tables = lambda image, **kwargs: {
        "tables": [table], "technique": "vlm", "reason": "распознано", "seconds": 1.0}
    try:
        report = table_strategy.recover_tables_for_image_document(parsed, str(image), config={"enabled": True})
    finally:
        table_strategy.recover_tables = orig

    assert report["applied"] is True and report["saved"] == 1
    assert parsed.pages[0].tables, "таблица должна попасть в разбор документа"
    saved = parsed.pages[0].tables[0]
    assert saved["extraction_method"] == "vlm-kag-qwen2vl:2b"
    assert saved["headers"] == ["Наименование", "Кол"]
    assert saved["rows"] == [["Наименование", "Кол"], ["Блок", "4"]]
    assert saved["markdown"].startswith("| Наименование")
    assert "<th>Наименование</th>" in saved["html"]


def test_non_image_document_is_skipped(tmp_path):
    from src.indexing.table_strategy import recover_tables_for_image_document

    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    report = recover_tables_for_image_document(_FakeParsed(), str(pdf))
    assert report["applied"] is False and "не картинка" in report["reason"]


def test_openai_protocol_payload_and_parsing(monkeypatch):
    """OpenAI-совместимый путь: llama.cpp server, vLLM, внешние VL-API говорят именно так."""
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["payload"] = json.loads(request.data)
        return _FakeResponse({"choices": [{"message": {"content": TABLE_MD}}]})

    monkeypatch.setattr(vlm_tables.urllib.request, "urlopen", fake_urlopen)
    cfg = _enabled_config(api="openai", endpoint="http://models:8081")
    table, reason = vlm_tables.recognize_table(b"image", config=cfg)
    assert captured["url"] == "http://models:8081/v1/chat/completions"
    content = captured["payload"]["messages"][0]["content"]
    assert content[0]["type"] == "text" and content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert captured["payload"]["max_tokens"] == cfg["num_predict"]
    assert table is not None and table.n_rows == 4


def test_openai_status_probe_uses_models_endpoint(monkeypatch):
    captured = {}

    def fake_urlopen(url, timeout=None):
        captured["url"] = url
        return _FakeResponse({"data": [{"id": "kag-qwen2vl:2b"}]})

    monkeypatch.setattr(vlm_tables.urllib.request, "urlopen", fake_urlopen)
    status = vlm_tables.vlm_tables_status(check_service=True)
    assert status["api"] == "ollama"                     # по умолчанию
    assert "/api/tags" in captured["url"]
