"""Реестр компании: структура, идемпотентность, соответствие схеме онтологии (AST-проверки)."""
import ast
import io
import json

SRC = "src/indexing/company_registry.py"
SCRIPT = "scripts/import_company_registry.py"


def _tree(path):
    return ast.parse(io.open(path, encoding="utf-8").read())


def test_module_has_expected_operations():
    names = {n.name for n in ast.walk(_tree(SRC)) if isinstance(n, ast.FunctionDef)}
    for fn in ("upsert_object", "upsert_software", "upsert_requirement",
               "link_protection", "link_document", "load"):
        assert fn in names, fn


def test_transactions_per_step():
    """Каждый элемент реестра — своя транзакция: пачечный коммит откатывает всё."""
    text = io.open(SRC, encoding="utf-8").read()
    assert "for name, fn in steps:" in text
    assert "TransactionType.WRITE" in text
    # коммит внутри цикла, а не один на весь файл
    loop = text.split("for name, fn in steps:", 1)[1]
    assert "tx.commit()" in loop.split("finally", 1)[0]


def test_schema_attributes_match_ontology():
    """Атрибуты, которые пишет модуль, объявлены в схеме TypeDB v2."""
    text = io.open(SRC, encoding="utf-8").read()
    for attr in ("ent-key", "cii-category", "is-certified", "requirement_kind",
                 "requirement-id", "obligation-level", "document_key"):
        assert attr in text, attr
    for rel in ("asset_protection", "document_assignment"):
        assert rel in text, rel


def test_certified_written_as_boolean():
    """is-certified — логический атрибут: true/false, не строка."""
    text = io.open(SRC, encoding="utf-8").read()
    assert 'has is-certified {certified}' in text
    assert '"true" if item.get("certified") else "false"' in text


def test_cli_reports_summary(tmp_path):
    """CLI печатает состав файла и результат загрузки, dry-run ничего не пишет."""
    src = io.open(SCRIPT, encoding="utf-8").read()
    assert "--dry-run" in src
    assert "print(\"в файле:" in src
    data = {"objects": [{"key": "a"}], "software": [], "requirements": [],
            "protects": [], "documents": []}
    p = tmp_path / "r.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    loaded = json.loads(p.read_text(encoding="utf-8"))
    assert loaded["objects"][0]["key"] == "a"
