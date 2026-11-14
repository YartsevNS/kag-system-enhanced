"""Тесты ключей записи в TypeDB: требование по тексту и связь «требование → мера».

Стережём два свойства, которые дорого перепутать:
1. ent-key требования зависит от хэша ТЕКСТА, а не от номера пункта (номер ненадёжен);
2. код меры в `requirement-id` превращается в связь `requirement_definition`, а не теряется.
"""

from __future__ import annotations

from src.indexing import typedb_writer as tw


def test_ent_key_requirement_is_text_hash():
    a = tw.ent_key("security_requirement", {"requirement_key": "sha256:aaa"})
    b = tw.ent_key("security_requirement", {"requirement_key": "sha256:aaa"})
    c = tw.ent_key("security_requirement", {"requirement_key": "sha256:bbb"})
    assert a == b
    assert a != c
    assert a.startswith("security_requirement:")


def test_measure_link_created_and_stub_measure_added():
    entities = {
        "security_requirement:r1": {
            "type": "security_requirement",
            "attrs": {"description": "В информационной системе должна обеспечиваться "
                                     "многофакторная аутентификация пользователей.",
                      "requirement-id": "ИАФ.2-1"},
            "chunks": {"doc_chunk_1"},
        }
    }
    relations: dict = {}
    added = tw.link_requirements_to_measures(entities, relations)
    assert added == 1
    rel = next(iter(relations.values()))
    assert rel["type"] == "requirement_definition"
    assert rel["parts"]["requirement"] == "security_requirement:r1"
    m_key = rel["parts"]["measure"]
    assert m_key in entities and entities[m_key]["type"] == "security_measure"
    # measure_key обязателен (@key, @card(1..1)): без него TypeDB отклонит всю транзакцию
    # фрагмента, и требования потеряются молча (CNT5/DVL8)
    assert entities[m_key]["attrs"].get("measure_key")


def test_measure_link_uses_existing_measure():
    m_key = tw.ent_key("security_measure", {"code": "иаф.2.1"})
    entities = {
        m_key: {"type": "security_measure", "attrs": {"code": "иаф.2.1"}, "chunks": {"c1"}},
        "security_requirement:r1": {
            "type": "security_requirement",
            "attrs": {"description": "Требование про аутентификацию устройств.",
                      "requirement-id": "ИАФ.2-1"},
            "chunks": {"c1"},
        },
    }
    relations: dict = {}
    assert tw.link_requirements_to_measures(entities, relations) == 1
    assert next(iter(relations.values()))["parts"]["measure"] == m_key


def test_no_link_for_clause_id():
    entities = {
        "security_requirement:r1": {
            "type": "security_requirement",
            "attrs": {"description": "Требование с номером пункта.", "requirement-id": "2.3-а"},
            "chunks": {"c1"},
        }
    }
    relations: dict = {}
    assert tw.link_requirements_to_measures(entities, relations) == 0
    assert not relations
