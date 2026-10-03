"""Тесты движка OCR: служба PP-OCRv5 как сменный источник строк и распознаватель ячеек.

Проверяем то, что легко сломать незаметно и что дорого стоит на проде:
  1. выключенная опция ничего не вызывает по сети — конвейер обязан работать ровно как раньше;
  2. недоступная служба — это откат на прежний движок с причиной, а не потеря страницы;
  3. формат строк службы (`text`/`quad`/`confidence`/`raw_scale`) читается так же, как у Occular,
     иначе ячейки таблицы разъедутся по масштабу.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from src.indexing import ocr_client, table_strategy


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _router(routes):
    """Заглушка службы: отвечает по адресу запроса — иначе проверка доступности уходила бы на /ocr.

    Адрес берём из Request.full_url: `str(request)` у объекта Request — это не адрес, и по нему
    маршрут не находится (на этом уже спотыкались).
    """
    def fake(url, *a, **k):
        target = getattr(url, "full_url", None) or str(url)
        for key, payload in routes.items():
            if key in target:
                return _FakeResponse(payload)
        raise AssertionError(f"неожиданный запрос к службе: {target}")

    return fake


def _config(enabled=True, url="http://models:8020", timeout_s=120):
    return {"enabled": enabled, "url": url, "timeout_s": timeout_s,
            "text_lang": "cyrillic", "cells_lang": "eslav"}


@pytest.fixture(autouse=True)
def _clean_probe_cache(monkeypatch):
    """Кэш доступности — глобальный; между тестами его надо сбрасывать, иначе тесты влияют друг на друга.

    Локальный движок гасим: в тестовой среде он либо отсутствует, либо полез бы качать модели.
    """
    ocr_client.reset_probe()
    monkeypatch.setattr(ocr_client, "local_available", lambda: False)
    monkeypatch.setattr(ocr_client, "lines_local", lambda image: [])
    yield
    ocr_client.reset_probe()


def test_service_is_disabled_by_default():
    """По умолчанию распознаёт прежний движок: включение службы — осознанное действие в админке."""
    assert ocr_client.get_service_config()["enabled"] is False


def test_disabled_service_makes_no_network_call(monkeypatch):
    def explode(*a, **k):
        raise AssertionError("при выключенной службе сетевой вызов недопустим")

    monkeypatch.setattr(ocr_client, "get_service_config", lambda: _config(enabled=False))
    monkeypatch.setattr(ocr_client.urllib.request, "urlopen", explode)

    assert ocr_client.service_available() is False
    assert ocr_client.lines_from_service(b"image") == []


def test_lines_from_service_parses_format(monkeypatch):
    payload = {"engine": "rapidocr-ppocrv5-cyrillic", "seconds": 1.2, "lines": [
        {"text": "Счет-фактура №", "quad": [[10, 20], [90, 20], [90, 34], [10, 34]],
         "confidence": 0.89, "raw_scale": 1.0},
        {"text": "   ", "quad": [[0, 0], [1, 0], [1, 1], [0, 1]], "confidence": 0.1},  # пустой — пропускаем
    ]}
    monkeypatch.setattr(ocr_client, "get_service_config", lambda: _config())
    monkeypatch.setattr(ocr_client.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(payload))

    lines = ocr_client.lines_from_service(b"image")
    assert len(lines) == 1
    assert lines[0]["text"] == "Счет-фактура №"
    assert lines[0]["confidence"] == pytest.approx(0.89)
    assert lines[0]["raw_scale"] == 1.0
    assert len(lines[0]["quad"]) == 4


def test_unreachable_service_returns_empty_and_is_cached(monkeypatch):
    calls = {"n": 0}

    def failing(*a, **k):
        calls["n"] += 1
        raise OSError("connection refused")

    monkeypatch.setattr(ocr_client, "get_service_config", lambda: _config())
    monkeypatch.setattr(ocr_client.urllib.request, "urlopen", failing)

    assert ocr_client.service_available() is False
    assert ocr_client.service_available() is False          # кэш: недоступный сервер не тормозит каждую страницу
    assert calls["n"] == 1


def test_cells_from_service_returns_texts(monkeypatch):
    payload = {"engine": "rapidocr-ppocrv5-cyrillic", "seconds": 0.4, "texts": [
        {"text": "796", "confidence": 0.93}, {"text": "шт", "confidence": 0.8}]}
    monkeypatch.setattr(ocr_client, "get_service_config", lambda: _config())
    monkeypatch.setattr(ocr_client.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(payload))

    image = np.zeros((10, 10, 3), dtype=np.uint8)
    quads = [np.array([[0, 0], [5, 0], [5, 5], [0, 5]], dtype=float)] * 2
    got = ocr_client.cells_from_service(image, quads)
    assert [t for t, _ in got] == ["796", "шт"]
    assert got[0][1] == pytest.approx(0.93)


def test_engine_path_uses_service_when_enabled(monkeypatch):
    """Включённая и доступная служба — строки берутся у неё, прежний движок не вызывается."""
    monkeypatch.setattr(ocr_client, "get_service_config", lambda: _config())
    monkeypatch.setattr(ocr_client.urllib.request, "urlopen", _router({
        "/health": {"status": "ok", "engine": "rapidocr-ppocrv5-cyrillic"},
        "/ocr": {"lines": [{"text": "строка службы", "quad": [[0, 0], [5, 0], [5, 5], [0, 5]],
                            "confidence": 0.9}]},
    }))

    def should_not_be_called(image):
        raise AssertionError("при работающей службе прежний движок вызываться не должен")

    monkeypatch.setattr(table_strategy, "_raw_lines_from_occular", should_not_be_called)
    lines = table_strategy.raw_lines_from_engine(b"image")
    assert [l["text"] for l in lines] == ["строка службы"]


def test_engine_path_falls_back_when_service_disabled(monkeypatch):
    """Выключенная служба — работаем прежним движком (поведение до перехода сохраняется)."""
    monkeypatch.setattr(ocr_client, "get_service_config", lambda: _config(enabled=False))
    monkeypatch.setattr(table_strategy, "_raw_lines_from_occular",
                        lambda image: [{"text": "строка прежнего движка",
                                        "quad": [[0, 0], [5, 0], [5, 5], [0, 5]], "confidence": 0.5}])
    lines = table_strategy.raw_lines_from_engine(b"image")
    assert [l["text"] for l in lines] == ["строка прежнего движка"]


def test_engine_path_falls_back_when_service_returns_nothing(monkeypatch):
    """Служба ответила пусто — страница не теряется: берём прежний движок."""
    monkeypatch.setattr(ocr_client, "get_service_config", lambda: _config())
    monkeypatch.setattr(ocr_client.urllib.request, "urlopen", lambda *a, **k: _FakeResponse({"lines": []}))
    monkeypatch.setattr(table_strategy, "_raw_lines_from_occular",
                        lambda image: [{"text": "прежний движок", "quad": [[0, 0], [5, 0], [5, 5], [0, 5]],
                                        "confidence": 0.5}])
    lines = table_strategy.raw_lines_from_engine(b"image")
    assert [l["text"] for l in lines] == ["прежний движок"]


def test_cell_recognizer_uses_service_when_enabled(monkeypatch):
    """Распознаватель ячеек — тоже сменный: со включённой службой он ходит в неё, а не в Occular."""
    monkeypatch.setattr(ocr_client, "get_service_config", lambda: _config())
    monkeypatch.setattr(ocr_client.urllib.request, "urlopen", _router({
        "/health": {"status": "ok"},
        "/cells": {"texts": [{"text": "13 959,9", "confidence": 0.9}]},
    }))
    monkeypatch.setattr(table_strategy, "_cell_recognizer", None)
    monkeypatch.setattr(table_strategy, "_cell_recognizer_failed", False)

    recognizer = table_strategy.make_cell_recognizer()
    assert recognizer is not None
    image = np.zeros((10, 10, 3), dtype=np.uint8)
    got = recognizer(image, [np.array([[0, 0], [5, 0], [5, 5], [0, 5]], dtype=float)])
    assert got[0][0] == "13 959,9"


def test_status_without_check_makes_no_call(monkeypatch):
    def explode(*a, **k):
        raise AssertionError("без check служба не опрашивается")

    monkeypatch.setattr(ocr_client, "get_service_config", lambda: _config(enabled=False))
    monkeypatch.setattr(ocr_client.urllib.request, "urlopen", explode)
    status = ocr_client.ocr_service_status(check_service=False)
    assert status["enabled"] is False
    assert "локальный движок" in status["detail"]


def test_default_languages_are_split():
    """По умолчанию текст и ячейки идут разными языками — из замера на накладной (см. ocr_client)."""
    assert ocr_client.DEFAULT_TEXT_LANG == "cyrillic"
    assert ocr_client.DEFAULT_CELLS_LANG == "eslav"
    assert ocr_client.LANGUAGES == ("cyrillic", "eslav")


def test_text_requests_use_text_language(monkeypatch):
    """Запрос строк страницы уходит с языком текста: иначе настройка в админке ни на что не влияет."""
    seen = {}

    def fake(url, *a, **k):
        seen["url"] = getattr(url, "full_url", str(url))
        return _FakeResponse({"lines": [{"text": "строка", "quad": [[0, 0], [1, 0], [1, 1], [0, 1]],
                                         "confidence": 0.9}]})

    monkeypatch.setattr(ocr_client, "get_service_config",
                        lambda: {**_config(), "text_lang": "eslav", "cells_lang": "cyrillic"})
    monkeypatch.setattr(ocr_client.urllib.request, "urlopen", fake)
    ocr_client.lines_from_service(b"image")
    assert "lang=eslav" in seen["url"]


def test_cells_requests_carry_cells_language(monkeypatch):
    """Запрос вырезок несёт язык ячеек в теле — служба выбирает модель по нему."""
    seen = {}

    def fake(request, *a, **k):
        seen["body"] = json.loads(request.data.decode())
        return _FakeResponse({"texts": [{"text": "13 959,9", "confidence": 0.9}]})

    monkeypatch.setattr(ocr_client, "get_service_config",
                        lambda: {**_config(), "text_lang": "cyrillic", "cells_lang": "eslav"})
    monkeypatch.setattr(ocr_client.urllib.request, "urlopen", fake)

    image = np.zeros((10, 10, 3), dtype=np.uint8)
    quads = [np.array([[0, 0], [5, 0], [5, 5], [0, 5]], dtype=float)]
    ocr_client.cells_from_service(image, quads)
    assert seen["body"]["lang"] == "eslav"
