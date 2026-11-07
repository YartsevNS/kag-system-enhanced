"""P2.11: удаление документа чистит онтологию (техдолг 17)."""
import io

CLEANUP = "src/indexing/typedb_cleanup.py"
SVC = "src/api/services/document_service.py"


def test_cleanup_module_order():
    """Связи снимаются ДО удаления объектов: иначе TypeDB не даёт удалить (CNT5)."""
    text = io.open(CLEANUP, encoding="utf-8").read()
    assert "def delete_document_ontology(" in text
    roles_pos = text.index("_ROLES = (")
    delete_pos = text.index('step("elements", [f\'{sel} delete $x;\']')
    assert roles_pos < delete_pos, "связи должны сниматься раньше удаления"


def test_cleanup_covers_required_roles():
    text = io.open(CLEANUP, encoding="utf-8").read()
    for rel, role in (("applicability_criteria", "applied_requirement"),
                      ("document_hierarchy", "structural_part"),
                      ("document_hierarchy", "sub_part"),
                      ("measure_definition", "core_measure"),
                      ("requirement_basis", "requirement")):
        assert rel in text and role in text, f"{rel}:{role}"


def test_cleanup_removes_versions_and_acts():
    text = io.open(CLEANUP, encoding="utf-8").read()
    assert "document_version_of (version: $v)" in text
    assert "supersedes (newer: $v)" in text
    assert "document_issuance (regulated_item: $d)" in text
    assert "document_assignment (regulated_doc: $d)" in text


def test_cleanup_keeps_registry_objects():
    """Реестр компании (объекты КИИ, СЗИ) не принадлежит документу — не удаляем."""
    text = io.open(CLEANUP, encoding="utf-8").read()
    body = text.split("_DOC_OWNED = (", 1)[1].split("_DOC_ACTS", 1)[0]
    assert "cii_object" not in body
    assert "security_software" not in body
    assert "_DOC_ACTS" in text
    assert "regulatory_document" in text.split("_DOC_ACTS = (", 1)[1].split(")", 1)[0]


def test_delete_calls_cleanup():
    text = io.open(SVC, encoding="utf-8").read()
    block = text.split("async def delete_document", 1)[1].split("def _save_document_tables", 1)[0]
    assert "delete_document_ontology" in block
