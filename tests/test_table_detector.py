"""Тесты этапа «детектор области таблицы»: контракт модели, откат и кроп.

Модель ONNX в тестах не нужна: сессия подменяется заглушкой с теми же выходами (logits, pred_boxes),
поэтому проверяются именно наши правила — фильтр класса, порог, пересчёт координат, отсутствие модели.
"""
from __future__ import annotations

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from src.indexing import table_detector  # noqa: E402


class _FakeSession:
    def __init__(self, logits: np.ndarray, boxes: np.ndarray):
        self.logits, self.boxes = logits, boxes

    def run(self, names, feeds):  # noqa: ANN001, ARG002
        return [self.logits, self.boxes]


def _logits(rows):
    """logits [1,300,25]: у строки rows класс 21 «горит», остальные нули (sigmoid(0)=0.5)."""
    arr = np.zeros((1, 300, 25), np.float32)
    for i, (cls, value) in enumerate(rows):
        arr[0, i, cls] = value
    return arr


def _boxes(entries):
    arr = np.zeros((1, 300, 4), np.float32)
    for i, box in enumerate(entries):
        arr[0, i] = box
    return arr


def _cfg(path: str = "", enabled: bool = True, conf: float = 0.5, pad: int = 8) -> dict:
    return {"enabled": enabled, "path": path, "conf": conf, "pad": pad}


def _page(w: int = 1000, h: int = 1400) -> np.ndarray:
    return np.full((h, w, 3), 255, np.uint8)


def test_disabled_by_default(monkeypatch):
    monkeypatch.setattr(table_detector, "detector_config", lambda: _cfg(enabled=False))
    assert table_detector.detector_available() is False
    assert table_detector.detect_tables(_page()) is None


def test_enabled_without_model_file(monkeypatch, tmp_path):
    missing = str(tmp_path / "нет-такой.onnx")
    monkeypatch.setattr(table_detector, "detector_config", lambda: _cfg(path=missing))
    assert table_detector.detector_available() is False
    assert table_detector.detect_tables(_page()) is None


def test_box_is_converted_to_pixels_and_filtered(monkeypatch, tmp_path):
    model = tmp_path / "det.onnx"
    model.write_bytes(b"not-a-real-model")
    monkeypatch.setattr(table_detector, "detector_config", lambda: _cfg(path=str(model)))
    # строка 0 — таблица (класс 21, значение 3.0 → sigmoid ≈ 0.95), строка 1 — другой класс,
    # строка 2 — таблица, но слишком мелкая (нормализованные 0.01×0.01 ≈ 10×14 px)
    session = _FakeSession(
        _logits([(21, 3.0), (5, 4.0), (21, 3.0)]),
        _boxes([(0.5, 0.5, 0.5, 0.2), (0.3, 0.3, 0.2, 0.2), (0.5, 0.5, 0.01, 0.01)]),
    )
    monkeypatch.setattr(table_detector, "_session", lambda path: session)

    found = table_detector.detect_tables(_page())
    assert found is not None and len(found) == 1  # не-таблица отсеяна, мелкая отсеяна
    box = found[0]["bbox"]
    assert box == pytest.approx((250.0, 560.0, 750.0, 840.0), abs=1.0)
    assert found[0]["score"] == pytest.approx(0.95, abs=0.01)


def test_conf_threshold_filters_low_scores(monkeypatch, tmp_path):
    model = tmp_path / "det.onnx"
    model.write_bytes(b"x")
    monkeypatch.setattr(table_detector, "detector_config", lambda: _cfg(path=str(model), conf=0.9))
    session = _FakeSession(_logits([(21, 1.0)]), _boxes([(0.5, 0.5, 0.5, 0.2)]))  # sigmoid(1)=0.73 < 0.9
    monkeypatch.setattr(table_detector, "_session", lambda path: session)
    assert table_detector.detect_tables(_page()) == []


def test_crop_table_clamps_to_image_and_returns_png():
    page = _page(w=200, h=200)
    page[50:150, 50:150] = 0  # чёрный квадрат — чтобы убедиться, что режем именно область
    data = table_detector.crop_table(page, (40.0, 40.0, 160.0, 160.0), pad=20)
    assert isinstance(data, bytes) and data.startswith(b"\x89PNG")
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    assert img.shape[:2] == (160, 160)          # (160-40) + 2*20, и рамка не вылезла за края


def test_crop_table_rejects_tiny_box():
    assert table_detector.crop_table(_page(), (10.0, 10.0, 15.0, 15.0), pad=0) is None


def test_crop_table_returns_none_for_broken_input():
    assert table_detector.crop_table(b"not-an-image", (0.0, 0.0, 100.0, 100.0)) is None


def test_status_reports_configuration(monkeypatch, tmp_path):
    model = tmp_path / "det.onnx"
    model.write_bytes(b"x")
    monkeypatch.setattr(table_detector, "detector_config", lambda: _cfg(path=str(model)))
    st = table_detector.status()
    assert st["enabled"] is True and st["model_exists"] is True and st["class_id"] == 21
