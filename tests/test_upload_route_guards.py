"""Структурные проверки upload.py: права на местах, кэш без объекта пользователя.

Дополняют юниты на document_access: юниты проверяют логику, эти — что она
действительно подключена к эндпоинтам (иначе логика есть, а доступа нет).
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "src/api/routes/upload.py").read_text(encoding="utf-8")
TREE = ast.parse(SRC)

READ_ENDPOINTS = [
    "get_document_details", "get_document_chunks", "get_document_preview",
    "get_document_thumbnail", "get_document_status", "get_document_versions",
    "diff_document_versions", "check_ocr", "view_ocr_markdown",
    "get_document_tables", "search_document_tables", "get_document_access",
]
ADMIN_ONLY = ["reanalyze_all_documents", "reindex_all_documents",
              "reprocess_pending_documents", "reprocess_ocr"]


def _func(name):
    for node in ast.walk(TREE):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"функция не найдена: {name}")


def _signature(name):
    return ast.unparse(_func(name).args)


def _body(name):
    node = _func(name)
    return ast.unparse(node)


def test_read_endpoints_require_auth_and_acl():
    for name in READ_ENDPOINTS:
        sig = _signature(name)
        assert "Depends" in sig, f"{name}: нет Depends — эндпоинт без проверки прав"
        assert "ensure_can_read" in _body(name), (
            f"{name}: нет проверки ACL — документ отдаётся без учёта visibility/allow/deny"
        )


def test_mass_operations_are_admin_only():
    for name in ADMIN_ONLY:
        assert "get_current_admin" in _signature(name), f"{name}: массовая операция не admin-only"


def test_reindex_and_process_check_owner():
    assert "ensure_owner_or_admin" in _body("reindex_document"), "reindex без проверки владельца"
    body = _body("process_document_now")
    assert "Требуется аутентификация" in body, "process пускает анонима/системный документ без админа"
    assert "системный документ" in body, "process не ограничивает системные документы админом"


def test_tus_handlers_check_session_owner():
    for name in ("tus_head", "tus_patch", "tus_delete"):
        body = _body(name)
        assert "_tus_check_owner" in body, f"{name}: нет проверки владельца TUS-сессии"
        assert "Depends" in _signature(name), f"{name}: нет зависимости авторизации"


def test_tus_create_respects_upload_block():
    assert "_deny_if_uploads_blocked()" in _body("tus_create"), (
        "TUS создаёт сессию в обход блокировки загрузки"
    )


def test_dead_cache_is_removed():
    """@_cached не применялся к роуту (FastAPI держит оригинальную функцию) —
    мёртвый кэш с ключом по адресу памяти убран, а не «починен»."""
    assert "_cached" not in SRC, "в модуле снова есть декоратор кэша"
    assert "_cache: Dict" not in SRC, "вернулся модульный кэш"
    assert "count_document_points" in SRC, (
        "/details должен считать чанки точным count, а не прокруткой всех точек"
    )


def test_document_access_has_pydantic_model():
    assert "class DocumentAccessUpdate" in SRC, "права документа принимаются без модели"
    assert "payload.model_dump(exclude_unset=True)" in SRC, (
        "частичное обновление прав должно опираться на exclude_unset"
    )


def test_dependency_names_are_imported():
    """Страховка от NameError на старте: имя из Depends(…) должно быть импортировано.

    На этих граблях уже стояли: добавили get_current_admin в подписи, но не в
    импорт — api ушёл в crash-loop, а статические проверки этого не видят.
    """
    import re

    header = SRC.split("@router.")[0]
    used = set(re.findall(r"Depends\((\w+)", SRC))
    missing = sorted(n for n in used if n not in header)
    assert not missing, f"не импортированы зависимости: {missing}"


def test_reprocess_pending_has_no_create_task():
    """create_task без ссылки + count до выполнения: заменено на прямой enqueue."""
    body = _body("reprocess_pending_documents")
    assert "create_task" not in body, "вернулся asyncio.create_task"
    assert "enqueue_document(did, force=True)" in body, "нет прямой постановки в очередь"
    assert "_process_document_async" not in SRC, "вернулась обёртка _process_document_async"


def test_no_dead_task_imports():
    assert "from src.indexing.tasks import process_document" not in SRC, (
        "мёртвый импорт process_document вернулся (нигде не используется)"
    )


def test_bulk_uses_archive_guard():
    assert "check_member_names(" in SRC and "check_tar_members(" in SRC, (
        "upload_bulk распаковывает архивы без проверки имён/ссылок"
    )
    assert "safe_target(" in SRC, "нет проверки пути распаковки"
    assert "ArchiveRejected" in SRC, "отказ по архиву не обрабатывается отдельно"


def test_limits_come_from_settings():
    assert "MAX_FILE_SIZE = _settings.MAX_FILE_SIZE" in SRC, "лимит файла снова хардкод"
    assert "UPLOAD_TEMP_DIR" in SRC, "каталог распаковки снова хардкод /tmp"


# ── вторая половина аудита: блокирующий I/O, iterdir, лимит чанков ─────────

HOT_ASYNC = [
    "get_document_details", "get_document_chunks", "get_document_thumbnail",
    "get_document_preview", "queue_status", "tus_head", "tus_patch",
    "tus_delete", "upload_bulk", "tus_create",
]


def test_blocking_io_moved_to_thread():
    """В async-обработчиках ФС/БД/Qdrant/celery не должны блокировать event loop."""
    missing = [n for n in HOT_ASYNC if "asyncio.to_thread" not in _body(n)]
    assert not missing, f"синхронный I/O в async без to_thread: {missing}"


def test_routes_do_not_scan_uploads_dir():
    """Поиск файла перебором каталога (iterdir по всем документам) убран."""
    assert "upload_dir.iterdir" not in SRC, "вернулся перебор каталога при поиске файла"
    assert "find_file" in SRC, "роуты должны искать файл через document_service.find_file"


def test_chunks_report_truncation():
    assert "CHUNKS_SCROLL_LIMIT" in SRC
    body = _body("get_document_chunks")
    assert "truncated" in body, "лимит scroll должен отражаться в ответе, а не теряться молча"


def test_tus_delete_returns_204_explicitly():
    body = _body("tus_delete")
    assert "Response(status_code=204)" in body, (
        "Response() отдаёт 200 и перебивает объявленный в декораторе 204"
    )


def test_dirs_taken_from_settings():
    assert "_settings.TUS_DIR" in SRC, "TUS_DIR снова литерал"
    assert "_settings.THUMBNAILS_DIR" in SRC, "каталог миниатюр снова литерал"


def test_model_returns_explicit_status_for_deleted_document():
    tasks_src = (ROOT / "src/indexing/tasks.py").read_text(encoding="utf-8")
    assert '{"status": "not_found"' in tasks_src, (
        "задача на удалённый документ должна завершаться явно, а не падать в document_service"
    )
