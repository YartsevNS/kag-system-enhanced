"""Тесты распознавания скана через службу OCR (парсер) — и без неё.

Проверяем то, от чего зависит «уберем ли мы Occular из образа»:
  1. со включённой службой текст страницы приходит от неё, порядок чтения применяется;
  2. выключенная служба не делает сетевых вызовов и уходит на прежний движок;
  3. `parse_ocular_only` работает, даже если самого Occular в образе нет, — но только когда служба жива.
"""
from __future__ import annotations

import json

import pytest

from src.indexing import ocr_client
from src.indexing.hybrid_parser import HybridDocumentParser


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
    """Заглушка службы: отвечает по адресу запроса (адрес у Request — в full_url)."""
    def fake(url, *a, **k):
        target = getattr(url, "full_url", None) or str(url)
        for key, payload in routes.items():
            if key in target:
                return _FakeResponse(payload)
        raise AssertionError(f"неожиданный запрос к службе: {target}")

    return fake


def _parser() -> HybridDocumentParser:
    """Парсер без инициализации движков: тяжёлые модели в тестах не нужны."""
    parser = HybridDocumentParser.__new__(HybridDocumentParser)
    parser._dpi = 200
    parser._ocular = None
    parser._ocular_available = False
    parser._force_ocr = False
    parser._deskew = True
    parser._reading_order = True
    return parser


def _config(enabled=True):
    return {"enabled": enabled, "url": "http://models:8020", "timeout_s": 120,
            "text_lang": "cyrillic", "cells_lang": "eslav"}


@pytest.fixture(autouse=True)
def _clean_probe_cache(monkeypatch):
    """Локальный движок в тестах гасим: иначе он полез бы качать модели на тестовой машине."""
    ocr_client.reset_probe()
    monkeypatch.setattr(ocr_client, "local_available", lambda: False)
    monkeypatch.setattr(ocr_client, "lines_local", lambda image: [])
    yield
    ocr_client.reset_probe()


def test_pages_text_from_service_returns_text_and_applies_reading_order(monkeypatch, tmp_path):
    """Строки службы приходят в порядке распознавания — в документ они должны попасть по геометрии."""
    image = tmp_path / "scan.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")          # содержимое не важно: служба замокана
    # Две строки: нижняя приходит первой (как из распознавания), верхняя — второй.
    payload = {"lines": [
        {"text": "нижняя строка", "quad": [[10, 100], [200, 100], [200, 120], [10, 120]], "confidence": 0.9},
        {"text": "верхняя строка", "quad": [[10, 10], [200, 10], [200, 30], [10, 30]], "confidence": 0.9},
    ]}
    monkeypatch.setattr(ocr_client, "get_service_config", lambda: _config())
    monkeypatch.setattr(ocr_client.urllib.request, "urlopen", _router({
        "/health": {"status": "ok"},
        "/ocr": payload,
    }))

    pages = _parser()._pages_text_from_service(str(image))
    assert pages is not None and len(pages) == 1
    assert "верхняя строка" in pages[0] and "нижняя строка" in pages[0]
    assert pages[0].index("верхняя строка") < pages[0].index("нижняя строка")


def test_pages_text_from_service_none_when_disabled(monkeypatch, tmp_path):
    def explode(*a, **k):
        raise AssertionError("при выключенной службе сетевой вызов недопустим")

    image = tmp_path / "scan.png"
    image.write_bytes(b"data")
    monkeypatch.setattr(ocr_client, "get_service_config", lambda: _config(enabled=False))
    monkeypatch.setattr(ocr_client.urllib.request, "urlopen", explode)

    assert _parser()._pages_text_from_service(str(image)) is None


def test_parse_ocular_only_without_occular_when_service_disabled(monkeypatch, tmp_path):
    """Без движка вообще документ не разбирается — прежнее поведение сохранено."""
    image = tmp_path / "scan.png"
    image.write_bytes(b"data")
    monkeypatch.setattr(ocr_client, "get_service_config", lambda: _config(enabled=False))
    assert _parser().parse_ocular_only(str(image)) is None


def test_parse_ocular_only_works_with_service_and_no_occular(monkeypatch, tmp_path):
    """Ключевое: при живом службе сканы разбираются даже после удаления весов Occular из образа."""
    image = tmp_path / "scan.png"
    image.write_bytes(b"data")
    payload = {"lines": [{"text": "Счет-фактура №", "quad": [[10, 10], [200, 10], [200, 30], [10, 30]],
                          "confidence": 0.9}]}
    monkeypatch.setattr(ocr_client, "get_service_config", lambda: _config())
    monkeypatch.setattr(ocr_client.urllib.request, "urlopen", _router({
        "/health": {"status": "ok"},
        "/ocr": payload,
    }))

    parsed = _parser().parse_ocular_only(str(image))
    assert parsed is not None
    assert "Счет-фактура" in parsed.full_text
    assert parsed.metadata.get("ocr_engine") == "service-ppocrv5"
