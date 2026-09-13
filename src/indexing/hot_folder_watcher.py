"""
Watchdog для hot folder — автоматическая обработка файлов из /app/data/hot/.

При появлении нового файла в hot/ он автоматически загружается
в document_service и отправляется в Celery на обработку.

Использует inotify (watchdog) — не поллинг, почти零 нагрузки.
"""

import os
import asyncio
import uuid
from pathlib import Path
from loguru import logger

HOT_DIR = Path("/app/data/hot")
POLL_INTERVAL = 5  # секунд между проверками (если inotify не сработал)


def _list_hot_files() -> list:
    """Имена файлов в hot-каталоге (синхронно, вызывается через to_thread)."""
    try:
        if not HOT_DIR.exists():
            return []
        return [f.name for f in HOT_DIR.iterdir() if f.is_file()]
    except OSError:
        return []


def _read_file_bytes(path) -> bytes:
    """Прочитать файл (синхронно, вызывается через to_thread)."""
    with open(path, "rb") as f:
        return f.read()


class HotFolderWatcher:
    """
    Наблюдатель за hot-директорией.

    Работает в двух режимах:
    1. Inotify (watchdog.Observer) — мгновенное обнаружение
    2. Polling (каждые 5 секунд) — fallback если inotify не сработал
    """

    def __init__(self):
        self._running = False
        self._task: asyncio.Task = None
        self._observer = None
        self._seen_files: set = set()

    async def start(self):
        """Запустить наблюдение (в фоновом asyncio task)."""
        if self._running:
            return

        self._running = True

        # Создаём hot директорию если нет. ФС — в потоке: watcher живёт в
        # процессе API, синхронные вызовы в его цикле блокируют event loop.
        await asyncio.to_thread(HOT_DIR.mkdir, parents=True, exist_ok=True)

        # Помечаем уже существующие файлы как "виденные"
        self._seen_files.update(await asyncio.to_thread(_list_hot_files))

        logger.info(f"📁 HotFolderWatcher: запущен, директория: {HOT_DIR}")

        # Запускаем polling в фоне
        self._task = asyncio.create_task(self._poll_loop())

        # Пробуем inotify (watchdog)
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler

            class HotHandler(FileSystemEventHandler):
                def __init__(self, watcher):
                    self.watcher = watcher

                def on_created(self, event):
                    if not event.is_directory:
                        asyncio.run_coroutine_threadsafe(
                            self.watcher._process_file(Path(event.src_path)),
                            asyncio.get_event_loop(),
                        )

            self._observer = Observer()
            self._observer.schedule(HotHandler(self), str(HOT_DIR), recursive=False)
            self._observer.start()
            logger.info("📁 HotFolderWatcher: inotify активен")
        except Exception as e:
            logger.info(f"📁 HotFolderWatcher: inotify недоступен ({e}), используется polling")

    async def stop(self):
        """Остановить наблюдение."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._observer:
            self._observer.stop()
            self._observer.join()
        logger.info("📁 HotFolderWatcher: остановлен")

    async def _poll_loop(self):
        """Polling — проверка новых файлов каждые N секунд."""
        while self._running:
            try:
                # Список файлов — в потоке (это цикл polling, а не разовая работа).
                names = await asyncio.to_thread(_list_hot_files)
                for name in names:
                    if name not in self._seen_files:
                        self._seen_files.add(name)
                        # Небольшая задержка чтобы файл дописался
                        await asyncio.sleep(1)
                        await self._process_file(HOT_DIR / name)
            except Exception as e:
                logger.warning(f"HotFolderWatcher poll error: {e}")
            await asyncio.sleep(POLL_INTERVAL)

    async def _process_file(self, file_path: Path):
        """Обработать новый файл из hot-директории."""
        upload_id = str(uuid.uuid4())
        filename = file_path.name
        logger.info(f"[HotFolder] 📥 Новый файл: {filename}")

        try:
            # Ждём пока файл допишется (проверяем стабильность размера)
            for _ in range(5):
                size1 = await asyncio.to_thread(lambda: file_path.stat().st_size)
                await asyncio.sleep(0.5)
                size2 = await asyncio.to_thread(lambda: file_path.stat().st_size)
                if size1 == size2:
                    break

            # Читаем файл (в потоке: файлы бывают крупные)
            content = await asyncio.to_thread(_read_file_bytes, file_path)

            if not content:
                logger.warning(f"[HotFolder] Пустой файл: {filename}")
                return

            # Определяем тип по расширению
            ext = file_path.suffix.lower()
            mime_map = {
                ".pdf": "application/pdf",
                ".txt": "text/plain",
                ".md": "text/markdown",
                ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ".csv": "text/csv",
                ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
            }
            file_type = mime_map.get(ext, "application/octet-stream")

            # Валидация
            from src.security.validator import SecurityValidator, SecurityValidationError
            SecurityValidator.validate_file_upload(
                file_path="", filename=filename,
                file_size=len(content), mime_type=file_type,
            )

            # Загружаем в document_service
            from src.api.services.document_service import document_service
            record = await document_service.upload_document(
                filename=filename, file_content=content,
                file_type=file_type, upload_id=upload_id,
            )

            # Отправляем в Celery
            from src.indexing.tasks import process_document as celery_task
            celery_task.delay(document_id=record.document_id)

            logger.info(f"[HotFolder] ✅ Файл обработан: {filename} → {record.document_id[:12]}")

        except Exception as e:
            logger.error(f"[HotFolder] ❌ Ошибка обработки {filename}: {e}")


# Глобальный экземпляр
hot_folder_watcher = HotFolderWatcher()
