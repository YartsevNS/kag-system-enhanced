"""Служебные фрагменты: титул/оглавление/колонтитул не вытесняют содержание из контекста."""
from pathlib import Path

from src.indexing.service_chunks import is_service_fragment, order_context, split_service

TITLE = ("ФЕДЕРАЛЬНОЕ АГЕНТСТВО ПО ТЕХНИЧЕСКОМУ РЕГУЛИРОВАНИЮ\nИ МЕТРОЛОГИИ\n"
         "Р 50.1.112—2016")
TOC = ("Содержание\n1 Область применения............ 3\n2 Нормативные ссылки............ 4\n"
       "3 Термины и определения.......... 5\n4 Обозначения и сокращения....... 6")
BODY = ("4.3 Требования к транспортному ключевому контейнеру. Контейнер должен обеспечивать "
        "хранение ключа электронной подписи и его передачу в защищённом виде; формат полей "
        "определён в разделе 5. При передаче ключа используется шифрование по ГОСТ Р 34.12.")


def test_титул_и_оглавление_служебные():
    assert is_service_fragment(TITLE)
    assert is_service_fragment(TOC)


def test_содержательный_текст_не_служебный():
    assert not is_service_fragment(BODY)
    assert not is_service_fragment("Монтаж должен выполняться в соответствии с требованиями раздела 4.3.")


def test_порядок_содержательные_вперёд():
    results = [
        {"content": TITLE, "score": 0.9},
        {"content": BODY, "score": 0.8},
        {"content": TOC, "score": 0.7},
        {"content": BODY, "score": 0.6},
    ]
    ordered, moved = order_context(results, min_substantive=2)
    assert [r["content"][:20] for r in ordered[:2]] == [BODY[:20], BODY[:20]]
    assert moved >= 1, "служебные посчитаны и отодвинуты"


def test_если_содержательных_мало_служебные_остаются():
    results = [{"content": TITLE}, {"content": TOC}, {"content": BODY}]
    ordered, _ = order_context(results, min_substantive=3)
    assert len(ordered) == 3, "ничего не выбрасываем"


def test_только_служебные_не_теряются():
    results = [{"content": TITLE}, {"content": TOC}]
    ordered, moved = order_context(results, min_substantive=3)
    assert len(ordered) == 2 and moved == 0


def test_пустой_текст_служебный():
    assert is_service_fragment("")
    assert split_service([{"content": ""}, {"content": BODY}])[0][0]["content"] == BODY


def test_подключено_в_чате():
    src = Path("src/api/services/chat_service.py").read_text(encoding="utf-8")
    assert src.count("order_context") >= 2, "оба пути сборки контекста (обычный и потоковый)"
