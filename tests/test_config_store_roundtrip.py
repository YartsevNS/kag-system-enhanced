"""config_store: round-trip значений и чтение старых «сырых» записей.

Симптом, который закрывают тесты (найден на стенде 2026-09-13): set() писал
строки сырым текстом, а get() всегда вызывал json.loads. Итог: set("running") →
get() → None, set("123") → int 123, и /rebuild-status никогда не показывал
«идёт перестроение», потому что статус — строка.
"""
import json
from pathlib import Path

from src.api.services.config_store import config_store

ROOT = Path(__file__).resolve().parents[1]


def test_string_round_trip_is_exact():
    for value in ("running", "idle", "123", "true", "", "ГОСТ Р 57580"):
        encoded = json.dumps(value)          # так теперь пишет set()
        assert config_store._decode_value(encoded) == value, f"строка {value!r} испортилась"


def test_number_bool_and_containers_round_trip():
    for value in (0, 123, 1.5, True, False, None, [1, 2], {"a": 1}, {"blocked": True, "message": ""}):
        assert config_store._decode_value(json.dumps(value)) == value, f"значение {value!r} испортилось"


def test_legacy_raw_rows_are_readable():
    """Старые записи (сырой текст) должны читаться строкой, а не теряться."""
    assert config_store._decode_value("idle") == "idle"
    assert config_store._decode_value("running") == "running"
    assert config_store._decode_value("completed") == "completed"


def test_legacy_numeric_looking_string_becomes_number():
    """Плата за отсутствие типа в БД: «123» без кавычек неотличимо от числа.

    Таких записей в системе нет (проверено: из 3228 ключей сырых было 2, обе —
    слова), а новые пишутся в кавычках, поэтому поведение зафиксировано тестом.
    """
    assert config_store._decode_value("123") == 123


def test_get_uses_decoder_not_bare_json_loads():
    src = (ROOT / "src/api/services/config_store.py").read_text(encoding="utf-8")
    start = src.index("    def get(self")
    block = src[start:src.index("    def set(self")]
    assert "_decode_value" in block, "get() обязан разбирать значение тем же способом, что и записи"
    assert "json.loads(record.value)" not in block, "прямой json.loads на сырой строке даёт None"


def test_set_serializes_strings_as_json():
    src = (ROOT / "src/api/services/config_store.py").read_text(encoding="utf-8")
    start = src.index("    def set(self")
    block = src[start:start + 1400]
    assert "json.dumps(value)" in block
    assert "serialized = str(value)" not in block, (
        "strings must be JSON-encoded, otherwise round-trip breaks (raw '123' != str)"
    )
