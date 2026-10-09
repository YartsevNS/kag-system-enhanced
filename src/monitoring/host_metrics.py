"""Метрики хоста и хранилища: съём фактических значений утилит, без вычислений.

Зачем: страницы системы должны показывать то, что реально отдают утилиты (`df`, `du`, `docker system df`),
а динамику — Grafana по истории Prometheus. Здесь только съём «как есть»: байты, размеры, монтирования.
Ни процентов, ни агрегатов — их считает Grafana на своей стороне (история, средние, максимумы).

Почему командой, а не через библиотеку: значения должны совпадать с тем, что видит администратор
в терминале на том же стенде. Один источник — одна правда, и расхождений не бывает.

Как работает: фоновая задача `host_metrics_loop` (раз в 60 с) снимает значения и обновляет датчики
Prometheus. Снимает всегда, независимо от того, открыта ли страница: тогда при заходе в интерфейс
данные уже есть, а история (для графиков) не имеет дыр.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from typing import Dict, List

from loguru import logger
from prometheus_client import Gauge

# ── Датчики (значения обновляются съёмом, ничего не вычисляем) ───────────────
FS_SIZE = Gauge("kag_fs_size_bytes", "Размер файловой системы, байт (df)", ["mount"])
FS_USED = Gauge("kag_fs_used_bytes", "Занято на файловой системе, байт (df)", ["mount"])
FS_FREE = Gauge("kag_fs_free_bytes", "Свободно на файловой системе, байт (df)", ["mount"])
DIR_BYTES = Gauge("kag_dir_bytes", "Размер каталога, байт (du)", ["path"])
DOCKER_BYTES = Gauge("kag_docker_bytes", "Размер в Docker, байт (docker system df)", ["kind"])
HOST_COLLECT_ERRORS = Gauge("kag_host_collect_errors_total",
                            "Сбои съёма показателей хранилища (растёт — смотреть причину)", ["source"])

DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
TRACKED_DIRS = [
    (f"{DATA_DIR}", "данные (всё)"),
    (f"{DATA_DIR}/uploads", "файлы документов"),
    (f"{DATA_DIR}/thumbnails", "миниатюры"),
    (f"{DATA_DIR}/ocr_results", "результаты распознавания"),
]


# ── Съём: команды, значения как есть ─────────────────────────────────────────

def read_filesystems() -> List[Dict[str, int | str]]:
    """`df -B1 -P` — размер/занято/свободно в байтах, плюс точка монтирования.

    Без `-x` фильтров: всё, что смонтировано, должно быть видно (в контейнере это, в частности,
    bind-каталоги стенда, и по ним видно ФС хоста).
    """
    out = _run(["df", "-B1", "-P"])
    rows: List[Dict[str, int | str]] = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        fs, size, used, avail, _pct, mount = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5]
        try:
            rows.append({"fs": fs, "mount": mount, "size": int(size),
                         "used": int(used), "free": int(avail)})
        except ValueError:
            continue
    return rows


def read_dir_bytes(path: str) -> int:
    """`du -sb <каталог>` — суммарный размер в байтах, как в терминале."""
    out = _run(["du", "-sb", path])
    if not out:
        return -1
    try:
        return int(out.split()[0])
    except (ValueError, IndexError):
        return -1


def read_docker_bytes() -> Dict[str, int]:
    """Размеры Docker через SDK (`docker system df` теми же числами, байты).

    CLI `docker` в образе api нет, поэтому берём данные SDK — это тот же расчёт, что делает CLI.
    """
    result: Dict[str, int] = {}
    try:
        from src.api.services.docker_monitor import docker_monitor
        client = docker_monitor._ensure_client() or getattr(docker_monitor, "_client", None)
        if client is None:
            return result
        df = client.df()
        images = df.get("Images") or []
        result["images"] = sum(int(i.get("Size") or 0) for i in images)
        result["images_shared"] = sum(int(i.get("SharedSize") or 0) for i in images)
        containers = df.get("Containers") or []
        result["containers"] = sum(int(c.get("SizeRw") or 0) for c in containers)
        volumes = df.get("Volumes") or []
        result["volumes"] = sum(int(v.get("UsageData", {}).get("Size") or 0) for v in volumes)
    except Exception as e:
        logger.debug(f"съём размеров Docker не удался: {e}")
        HOST_COLLECT_ERRORS.labels(source="docker").inc()
    return result


def collect_once() -> None:
    """Один съём: обновляем датчики. Никогда не бросает — метрика не имеет права ломать сервис."""
    try:
        for row in read_filesystems():
            mount = str(row["mount"])
            FS_SIZE.labels(mount=mount).set(row["size"])
            FS_USED.labels(mount=mount).set(row["used"])
            FS_FREE.labels(mount=mount).set(row["free"])
    except Exception as e:
        logger.debug(f"съём файловых систем не удался: {e}")
        HOST_COLLECT_ERRORS.labels(source="df").inc()

    for path, _label in TRACKED_DIRS:
        if not os.path.isdir(path):
            continue
        size = read_dir_bytes(path)
        if size >= 0:
            DIR_BYTES.labels(path=path).set(size)

    for kind, value in read_docker_bytes().items():
        DOCKER_BYTES.labels(kind=kind).set(value)


async def host_metrics_loop(interval_s: int = 60) -> None:
    """Фоновый съём раз в минуту: страницы открываются с уже готовыми данными, история без дыр."""
    while True:
        try:
            await asyncio.to_thread(collect_once)
        except Exception as e:
            logger.warning(f"Съём показателей хранилища: {type(e).__name__}: {e}")
        await asyncio.sleep(interval_s)


def _run(cmd: List[str], timeout: float = 20.0) -> str:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return res.stdout if res.returncode == 0 else ""
    except Exception:
        return ""
