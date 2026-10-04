"""Тесты детектора ячеек таблицы (RT-DETR) и признака «таблица прозаическая».

Модель ONNX в тестах не нужна: сессия подменяется заглушкой с теми же выходами. Проверяем именно наши
правила — отбор по скору и классу, перевод координат из 640-пространства, NMS, группировку в строки,
склейку текста и признак прозы (он калиброван замером, поэтому тесты фиксируют калибровку).
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from src.indexing import table_cells as tc  # noqa: E402


class _FakeSession:
    def __init__(self, rows):
        self._rows = np.asarray(rows, dtype=np.float32).reshape(-1, 6)

    def get_inputs(self):
        return [SimpleNamespace(name="image"), SimpleNamespace(name="im_shape"),
                SimpleNamespace(name="scale_factor")]

    def run(self, names, feed):  # noqa: ANN001, ARG002
        return [self._rows, np.array([300], dtype=np.int32)]


def _cfg(path: str = "/tmp/cells.onnx", enabled: bool = True, conf: float = 0.4) -> dict:
    return {"enabled": enabled, "path": path, "conf": conf}


def line(text: str, x0: float, y0: float, x1: float, y1: float) -> dict:
    return {"text": text, "bbox": [x0, y0, x1, y1]}


def _page(w: int = 1280, h: int = 640) -> np.ndarray:
    return np.full((h, w, 3), 255, np.uint8)


# ── доступность и поведение по умолчанию ───────────────────────────────────
def test_disabled_by_default(monkeypatch):
    monkeypatch.setattr(tc, "cells_config", lambda: _cfg(enabled=False))
    assert tc.cells_available() is False
    assert tc.detect_cells(_page()) is None
    assert tc.extract_prose_table(_page(), []) is None


def test_enabled_without_model_file(monkeypatch, tmp_path):
    monkeypatch.setattr(tc, "cells_config", lambda: _cfg(path=str(tmp_path / "нет.onnx")))
    assert tc.cells_available() is False
    assert tc.detect_cells(_page()) is None


# ── контракт модели ───────────────────────────────────────────────────────
def test_boxes_converted_from_640_space_and_filtered(monkeypatch, tmp_path):
    model = tmp_path / "cells.onnx"
    model.write_bytes(b"x")
    monkeypatch.setattr(tc, "cells_config", lambda: _cfg(path=str(model)))
    monkeypatch.setattr(tc, "cells_available", lambda cfg=None: True)
    session = _FakeSession([
        [0, 0.95, 10, 10, 100, 50],      # нормальная ячейка
        [0, 0.10, 20, 20, 100, 60],      # скор ниже порога
        [1, 0.90, 30, 30, 100, 60],      # другой класс
        [0, 0.80, 0, 0, 639, 639],       # почти весь кроп (не ячейка, а «вся таблица»)
        [0, 0.85, 5, 5, 7, 7],           # мельче 8 px
    ])
    monkeypatch.setattr(tc, "_session", lambda path: session)

    found = tc.detect_cells(_page(w=1280, h=640))
    assert found is not None and len(found) == 1
    assert found[0] == pytest.approx((20.0, 10.0, 200.0, 50.0), abs=0.5)   # ×2 по X, ×1 по Y


def test_nms_drops_overlapping_boxes(monkeypatch, tmp_path):
    model = tmp_path / "cells.onnx"
    model.write_bytes(b"x")
    monkeypatch.setattr(tc, "cells_config", lambda: _cfg(path=str(model)))
    monkeypatch.setattr(tc, "cells_available", lambda cfg=None: True)
    session = _FakeSession([
        [0, 0.90, 10, 10, 200, 200],
        [0, 0.85, 15, 15, 205, 205],     # почти то же место
        [0, 0.80, 400, 10, 600, 200],    # другая ячейка
    ])
    monkeypatch.setattr(tc, "_session", lambda path: session)
    assert len(tc.detect_cells(_page(w=640, h=640))) == 2


# ── строки и текст ────────────────────────────────────────────────────────
def test_group_rows_by_height_overlap_and_left_to_right():
    cells = [(0, 0, 100, 40), (110, 5, 200, 45), (0, 60, 100, 100), (110, 60, 200, 100)]
    rows = tc.group_rows(cells)
    assert len(rows) == 2
    assert [c[0] for c in rows[0]] == [0, 110]      # внутри строки слева вправо
    assert len(rows[1]) == 2


def test_rows_from_cells_merges_all_lines_of_cell():
    cells = [(0, 0, 100, 100), (100, 0, 200, 100)]
    lines = [line("первая", 10, 10, 90, 30), line("вторая", 10, 40, 90, 60),
             line("соседняя", 110, 10, 190, 30)]
    matrix = tc.rows_from_cells(cells, lines)
    assert matrix == [["первая вторая", "соседняя"]]


# ── признак прозаической таблицы (калибровка замером) ─────────────────────
def test_prose_rows_detects_prose_table():
    rows = [["Этап урока", "Деятельность учителя", "Деятельность обучающихся"]] + [
        ["Организационно-мотивационный этап урока", "Здравствуйте, ребята, я ваш учитель",
         "Приветствуют учителя"] for _ in range(5)]
    assert tc.is_prose_rows(rows) is True


def test_prose_rows_ignores_numeric_table():
    rows = [["№", "Наименование работ", "Стоимость"]] + [[str(i), "монтаж кабеля", "4 500,00"]
                                                         for i in range(1, 8)]
    assert tc.is_prose_rows(rows) is False


def test_prose_rows_ignores_degenerate_and_empty():
    assert tc.is_prose_rows([["длинный текст ячейки номер один", "ещё один длинный текст"]]) is False
    assert tc.is_prose_rows([]) is False


# ── сборка таблицы ────────────────────────────────────────────────────────
def test_extract_prose_table_returns_matrix(monkeypatch):
    monkeypatch.setattr(tc, "detect_cells", lambda image, cfg=None: [
        (0, 0, 100, 50), (100, 0, 200, 50), (0, 50, 100, 100), (100, 50, 200, 100)])
    lines = [line("шапка 1", 10, 10, 90, 30), line("шапка 2", 110, 10, 190, 30),
             line("значение 1", 10, 60, 90, 80), line("значение 2", 110, 60, 190, 80)]
    result = tc.extract_prose_table(_page(), lines)
    assert result is not None
    assert result["rows"] == [["шапка 1", "шапка 2"], ["значение 1", "значение 2"]]
    assert result["source"] == "rtdetr-cells" and result["cells"] == 4


def test_extract_prose_table_rejects_single_row(monkeypatch):
    monkeypatch.setattr(tc, "detect_cells", lambda image, cfg=None: [(0, 0, 100, 50)])
    assert tc.extract_prose_table(_page(), [line("x", 10, 10, 90, 30)]) is None


def test_status_exposes_thresholds(monkeypatch, tmp_path):
    model = tmp_path / "cells.onnx"
    model.write_bytes(b"x")
    monkeypatch.setattr(tc, "cells_config", lambda: _cfg(path=str(model)))
    st = tc.status()
    assert st["model_exists"] is True
    assert st["prose_thresholds"]["min_rows"] == 4
