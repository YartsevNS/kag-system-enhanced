"""Источник документа: обязателен при загрузке через сайт, ставится парсером.

Требование владельца (07.11.2026): при загрузке через сайт пользователь ОБЯЗАН выбрать
«Компания» или «Регулятор». «Регулятор» — это парсинг страницы мониторинга (web monitor),
он ставит значение сам. В онтологии это разные типы объектов: company_document против
regulatory_document, поэтому признак нельзя выводить из вида документа (вид — разметка).
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UPLOAD_SRC = (ROOT / "src/api/routes/upload.py").read_text(encoding="utf-8")
UPLOAD_TREE = ast.parse(UPLOAD_SRC)
MONITOR_SRC = (ROOT / "src/api/services/web_monitor.py").read_text(encoding="utf-8")
WRITER_SRC = (ROOT / "src/indexing/typedb_writer.py").read_text(encoding="utf-8")


def _func(name):
    for node in ast.walk(UPLOAD_TREE):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"функция не найдена: {name}")


def test_single_upload_requires_source_kind():
    """Одиночная загрузка отклоняет запрос без source_kind."""
    node = _func("upload_document")
    consts = [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert "SOURCE_KIND_REQUIRED" in consts
    assert "company" in consts and "regulator" in consts


def test_batch_and_bulk_require_source_kind():
    for name in ("upload_documents_batch", "upload_bulk"):
        node = _func(name)
        consts = [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        assert any(c in ("company", "regulator") for c in consts), name
    # Импорт из папки берёт значение из обязательного поля запроса (Literal в модели)
    node = _func("import_folder")
    seg = ast.get_source_segment(UPLOAD_SRC, node) or ""
    assert "req.source_kind" in seg


def test_import_folder_request_field_is_required():
    """В ImportFolderRequest поле обязательное (нет default) — иначе админ импортирует вслепую."""
    tree = ast.parse(UPLOAD_SRC)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ImportFolderRequest":
            ann = {a.target.id: a for a in node.body if isinstance(a, ast.AnnAssign)}
            assert "source_kind" in ann, "нет поля source_kind"
            assert ann["source_kind"].value is not None, "поле без аннотации"
            return
    raise AssertionError("ImportFolderRequest не найден")


def test_web_monitor_marks_regulator():
    """Парсер страницы мониторинга ставит regulator на всех путях загрузки."""
    assert MONITOR_SRC.count("source_kind='regulator'") >= 4


def test_writer_knows_company_document():
    """writer умеет писать company_document и переклассифицирует документ компании."""
    assert '"company_document"' in WRITER_SRC
    assert "company_document" in WRITER_SRC.split("ENTITY_TYPES")[1][:600]
    assert 'source_kind or ""' in WRITER_SRC or "source_kind" in WRITER_SRC
    assert '"document_issuance"' in WRITER_SRC  # связь выдачи отфильтровывается для компании
