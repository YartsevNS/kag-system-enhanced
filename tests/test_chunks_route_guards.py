"""Роут /api/v1/chunks: синхронный Qdrant в async и честное усечение.

Тот же класс, что в upload.py: синхронный клиент Qdrant вызывался прямо в async
функции (блокирует event loop), а жёсткий лимит scroll молча терял чанки.
"""
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "src/api/routes/chunks.py").read_text(encoding="utf-8")


def test_scroll_runs_in_thread():
    assert "asyncio.to_thread(" in SRC, "scroll_points снова вызывается прямо в async"
    assert "await asyncio.to_thread" in SRC


def test_truncation_is_reported():
    assert "CHUNKS_SCROLL_LIMIT" in SRC, "лимит scroll не вынесен в константу"
    assert '"truncated": truncated' in SRC, "усечение снова тихое"
