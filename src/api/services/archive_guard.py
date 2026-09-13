"""Безопасный разбор архивов (zip/tar) при массовой загрузке.

Закрывает три вектора:

* **zip-slip / tar-slip** — имя члена архива вида `../../etc/passwd` приводит к
  записи за пределами каталога распаковки. `zipfile.extract` с Python 3.6 сам
  нормализует `..` внутри архива, `tarfile.extract` — НЕ защищает: он пишет по
  указанному пути как есть.
* **symlink/hardlink-атака** — член архива может быть ссылкой на `/etc`, и
  следующий член пишется «сквозь» неё (`tarfile.data_filter` есть только с
  Python 3.12, у нас 3.11 — поэтому проверяем вручную).
* **ZIP-бомба** — один небольшой архив, распаковывающийся в десятки гигабайт
  или в миллион файлов (лимит на размер входного архива такой сценарий не ловит).

Функции чистые (без HTTP и без файловой системы, кроме resolve пути), чтобы
проверяться юнит-тестами: решение «принять/отклонить» принимает вызывающий код.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

# Лимиты по умолчанию (переопределяются настройками приложения)
MAX_ARCHIVE_ENTRIES = 1000
MAX_ARCHIVE_UNCOMPRESSED = 2 * 1024 * 1024 * 1024   # 2 ГБ распакованного
MAX_COMPRESSION_RATIO = 100                          # распакованное / сжатое


class ArchiveRejected(Exception):
    """Архив отклонён: небезопасные имена, ссылки или превышены лимиты."""

    def __init__(self, reason: str, code: str = "ARCHIVE_REJECTED"):
        super().__init__(reason)
        self.reason = reason
        self.code = code


def safe_target(extract_dir: Path, name: str) -> Optional[Path]:
    """Путь для распаковки ВНУТРИ extract_dir или None, если имя небезопасно.

    Отклоняем: абсолютные пути, выход за каталог (`..`), пустые имена и любые
    имена, которые после resolve() оказались вне базового каталога.
    """
    if not name or name.strip() in ("", ".", ".."):
        return None
    if os.path.isabs(name) or name.startswith(("/", "\\")):
        return None
    base = extract_dir.resolve()
    target = (base / name).resolve()
    if target == base:
        return None
    if not str(target).startswith(str(base) + os.sep):
        return None
    return target


def check_limits(
    entries: Iterable[Tuple[str, int]],
    archive_size: int,
    max_entries: int = MAX_ARCHIVE_ENTRIES,
    max_uncompressed: int = MAX_ARCHIVE_UNCOMPRESSED,
    max_ratio: int = MAX_COMPRESSION_RATIO,
) -> Tuple[int, int]:
    """Проверить лимиты по списку (имя, размер_распакованного).

    Возвращает (число записей, суммарный распакованный размер).
    Бросает ArchiveRejected с понятной причиной.
    """
    items = list(entries)
    if not items:
        raise ArchiveRejected("Архив пуст", "ARCHIVE_EMPTY")
    if len(items) > max_entries:
        raise ArchiveRejected(
            f"Слишком много файлов в архиве: {len(items)} (максимум {max_entries})",
            "ARCHIVE_TOO_MANY_ENTRIES",
        )
    total = sum(max(0, int(size)) for _, size in items)
    if total > max_uncompressed:
        raise ArchiveRejected(
            f"Архив слишком большой в распакованном виде: {total} байт "
            f"(максимум {max_uncompressed})",
            "ARCHIVE_TOO_LARGE",
        )
    # Защита от бомбы: маленький архив, распаковывающийся в десятки раз больше.
    if archive_size > 0 and total > archive_size * max_ratio:
        raise ArchiveRejected(
            f"Подозрительное соотношение сжатия: {total} распакованных при "
            f"{archive_size} сжатых (лимит x{max_ratio})",
            "ARCHIVE_RATIO",
        )
    return len(items), total


def check_member_names(names: Iterable[str], extract_dir: Path) -> List[str]:
    """Проверить имена членов архива; вернуть список безопасных имён.

    Все небезопасные имена собираются и отклоняются разом: так в логе видно, что
    именно пытались записать, а не только первый плохой элемент.
    """
    bad = [n for n in names if safe_target(extract_dir, n) is None]
    if bad:
        sample = ", ".join(repr(n) for n in bad[:5])
        more = f" и ещё {len(bad) - 5}" if len(bad) > 5 else ""
        raise ArchiveRejected(
            f"Небезопасные имена в архиве (запись за пределы каталога): {sample}{more}",
            "ARCHIVE_UNSAFE_PATH",
        )
    return list(names)


def is_link_or_special(member) -> bool:
    """Ссылка или спецфайл (символическая/жёсткая ссылка, устройство, fifo)."""
    try:
        return bool(
            member.issym() or member.islnk() or member.ischr()
            or member.isblk() or member.isfifo() or member.isdev()
        )
    except Exception:
        return False


def check_tar_members(members, extract_dir: Path) -> None:
    """Проверить членов tar: пути, ссылки, спецфайлы, каталоги."""
    links = [m.name for m in members if is_link_or_special(m)]
    if links:
        sample = ", ".join(repr(n) for n in links[:5])
        raise ArchiveRejected(
            f"Ссылки и спецфайлы в архиве запрещены: {sample}",
            "ARCHIVE_LINKS",
        )
    check_member_names([m.name for m in members], extract_dir)
