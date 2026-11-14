"""Тесты Silver-разрешения: ключ требования по тексту, вид идентификатора, слияние/разделение.

Смысл набора — стеречь правила, которые дорого перепутать:
1. ключ требования зависит от ТЕКСТА, а не от номера пункта (замер 14.11.2026: номер надёжен
   только у 10 строк из 508);
2. «УПД.10-1-а» и «УПД.10-1а» — один идентификатор, а «ИАФ.2-5» и «ИАФ.2-орг» — разные;
3. регуляторы сводятся по коду органа, а не по написанию названия.
"""

from __future__ import annotations

from src.indexing import bronze_resolve as br
from src.indexing import typedb_writer as tw


def test_requirement_key_ignores_id_and_case():
    a = {"description": "В информационной системе должна обеспечиваться многофакторная "
                        "аутентификация пользователей.", "requirement-id": "ИАФ.2-1"}
    b = {"description": "в информационной системе должна обеспечиваться  многофакторная "
                        "аутентификация пользователей", "requirement-id": "5"}
    assert br.requirement_key(a) == br.requirement_key(b)
    assert br.requirement_key(a).startswith("sha256:")


def test_requirement_key_differs_for_different_text():
    a = {"description": "Шифрование хранимой информации на носителе."}
    b = {"description": "Управление потоками информации между компонентами."}
    assert br.requirement_key(a) != br.requirement_key(b)


def test_requirement_cluster_key_uses_text_not_clause():
    one = {"description": "Контроль целостности атрибутов безопасности.",
           "requirement-id": "ЗИС.6-1", "document_key": "kag:doc"}
    two = {"description": "контроль целостности атрибутов безопасности",
           "requirement-id": "зис.6-1", "document_key": "kag:doc"}
    other = {"description": "Изоляция процессов в выделенной области памяти.",
             "requirement-id": "ЗИС.6-1", "document_key": "kag:doc"}
    assert br.entity_cluster_key("security_requirement", one) == \
        br.entity_cluster_key("security_requirement", two)
    assert br.entity_cluster_key("security_requirement", one) != \
        br.entity_cluster_key("security_requirement", other)


def test_id_compare_collapses_hyphen_and_case():
    assert br.id_compare_key("УПД.10-1-а") == br.id_compare_key("упд.10-1а")
    assert br.id_compare_key("ИАФ.2-5") != br.id_compare_key("ИАФ.2-орг")


def test_requirement_id_kinds():
    assert br.requirement_id_kind("2.3") == "clause"
    assert br.requirement_id_kind("2.3-а") == "clause"
    assert br.requirement_id_kind("7.7.1.5") == "clause"
    assert br.requirement_id_kind("приложение А") == "clause"
    assert br.requirement_id_kind("5") == "bare_number"
    assert br.requirement_id_kind("ИАФ.2-1") == "measure_ref"
    assert br.requirement_id_kind("пользователи-1") == "word"
    assert br.requirement_id_kind("") == "empty"


def test_requirement_measure_code_only_for_measure_ref():
    # дефис и точка в коде меры унифицируются намеренно (правило «АВЗ-1 = АВЗ.1 = авз 1»),
    # поэтому ИАФ.2-1 даёт «иаф.2.1» — тот же ключ, по которому живут меры
    assert br.requirement_measure_code({"requirement-id": "ИАФ.2-1"}) == "иаф.2.1"
    assert br.requirement_measure_code({"requirement-id": "2.3-а"}) == ""
    assert br.requirement_measure_code({"requirement-id": "5"}) == ""


def test_regulator_key_by_organ_code():
    short = {"name": "ФСТЭК России"}
    full = {"name": "Федеральная служба по техническому и экспортному контролю (ФСТЭК России)"}
    other = {"name": "Банк России"}
    k_short = br.entity_cluster_key("regulator", short)
    k_full = br.entity_cluster_key("regulator", full)
    k_other = br.entity_cluster_key("regulator", other)
    assert k_short == k_full
    assert k_short != k_other
    assert "fstek" in k_short


def test_measure_key_is_code():
    a = {"code": "АВЗ.1", "name": "Антивирусная защита"}
    b = {"code": "авз-1", "name": "антивирусная защита"}
    assert br.entity_cluster_key("security_measure", a) == \
        br.entity_cluster_key("security_measure", b)


def test_measure_key_normalized_in_collect():
    # ключ меры считается в ent_key из measure_key, поэтому нормализуем код ДО ent_key:
    # иначе «АВЗ-1» и «АВЗ.1» — два разных объекта (замер 14.11.2026: 431 мера вместо 120)
    def key_for(code: str) -> str:
        payload = {"entities": [{"id": "m1", "type": "security_measure",
                                 "attributes": {"code": code, "name": "Антивирусная защита"}}],
                   "relations": []}
        ents, _rels, _st = tw._collect(payload, "doc_chunk_1", "doc-1")
        (k, e), = ents.items()
        assert e["attrs"]["measure_key"] == "авз.1"
        return k

    assert key_for("АВЗ-1") == key_for("АВЗ.1") == key_for("авз 1")


def test_regulatory_document_is_not_regulator():
    # «regulatory_document» содержит подстроку «regulator»: проверка по подстроке уводила
    # 305 строк документа в ветку регулятора и в очередь no_issuer_code
    assert br.entity_branch("regulatory_document") == "document"
    assert br.entity_branch("document_version") == "document"
    assert br.entity_branch("regulator") == "regulator"
    assert br.entity_branch("security_requirement") == "requirement"
    assert br.entity_branch("security_measure") == "measure"
    assert br.entity_branch("structural_element") == "other"
