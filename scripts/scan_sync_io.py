#!/usr/bin/env python3
"""Карта синхронных вызовов внутри async-функций (по AST).

Зачем: один iterdir — симптом, а не диагноз. Нужен список ВСЕХ мест, где в
async-функции выполняется блокирующая работа (файлы, БД, Qdrant, Neo4j,
subprocess, docker SDK, celery inspect), чтобы чинить не по одному, а системно.

Как считаем:
* собираем имена всех async-функций проекта — вызовы таких имён не блокирующие;
* вызов считается уже безопасным, если он находится внутри await asyncio.to_thread(...)
  (или await run_in_executor / anyio.to_thread.run_sync);
* ищем вызовы по списку блокирующих паттернов (builtins ФС, shutil/os, zip/tar,
  subprocess/requests/urllib, celery inspect, docker SDK, sqlalchemy session,
  qdrant/neo4j синхронные методы, config_store, репозиторий документов).

Вывод: сводка по файлам + конкретные места (файл:строка, вызов, объемлющая
async-функция), отсортированные по «горячности» (частые эндпоинты помечены).
"""
import ast
import re
import sys
from collections import Counter
from pathlib import Path

BLOCKING = [
    # ФС
    (r"^open$", "файл: open()"),
    (r"\.(iterdir|rglob|glob|walk)$", "Path: обход каталога (дорого)"),
    (r"\.(read_text|write_text|read_bytes|write_bytes)$", "Path: чтение/запись файла"),
    (r"\.(unlink|mkdir|rmdir|rename|replace|touch)$", "Path: изменение ФС"),
    (r"\.(stat|lstat|exists|is_file|is_dir|resolve|samefile)$", "Path: проверка (дёшево)"),
    (r"^(shutil|os)\.(listdir|walk|scandir|stat|remove|unlink|rename|replace|makedirs|"
     r"mkdir|rmdir|removedirs|copytree|copy|copyfile|move|disk_usage)$", "ФС: os/shutil"),
    (r"^(zipfile\.ZipFile|tarfile\.open)$", "архив: открытие"),
    (r"^\.(extract|extractall|writestr|addfile)$", "архив: операция"),
    (r"\.(getmembers|infolist)$", "архив: чтение оглавления"),
    # процессы и сеть
    (r"^subprocess\.(run|call|check_call|check_output|Popen)$", "процесс: subprocess"),
    (r"^(requests|httpx)\.(get|post|put|delete|patch|request)$", "сеть: синхронный HTTP"),
    (r"^urllib\.request\.urlopen$|^urlopen$", "сеть: urllib"),
    (r"^time\.sleep$", "sleep"),
    # инфраструктура проекта
    (r"control\.inspect$|\.inspect$", "celery: inspect (блокирующий)"),
    (r"^docker\.from_env$|\.containers\.get$|\.containers\.list$|\.container_stats$|"
     r"\.get_container_stats$|\.logs\(", "docker SDK"),
    (r"^psutil\.", "psutil"),
    (r"(config_store\.(get|set|delete)$)", "config_store (БД)"),
    (r"(get_doc_repo\(\)|document_repository)\.", "БД: репозиторий документов"),
    (r"\.query\($|\.execute\($|session\.run$", "БД: SQL/Cypher"),
    (r"(scroll_points|get_collection|get_collections|collection_info|points_count|"
     r"delete_points|upsert_points|set_payload|retrieve|search_points)$", "qdrant: синхронный клиент"),
    (r"(get_stats|execute_cypher|search_entities|get_document_entities|get_entity_graph|"
     r"entity_chunks|search_chunks|get_document_graph|chunk_info|batch_create|"
     r"post_process_entities|deduplicate_entities_by_name|set_domain_schema)$",
     "kg_service (Neo4j)"),
    (r"^document_service\.(process_document|upload_document|delete_document|"
     r"get_document_status|_save_document_to_db|_generate_thumbnail)$", "document_service (sync)"),
    (r"^(hashlib|bcrypt)", "криптография (CPU)"),
]

SAFE_WRAPPERS = {"to_thread", "run_in_executor", "run_sync", "to_thread.run_sync"}
HOT_ENDPOINTS = re.compile(r"(preview|thumbnail|details|chunks|list|queue|status)")


def python_files(root: Path):
    return sorted(p for p in root.rglob("*.py") if ".venv" not in p.parts and "__pycache__" not in p.parts)


def async_names(files):
    names = set()
    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.AsyncFunctionDef,)):
                names.add(node.name)
    return names


def find_sync_io(path: Path, async_defs: set):
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError:
        return []
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        # Вложенные синхронные функции, переданные в to_thread/run_in_executor:
        # их тело исполняется в потоке, поэтому вызовы внутри — НЕ находки
        # (например queue_status → _inspect() → celery control.inspect).
        safe_nested = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and any(w in ast.unparse(sub.func) for w in SAFE_WRAPPERS):
                for arg in sub.args:
                    if isinstance(arg, ast.Name):
                        safe_nested.add(arg.id)
                    elif isinstance(arg, ast.Attribute):
                        safe_nested.add(arg.attr)
        skipped_calls = set()
        if safe_nested:
            for sub in ast.walk(node):
                if isinstance(sub, ast.FunctionDef) and sub.name in safe_nested:
                    for inner in ast.walk(sub):
                        if isinstance(inner, ast.Call):
                            skipped_calls.add(id(inner))
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            if id(sub) in skipped_calls:
                continue
            src = ast.unparse(sub.func)
            if any(p.search(src) for p in PATTERNS):
                if any(w in src for w in SAFE_WRAPPERS):
                    continue
                # вызов async-функции проекта — не блокирующий
                tail = src.split(".")[-1]
                if tail in async_defs:
                    continue
                # вызов уже внутри to_thread/run_in_executor (в т.ч. в лямбде,
                # переданной в to_thread) — это НЕ находка
                if is_wrapped(sub, node):
                    continue
                found.append((path, sub.lineno, src, node.name, is_wrapped(sub, node)))
    return found


PATTERNS = [re.compile(p) for p, _ in BLOCKING]


def is_wrapped(call: ast.Call, func: ast.AsyncFunctionDef) -> bool:
    """Лежит ли вызов внутри await asyncio.to_thread(...) — грубая, но полезная проверка."""
    for node in ast.walk(func):
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            inner = ast.unparse(node.value.func)
            if any(w in inner for w in SAFE_WRAPPERS):
                for sub in ast.walk(node.value):
                    if sub is call:
                        return True
    return False


def main(root: Path):
    files = python_files(root)
    async_defs = async_names(files)
    all_found = []
    for path in files:
        all_found.extend(find_sync_io(path, async_defs))

    def pattern(src: str) -> str:
        """Вызов без строковых/числовых литералов — чтобы «одно место» не считалось за 3."""
        import re as _re
        return _re.sub(r"'[^']*'|\"[^\"]*\"|\d+", "?", src)

    unique = {(str(p.relative_to(root)), func, pattern(src)) for p, _, src, func, _ in all_found}
    by_file = Counter(str(p.relative_to(root)) for p, *_ in all_found)
    by_kind = Counter()
    for _, _, src, _, _ in all_found:
        for pat, label in BLOCKING:
            if re.compile(pat).search(src):
                by_kind[label] += 1
                break

    print(f"файлов просканировано: {len(files)} | мест с синхронным I/O в async: {len(all_found)}")
    print()
    print(f"уникальных мест (файл+функция+вызов без литералов): {len(unique)}")
    print()
    print("по файлам:")
    for f, n in by_file.most_common(20):
        print(f"  {n:3}  {f}")
    print()
    print("по видам вызовов:")
    for k, n in by_kind.most_common(20):
        print(f"  {n:3}  {k}")
    print()
    print("горячие эндпоинты (в порядке частоты в UI):")
    hot = [x for x in all_found if HOT_ENDPOINTS.search(x[3])]
    for path, line, src, func, _ in hot[:40]:
        print(f"  {path.relative_to(root)}:{line}  {func}() → {src}")
    if len(hot) > 40:
        print(f"  … ещё {len(hot) - 40}")
    print()
    detail_file = sys.argv[2] if len(sys.argv) > 2 else "upload.py"
    print(f"детально по {detail_file}:")
    for path, line, src, func, _ in all_found:
        if path.name == detail_file:
            print(f"  {line:5}  {func:28} {src[:70]}")
    return all_found


if __name__ == "__main__":
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "src")
    main(root)
