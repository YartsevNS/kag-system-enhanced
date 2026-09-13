"""Разбор ответа анализатора документа: markdown, текст вокруг, обрыв, вложенность."""
from src.api.services.document_analyzer import document_analyzer as a


def test_чистый_json():
    res = a._parse_response('{"title": "ГОСТ Р 1", "type": "standard", "summary": "о чём", "topics": ["тема"]}', "f.pdf")
    assert res["recognized_title"] == "ГОСТ Р 1"
    assert res["document_type"] == "standard"
    assert res["topics"] == ["тема"]


def test_json_в_markdown_обёртке():
    res = a._parse_response('```json\n{"title": "T", "type": "standard", "topics": ["a", "b"]}\n```', "f.pdf")
    assert res["recognized_title"] == "T", "обёртка ```json не должна ломать разбор"


def test_json_с_текстом_вокруг():
    res = a._parse_response('Вот результат: {"title": "T2", "type": "standard"} — готово', "f.pdf")
    assert res["recognized_title"] == "T2"


def test_обрыв_ответа_не_даёт_полей():
    assert a._parse_response('{"title": "T3", "topics": ["a"', "f.pdf") == {}


def test_вложенные_скобки():
    res = a._parse_response('{"title": "T4", "type": "standard", "meta": {"x": {"y": 1}}, "topics": ["t"]}', "f.pdf")
    assert res["recognized_title"] == "T4" and res["topics"] == ["t"]


def test_неизвестный_тип_отбрасывается():
    res = a._parse_response('{"title": "T", "type": "чепуха"}', "f.pdf")
    assert "document_type" not in res


def test_пустой_ответ():
    assert a._parse_response("", "f.pdf") == {}
    assert a._parse_response("не json вовсе", "f.pdf") == {}


def test_лимиты_полей():
    res = a._parse_response('{"title": "%s", "summary": "%s", "topics": ["1","2","3","4","5","6","7"]}'
                            % ("т" * 400, "с" * 900), "f.pdf")
    assert len(res["recognized_title"]) == 200
    assert len(res["summary"]) == 500
    assert len(res["topics"]) == 5, "не больше 5 тем"
