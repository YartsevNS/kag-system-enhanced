"""Разделы документа: распознавание заголовков, крошка, проводка в конвейер."""
from pathlib import Path

from src.indexing.ids import build_embedding_text
from src.indexing.section_parser import (
    build_breadcrumb,
    detect_heading,
    parse_sections,
    sections_for_chunks,
)


def _chunks(texts):
    return [{"chunk_id": f"c{i}", "content": t} for i, t in enumerate(texts)]


# ── распознавание заголовков ─────────────────────────────────────────────────

def test_нумерованный_заголовок():
    head = detect_heading("4.3 Требования к монтажу\nдалее текст требования")
    assert head == {"number": "4.3", "title": "Требования к монтажу"}


def test_заголовок_раздел_и_приложение():
    assert detect_heading("РАЗДЕЛ 4 Общие положения")["number"].lower().startswith("раздел")
    head = detect_heading("Приложение А\nсправочные данные")
    assert head and "Приложение А" in head["number"]


def test_заголовок_заглавными():
    head = detect_heading("ТРЕБОВАНИЯ К СРЕДСТВАМ ЗАЩИТЫ\nтекст")
    assert head and head["title"].startswith("ТРЕБОВАНИЯ")


def test_колонтитул_не_заголовок():
    """Самообозначение документа (колонтитул) заголовком раздела не считается."""
    text = "Р 1323565.1.004—2017\nуведомление и тексты размещаются"
    assert detect_heading(text, "24d001b4-...._r-1323565.1.pdf") is None


def test_служебные_строки_не_заголовок():
    for line in ("© Стандартинформ, 2017", "Все права защищены", "https://example.org",
                 "2024-01-15", "42"):
        assert detect_heading(line + "\nобычный текст") is None, line


def test_заголовок_только_в_начале_чанка():
    text = ("первое предложение обычного текста\nвторое предложение\nтретье предложение\n"
            "4.3 Требования к монтажу")
    assert detect_heading(text) is None, "заголовок в середине чанка не должен размечаться"


# ── разметка по разделам ─────────────────────────────────────────────────────

def test_разметка_чанков_по_разделам():
    chunks = _chunks([
        "ВВЕДЕНИЕ\nобщий текст",
        "текст введения",
        "4.1 Область применения\nтекст",
        "продолжение",
        "4.2 Требования\nтекст требований",
    ])
    secs = parse_sections(chunks)
    assert len(secs) == 3
    assert secs[0]["title"] == "ВВЕДЕНИЕ" and secs[0]["chunk_indexes"] == [0, 1]
    assert secs[1]["number"] == "4.1" and secs[1]["chunk_indexes"] == [2, 3]
    assert secs[2]["number"] == "4.2" and secs[2]["chunk_indexes"] == [4]
    assert all(len(s["chunk_ids"]) == len(s["chunk_indexes"]) for s in secs)


def test_нет_заголовков_нет_разделов():
    assert parse_sections(_chunks(["просто текст", "ещё текст"])) == []


def test_страховка_от_ложной_структуры():
    """Если заголовков больше 40% чанков — это текст, а не структура."""
    chunks = _chunks([f"{i}.{i} Название раздела" for i in range(1, 21)])
    assert parse_sections(chunks) == []


def test_крошка_на_чанк():
    chunks = _chunks(["4.3 Требования к монтажу\nтекст", "продолжение"])
    crumbs = sections_for_chunks(chunks, "ГОСТ Р 123", "x_gost-r-123.pdf")
    assert crumbs[0] == "ГОСТ Р 123 / 4.3 Требования к монтажу"
    assert crumbs[1] == crumbs[0]


def test_крошка_без_разделов_пустая():
    assert sections_for_chunks(_chunks(["текст без структуры"]), "Док") == {}


# ── крошка и текст эмбеддинга ────────────────────────────────────────────────

def test_крошка_строится_и_обрезается():
    assert build_breadcrumb("ГОСТ", "4.3", "Монтаж") == "ГОСТ / 4.3 Монтаж"
    long = build_breadcrumb("Д" * 200, "4.3", "Монтаж", max_chars=50)
    assert len(long) <= 50 and long.endswith("…")


def test_эмбеддинг_карточка_плюс_раздел():
    text = build_embedding_text("текст", {
        "card_prefix": "Р 1323565.1.004—2017. Рекомендации", "section_breadcrumb": "ГОСТ / 4.3 Монтаж",
    })
    assert text.startswith("Р 1323565.1.004—2017. Рекомендации · ГОСТ / 4.3 Монтаж")
    assert text.endswith(": текст")


def test_эмбеддинг_только_раздел():
    text = build_embedding_text("текст", {"section_breadcrumb": "ГОСТ / 4.3 Монтаж"})
    assert text == "ГОСТ / 4.3 Монтаж: текст"


# ── проводка ─────────────────────────────────────────────────────────────────

def test_разделы_пишутся_в_граф_отдельным_типом_связи():
    src = Path("src/indexing/knowledge_graph.py").read_text(encoding="utf-8")
    assert "def create_sections" in src
    body = src[src.index("def create_sections"):src.index("def create_sections") + 2500]
    assert "SECTION_CHUNK" in body, "связь Section→Chunk не должна называться HAS_CHUNK"
    assert "HAS_SECTION" in body


def test_разделы_подключены_в_конвейере_и_перестроении():
    ds = Path("src/api/services/document_service.py").read_text(encoding="utf-8")
    assert "sections_for_chunks" in ds and "section_breadcrumb" in ds
    assert "kg_service.create_sections" in ds
    tasks = Path("src/indexing/tasks.py").read_text(encoding="utf-8")
    assert "create_sections" in tasks, "перестроение тоже должно создавать разделы"
