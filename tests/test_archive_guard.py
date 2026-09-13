"""Защита архивов: zip/tar-slip, ссылки, ZIP-бомбы.

Юниты на archive_guard + реальные архивы, собранные в памяти: именно так
проверяется, что отказ происходит ДО записи на диск.
"""
import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from src.api.services.archive_guard import (
    ArchiveRejected,
    check_limits,
    check_member_names,
    check_tar_members,
    is_link_or_special,
    safe_target,
)

TMP = Path("/tmp/archive-test")


def test_safe_target_allows_normal_names():
    assert safe_target(TMP, "docs/report.pdf") == (TMP / "docs/report.pdf").resolve()


@pytest.mark.parametrize("name", ["../evil.txt", "../../etc/passwd", "/etc/passwd", "", ".", ".."])
def test_safe_target_rejects_traversal(name):
    assert safe_target(TMP, name) is None


def test_safe_target_rejects_sibling_by_prefix():
    """`/tmp/archive-test-2` не должен считаться «внутри» `/tmp/archive-test`."""
    assert safe_target(TMP, "../archive-test-2/steal.txt") is None


def test_check_limits_rejects_too_many_entries():
    entries = [(f"f{i}.txt", 10) for i in range(1001)]
    with pytest.raises(ArchiveRejected) as e:
        check_limits(entries, archive_size=100_000, max_entries=1000)
    assert e.value.code == "ARCHIVE_TOO_MANY_ENTRIES"


def test_check_limits_rejects_too_large_total():
    entries = [("big.bin", 3 * 1024 ** 3)]
    with pytest.raises(ArchiveRejected) as e:
        check_limits(entries, archive_size=10_000, max_uncompressed=2 * 1024 ** 3)
    assert e.value.code == "ARCHIVE_TOO_LARGE"


def test_check_limits_rejects_bomb_ratio():
    entries = [("zeros.bin", 100 * 1024 * 1024)]     # 100 МБ нулей
    with pytest.raises(ArchiveRejected) as e:
        check_limits(entries, archive_size=200 * 1024, max_ratio=100)   # сжато 200 КБ
    assert e.value.code == "ARCHIVE_RATIO"


def test_check_limits_accepts_normal_archive():
    entries = [("a.txt", 100), ("b.txt", 200)]
    count, total = check_limits(entries, archive_size=500)
    assert (count, total) == (2, 300)


def test_check_limits_rejects_empty_archive():
    with pytest.raises(ArchiveRejected) as e:
        check_limits([], archive_size=10)
    assert e.value.code == "ARCHIVE_EMPTY"


def test_check_member_names_reports_all_bad_names():
    with pytest.raises(ArchiveRejected) as e:
        check_member_names(["ok.txt", "../evil1.txt", "../evil2.txt"], TMP)
    assert e.value.code == "ARCHIVE_UNSAFE_PATH"
    assert "evil1" in e.value.reason and "evil2" in e.value.reason


def test_zip_with_traversal_name_is_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(zipfile.ZipInfo("../evil.txt"), "pwned")
    data = buf.getvalue()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        infos = zf.infolist()
        check_limits([(i.filename, i.file_size) for i in infos], len(data))
        with pytest.raises(ArchiveRejected) as e:
            check_member_names([i.filename for i in infos], TMP)
    assert e.value.code == "ARCHIVE_UNSAFE_PATH"


def test_tar_with_traversal_name_is_rejected():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo("../evil.txt")
        payload = b"pwned"
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
    data = buf.getvalue()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tf:
        members = tf.getmembers()
        with pytest.raises(ArchiveRejected) as e:
            check_tar_members(members, TMP)
    assert e.value.code == "ARCHIVE_UNSAFE_PATH"


def test_tar_symlink_is_rejected():
    """symlink на /etc + запись «сквозь» него — классическая tar-атака."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        link = tarfile.TarInfo("link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc"
        tf.addfile(link)
        inside = tarfile.TarInfo("link/passwd")
        payload = b"x"
        inside.size = len(payload)
        tf.addfile(inside, io.BytesIO(payload))
    with tarfile.open(fileobj=io.BytesIO(buf.getvalue()), mode="r:") as tf:
        members = tf.getmembers()
        assert is_link_or_special(members[0])
        with pytest.raises(ArchiveRejected) as e:
            check_tar_members(members, TMP)
    assert e.value.code == "ARCHIVE_LINKS"


def test_zip_bomb_entry_count_is_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i in range(2000):
            zf.writestr(f"f{i}.txt", "x")
    data = buf.getvalue()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        infos = zf.infolist()
        with pytest.raises(ArchiveRejected) as e:
            check_limits([(i.filename, i.file_size) for i in infos], len(data),
                         max_entries=1000)
    assert e.value.code == "ARCHIVE_TOO_MANY_ENTRIES"


def test_zip_bomb_ratio_is_rejected():
    """10 МБ нулей сжимаются в считаные килобайты — соотношение выдаёт бомбу."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("zeros.bin", b"\0" * (10 * 1024 * 1024))
    data = buf.getvalue()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        infos = zf.infolist()
        with pytest.raises(ArchiveRejected) as e:
            check_limits([(i.filename, i.file_size) for i in infos], len(data),
                         max_uncompressed=2 * 1024 ** 3, max_ratio=100)
    assert e.value.code == "ARCHIVE_RATIO"


def test_normal_zip_passes_all_checks():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in ("a.txt", "sub/b.txt", "sub/c.md"):
            zf.writestr(name, "содержимое " * 20)
    data = buf.getvalue()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        infos = zf.infolist()
        count, total = check_limits([(i.filename, i.file_size) for i in infos], len(data))
        names = check_member_names([i.filename for i in infos], TMP)
    assert count == 3 and total > 0
    assert names == ["a.txt", "sub/b.txt", "sub/c.md"]
