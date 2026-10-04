"""Тесты врезки детектора в конвейер: порядок «сетка по странице → кроп от детектора».

Главное свойство: на чистом бланке сетка по странице даёт лучший результат (замер: 8×3 качество 0,98
против 9×5 и 0,59 по кропу), поэтому детектор — ВТОРОЙ шанс, и на успешной странице он вообще не
вызывается. Выключенный/недоступный детектор ничего не меняет.
"""
from __future__ import annotations

from src.indexing import table_detector, table_strategy
from src.indexing.table_recovery import RecoveredTable


def _cfg(**kw) -> dict:
    base = {"enabled": True, "path": "/tmp/det.onnx", "conf": 0.5, "pad": 8}
    base.update(kw)
    return base


def _table() -> RecoveredTable:
    return RecoveredTable(rows=[["a", "b"], ["c", "d"]], source="grid",
                          source_model="our-grid", quality=0.9, seconds=0.2)


def _page_lines(numeric: bool, inside=(100.0, 200.0, 900.0, 800.0)) -> list:
    """Строки страницы: 3 колонки, по 4 блока; numeric — две колонки чисел, иначе все текстовые."""
    lines: list = []
    x0, y0, x1, y1 = inside
    for i in range(4):
        y = y0 + 20 + i * 30
        left = str(i + 1) if numeric else "раздел"
        right = f"{100 + i * 10},00" if numeric else "описание работы"
        lines.append({"text": left, "bbox": [x0 + 20, y, x0 + 60, y + 20]})
        lines.append({"text": "наименование", "bbox": [x0 + 200, y, x0 + 500, y + 20]})
        lines.append({"text": right, "bbox": [x0 + 600, y, x0 + 700, y + 20]})
    return lines


def test_grid_success_does_not_call_detector(monkeypatch):
    """Детектор — второй шанс: если сетка по странице сработала, детектор не трогаем (и не платим за него)."""
    called = {"detect": 0}

    def spy(image, cfg=None):
        called["detect"] += 1
        return [{"score": 0.9, "bbox": (0.0, 0.0, 100.0, 100.0)}]

    monkeypatch.setattr(table_detector, "detector_config", lambda: _cfg())
    monkeypatch.setattr(table_detector, "detect_tables", spy)
    monkeypatch.setattr(table_strategy, "recognize_grid_tables",
                        lambda image, raw_lines=None: ([_table()], "сетка по линиям: 8×3, качество 0.98"))
    out = table_strategy.recover_tables(b"page")
    assert out["technique"] == "occular-grid"
    assert called["detect"] == 0
    assert "детектор" not in out["reason"]


def test_detector_off_keeps_previous_path(monkeypatch):
    monkeypatch.setattr(table_detector, "detector_config", lambda: _cfg(enabled=False))
    monkeypatch.setattr(table_detector, "detect_tables", lambda image, cfg=None: None)
    monkeypatch.setattr(table_strategy, "recognize_grid_tables",
                        lambda image, raw_lines=None: ([], "линий сетки не найдено"))
    out = table_strategy.recover_tables(b"page")
    assert out["technique"] != "detector-grid" and out["tables"] == []


def test_crop_used_when_page_grid_failed(monkeypatch):
    monkeypatch.setattr(table_detector, "detector_config", lambda: _cfg())
    monkeypatch.setattr(table_detector, "detect_tables",
                        lambda image, cfg=None: [{"score": 0.93, "bbox": (100.0, 200.0, 900.0, 800.0)}])
    monkeypatch.setattr(table_detector, "crop_table", lambda image, box, pad=8: b"PNG-crop")
    seen = {}

    def fake_grid(image, raw_lines=None):
        seen["image"] = image
        if image == b"PNG-crop":
            return [_table()], "сетка по линиям: 9 строк × до 3 колонок, качество 0.7"
        return [], "линий сетки не найдено за 0.1 с"

    monkeypatch.setattr(table_strategy, "recognize_grid_tables", fake_grid)
    out = table_strategy.recover_tables(b"page", raw_lines=_page_lines(numeric=True))
    assert out["technique"] == "detector-grid"
    assert seen["image"] == b"PNG-crop"
    assert "тип: numeric" in out["reason"]
    assert "линий сетки не найдено" in out["reason"]          # причина первой попытки сохранена
    assert any("тип таблицы: numeric" in n for n in out["tables"][0].notes)


def test_prose_region_is_reported_as_prose(monkeypatch):
    monkeypatch.setattr(table_detector, "detector_config", lambda: _cfg())
    monkeypatch.setattr(table_detector, "detect_tables",
                        lambda image, cfg=None: [{"score": 0.9, "bbox": (100.0, 200.0, 900.0, 800.0)}])
    monkeypatch.setattr(table_detector, "crop_table", lambda image, box, pad=8: b"PNG-crop")
    monkeypatch.setattr(table_strategy, "recognize_grid_tables",
                        lambda image, raw_lines=None: ([_table()], "сетка") if image == b"PNG-crop"
                        else ([], "нет линий"))
    out = table_strategy.recover_tables(b"page", raw_lines=_page_lines(numeric=False))
    assert "тип: prose" in out["reason"]


def test_crop_failure_mentioned_and_other_paths_continue(monkeypatch):
    monkeypatch.setattr(table_detector, "detector_config", lambda: _cfg())
    monkeypatch.setattr(table_detector, "detect_tables",
                        lambda image, cfg=None: [{"score": 0.9, "bbox": (0.0, 0.0, 500.0, 500.0)}])
    monkeypatch.setattr(table_detector, "crop_table", lambda image, box, pad=8: None)
    monkeypatch.setattr(table_strategy, "recognize_grid_tables",
                        lambda image, raw_lines=None: ([], "нет линий"))
    out = table_strategy.recover_tables(b"page")
    assert out["technique"] != "detector-grid"
    assert out["tables"] == []                                # страница не потеряна, просто без таблицы


def test_detector_failure_does_not_break_page(monkeypatch):
    def boom(image, cfg=None):
        raise RuntimeError("детектор упал")

    monkeypatch.setattr(table_detector, "detect_tables", boom)
    monkeypatch.setattr(table_strategy, "recognize_grid_tables",
                        lambda image, raw_lines=None: ([], "нет линий"))
    out = table_strategy.recover_tables(b"page")
    assert out["technique"] != "detector-grid"


def test_translate_lines_shifts_into_crop_coordinates():
    lines = [{"text": "x", "quad": [[100, 200], [150, 200], [150, 220], [100, 220]]}]
    moved = table_strategy._translate_lines(lines, (60.0, 150.0, 400.0, 400.0), pad=10)
    assert moved[0]["quad"][0] == [50.0, 60.0]        # вычли (60-10, 150-10)
    assert table_strategy._translate_lines(None, (0, 0, 1, 1), 0) == []
