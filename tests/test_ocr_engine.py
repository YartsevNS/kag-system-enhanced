"""Тесты выбора движка инференса распознавания (OpenVINO против CPU).

Проверяем правила: без настройки — CPU; настройка без провайдера — тихий откат; с провайдером —
однократная подмена и идемпотентность; настройки недоступны — как раньше.
"""
from __future__ import annotations

import sys
import types

import pytest

from src.indexing import ocr_engine


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(ocr_engine, "_patched", False)
    yield
    monkeypatch.setattr(ocr_engine, "_patched", False)


def test_default_is_cpu(monkeypatch):
    monkeypatch.setattr(ocr_engine, "engine_setting", lambda: "cpu")
    monkeypatch.setattr(ocr_engine, "openvino_available", lambda: True)
    assert ocr_engine.enable_if_configured() is False
    assert ocr_engine.is_active() is False


def test_setting_without_provider_falls_back(monkeypatch):
    monkeypatch.setattr(ocr_engine, "engine_setting", lambda: "openvino")
    monkeypatch.setattr(ocr_engine, "openvino_available", lambda: False)
    assert ocr_engine.enable_if_configured() is False
    assert ocr_engine.is_active() is False


def test_patch_applied_once_and_idempotent(monkeypatch):
    calls: list[str] = []

    class FakeProviderConfig:
        def get_ep_list(self):
            calls.append("orig")
            return [("CPUExecutionProvider", {})]

    # подсовываем поддельный модуль rapidocr с ProviderConfig
    fake_pc = types.ModuleType("rapidocr.inference_engine.onnxruntime.provider_config")
    fake_pc.ProviderConfig = FakeProviderConfig
    fake_ort = types.ModuleType("rapidocr.inference_engine.onnxruntime")
    fake_ort.provider_config = fake_pc
    fake_rapid = types.ModuleType("rapidocr.inference_engine")
    fake_rapid.onnxruntime = fake_ort
    for name, mod in (
        ("rapidocr", types.ModuleType("rapidocr")),
        ("rapidocr.inference_engine", fake_rapid),
        ("rapidocr.inference_engine.onnxruntime", fake_ort),
        ("rapidocr.inference_engine.onnxruntime.provider_config", fake_pc),
    ):
        monkeypatch.setitem(sys.modules, name, mod)

    monkeypatch.setattr(ocr_engine, "engine_setting", lambda: "openvino")
    monkeypatch.setattr(ocr_engine, "openvino_available", lambda: True)

    assert ocr_engine.enable_if_configured() is True
    assert ocr_engine.is_active() is True

    providers = FakeProviderConfig().get_ep_list()
    assert providers[0][0] == "OpenVINOExecutionProvider"
    assert providers[0][1] == {"device_type": "CPU"}
    assert providers[-1][0] == "CPUExecutionProvider"  # откат на CPU остаётся в списке
    assert calls == ["orig"]

    assert ocr_engine.enable_if_configured() is True  # повторно — без новой подмены
    assert calls == ["orig"]


def test_status_reports_setting_and_availability(monkeypatch):
    monkeypatch.setattr(ocr_engine, "engine_setting", lambda: "auto")
    monkeypatch.setattr(ocr_engine, "openvino_available", lambda: True)
    st = ocr_engine.status()
    assert st == {"setting": "auto", "provider_available": True, "active": False}


def test_setting_defaults_to_cpu_when_store_unavailable(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name.startswith("src.api.services.config_store"):
            raise RuntimeError("нет настроек")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert ocr_engine.engine_setting() == "cpu"
