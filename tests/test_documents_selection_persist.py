"""Регрессия: выбор галочками на странице «Документы» не сбрасывается автообновлением.

Жалоба пользователя 20.09.2026: отметил файлы чекбоксами для удаления пачкой —
через какое-то время выбор слетел, удалить несколько сразу не получилось.

Причина была структурная: выбор жил только в DOM (querySelectorAll('.doc-check:checked')),
а таблица перерисовывается по таймеру (15 с, а пока идёт обработка — каждые 5 с) и
сбрасывала галочки, панель массовых действий и «выбрать все».

Эти проверки — статические (страница без сборщика, JS внутри HTML): они ловят
возврат именно этой ошибки. Полноценная проверка поведения — в браузере.
"""
import re
from pathlib import Path

PAGE = Path(__file__).resolve().parents[1] / "src/api/static/documents.html"
HTML = PAGE.read_text(encoding="utf-8")
JS = max(re.findall(r"<script>(.*?)</script>", HTML, re.S), key=len)


def test_selection_kept_in_variable_not_only_dom():
    assert re.search(r"let\s+selectedDocs\s*=\s*new\s+Set\(\)", JS), \
        "выбор должен храниться в selectedDocs, иначе перерисовка его теряет"


def test_render_applies_selection_instead_of_resetting():
    render = JS.split("function renderAll")[1].split("async function showDetail")[0]
    assert "selectedDocs" in render, "renderAll должна восстанавливать галочки из selectedDocs"
    assert "syncSelectionUI()" in render, "renderAll должна синхронизировать UI выбора"
    assert "sa.checked = false" not in render, "renderAll больше НЕ сбрасывает «выбрать все»"
    assert "bulk-actions').style.display = 'none'" not in render, \
        "renderAll больше НЕ прячет панель массовых действий"


def test_checkbox_uses_persistent_state():
    assert re.search(r'class="doc-check"[^>]*\$\{selectedDocs\.has\(', JS), \
        "галочка строки должна рисоваться из selectedDocs"
    assert 'onchange="onDocCheck(this)"' in JS, "отметка должна попадать в selectedDocs"


def test_autorefresh_skipped_while_selection_active():
    assert re.search(r"async function loadAll\(auto\s*=\s*false\)", JS), "loadAll должна знать, авто это или нет"
    assert re.search(r"if \(auto && selectedDocs\.size > 0\) return;", JS), \
        "автообновление не должно сбрасывать выбор"
    assert re.search(r"setInterval\(\(\) => loadAll\(true\), 15000\)", JS), \
        "таймер 15 с должен идти как автообновление"
    assert "setInterval(loadAll, 15000)" not in JS, "старый таймер сбрасывал выбор"


def test_bulk_actions_use_selection_set():
    delete_fn = JS.split("async function bulkDelete")[1].split("\nfunction ")[0]
    assert "[...selectedDocs]" in delete_fn, "массовое удаление должно идти по selectedDocs"
    assert "querySelectorAll('.doc-check:checked')" not in delete_fn, \
        "иначе удаляется только то, что видно в DOM после перерисовки"
    assert "failedIds" in delete_fn, "неудачные удаления должны остаться отмеченными"

    process_fn = JS.split("async function processSelected")[1].split("async function bulkDelete")[0]
    assert "[...selectedDocs]" in process_fn, "массовая обработка должна идти по selectedDocs"


def test_bulk_delete_does_not_depend_on_native_confirm():
    """Жалоба 20.09.2026: «кнопка удалить выбранные не действует».

    Причина: подтверждение шло через window.confirm(), а Chrome умеет глушить
    системные диалоги страницы — тогда клик молча ничего не делал (ни запроса, ни
    сообщения). Подтверждение должно быть в самой панели (два нажатия).
    """
    delete_fn = JS.split("async function bulkDelete")[1].split("\n// ")[0]
    assert "confirm(" not in delete_fn, \
        "системный confirm() в массовом удалении использовать нельзя: его могут заглушить"
    assert "_bulkArmedKey" in delete_fn, "нужно подтверждение вторым нажатием"
    assert "setBulkStatus" in delete_fn, "пользователь должен видеть ход и итог удаления"
    assert 'id="btn-bulk-delete"' in HTML and 'id="bulk-status"' in HTML, \
        "кнопка и строка состояния должны иметь id — по ним идёт подтверждение и отчёт"


def test_failed_delete_shows_reason():
    delete_fn = JS.split("async function bulkDelete")[1].split("\n// ")[0]
    assert "reasons" in delete_fn and "r.status" in delete_fn, \
        "код ответа сервера должен попадать в сообщение, иначе причина отказа не видна"


def test_uncaught_errors_are_visible():
    """Ни одно действие не должно молча ничего не делать."""
    assert re.search(r"addEventListener\('error'", JS), \
        "необработанные ошибки страницы должны показываться пользователю"


def test_clear_selection_button_exists():
    assert "clearSelection()" in HTML and "function clearSelection()" in JS, \
        "нужна кнопка «снять выбор», иначе снять пачку можно только по одной"
