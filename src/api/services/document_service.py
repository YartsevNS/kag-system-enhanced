"""
Document Service для KAG

Отвечает за:
- Загрузку документов (с хешированием SHA-256 для контроля дубликатов)
- Парсинг и чанкинг
- Векторизацию через Embeddings
- Сохранение в Qdrant
- Отслеживание статуса обработки
- Версионность: хранение оригиналов и бэкапов при замене
- Дедупликацию по хешу: одинаковый файл → предложение заменить
"""

from typing import Dict, Any, List, Optional
from pathlib import Path
import uuid
import time
import os
import hashlib
import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from loguru import logger
from pydantic import BaseModel, Field

from src.indexing.parsers import document_parser, text_chunker
from src.indexing.embeddings_service import embeddings_service
from src.config import get_settings


class DocumentRecord(BaseModel):
    """Запись о документе"""
    document_id: str = Field(..., description="ID документа")
    filename: str = Field(..., description="Имя файла")
    file_type: str = Field(default="application/octet-stream", description="Тип файла (MIME)")
    file_size: int = Field(default=0, description="Размер файла")
    status: str = Field(default="pending", description="Статус: pending, processing, completed, failed")
    progress: float = Field(default=0.0, description="Прогресс обработки (0-100)")
    chunks_count: int = Field(default=0, description="Количество чанков")
    error: Optional[str] = Field(default=None, description="Ошибка если есть")
    uploaded_by: Optional[str] = Field(default=None, description="ID пользователя")
    group_ids: Optional[List[str]] = Field(default=None, description="ID групп")
    # Классификация (заполняется анализатором)
    document_type: Optional[str] = Field(default=None, description="Тип: contract, invoice, report...")
    recognized_title: Optional[str] = Field(default=None, description="Распознанное название")
    summary: Optional[str] = Field(default=None, description="Краткое описание")
    topics: Optional[List[str]] = Field(default=None, description="Ключевые темы")
    # Контроль дубликатов и версионность
    file_hash: Optional[str] = Field(default=None, description="SHA-256 хеш содержимого файла")
    version: int = Field(default=1, description="Версия документа (1 = оригинал)")
    previous_hash: Optional[str] = Field(default=None, description="Хеш предыдущей версии (если была замена)")
    original_text: Optional[str] = Field(default=None, description="Извлечённый текст оригинала для сравнения версий")
    source_metadata: Optional[dict] = Field(default=None, description="Метаданные источника (doc_type, doc_number, doc_title, download_url)")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class DocumentService:
    """
    Сервис обработки документов.

    Полный pipeline:
    1. Сохранение файла
    2. Парсинг
    3. Чанкинг
    4. Векторизация
    5. Сохранение в Qdrant

    Метаданные документов хранятся в PostgreSQL через DocumentRepository (SQL).
    """

    def __init__(self, upload_dir: Optional[str] = None):
        """
        Инициализация сервиса.

        Args:
            upload_dir: Директория для загруженных файлов
        """
        settings = get_settings()
        
        # Семафор для последовательной обработки (1 документ за раз)
        self._processing_lock = asyncio.Semaphore(1)
        
        # Используем /app/data/uploads (принадлежит kag, persistent)
        upload_base = Path("/app/data")
        self._upload_dir = upload_base / "uploads"
        self._ocr_dir = upload_base / "ocr_results"
        self._thumb_dir = upload_base / "thumbnails"

        for d in [self._upload_dir, self._ocr_dir, self._thumb_dir]:
            try:
                d.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
        
        try:
            self._upload_dir.mkdir(parents=True, exist_ok=True)
            # Проверяем доступность на запись
            test_file = self._upload_dir / ".test"
            test_file.write_text("test")
            test_file.unlink()
        except Exception:
            logger.warning("data/uploads недоступен, использую /tmp")
            self._upload_dir = Path("/tmp/kag_uploads")
            self._upload_dir.mkdir(parents=True, exist_ok=True)

        # Кэш метаданных (загружается из БД при старте)
        self._documents: Dict[str, DocumentRecord] = {}

        # Загружаем метаданные из БД
        self._load_documents_from_db()

        logger.info(f"DocumentService инициализирован: {self._upload_dir}, документов в кэше: {len(self._documents)}")
        
        # Автоочистка: удаляем записи без файлов на диске
        self._cleanup_stale_records()

    def _cleanup_stale_records(self):
        """Удалить записи в БД, для которых нет файлов на диске."""
        try:
            existing = set()
            if self._upload_dir.exists():
                for f in self._upload_dir.iterdir():
                    if f.is_file():
                        existing.add(f.name[:36])
            
            from src.api.services.document_repository import get_doc_repo
            stale = [did for did in self._documents if did not in existing]
            for did in stale:
                fname = self._documents[did].filename if did in self._documents else '?'
                get_doc_repo().delete(did)
                del self._documents[did]
            
            if stale:
                logger.info(f"Автоочистка: удалено {len(stale)} stale-записей без файлов")
        except Exception as e:
            logger.warning(f"Автоочистка не выполнена: {e}")

    def _load_documents_from_db(self):
        """Загрузить метаданные документов из SQL (DocumentRepository)"""
        try:
            from src.api.services.document_repository import get_doc_repo
            repo = get_doc_repo()
            docs, _ = repo.list(limit=10000)
            for doc in docs:
                try:
                    record = DocumentRecord(
                        document_id=doc.id,
                        filename=doc.filename or "unknown",
                        file_hash=doc.file_hash or "",
                        status=doc.status or "pending",
                        progress=doc.progress or 0,
                        chunks_count=doc.chunks_count or 0,
                        file_size=doc.file_size or 0,
                        version=doc.version or 1,
                    )
                    self._documents[doc.id] = record
                except Exception as e:
                    logger.warning(f"Ошибка загрузки документа {doc.id}: {e}")

            if self._documents:
                logger.info(f"Загружено {len(self._documents)} документов из БД")
        except Exception as e:
            logger.debug(f"БД недоступна, использую пустой кэш: {e}")

    def _save_document_to_db(self, document_id: str):
        """Сохранить метаданные документа в SQL (не блокирует обработку)"""
        try:
            from src.api.services.document_repository import get_doc_repo
            record = self._documents.get(document_id)
            if not record:
                return
            data = record.model_dump()
            get_doc_repo().upsert(document_id, data)
        except Exception as e:
            logger.debug(f"БД недоступна, пропускаю сохранение: {e}")

    async def upload_document(
        self,
        filename: str,
        file_content: bytes,
        file_type: Optional[str] = None,
        uploaded_by: Optional[str] = None,
        group_ids: Optional[List[str]] = None,
        force_new: bool = False,
        upload_id: Optional[str] = None,
        source_metadata: Optional[dict] = None
    ) -> DocumentRecord:
        """
        Загрузить документ с контролем дубликатов и версионностью.

        Алгоритм:
        1. SHA-256 хеш содержимого (быстро, в памяти)
        2. Если хеш совпадает с существующим — возвращаем существующий (не дублируем)
        3. Если force_new — бэкапим старую версию
        4. Сохраняем файл на диск: /app/data/uploads/{doc_id}_{filename}
        5. Создаём миниатюру

        Args:
            filename: Имя файла (оригинальное)
            file_content: Содержимое файла (байты)
            file_type: MIME тип (опционально)
            uploaded_by: ID пользователя
            group_ids: Список group_id для RBAC
            force_new: Принудительно создать новый документ
            upload_id: UUID загрузки (для логов)

        Returns:
            DocumentRecord
        """
        # ========== Этап 0: санитизация имени файла (защита от path traversal) ==========
        # Берём только basename, отбрасываем любые path-компоненты и null-байты.
        sanitized = Path(filename).name.replace("\x00", "").strip()
        if not sanitized or sanitized in (".", ".."):
            sanitized = f"document_{uuid.uuid4().hex[:8]}{Path(filename).suffix}"
        filename = sanitized

        # ========== Этап 1: вычисляем SHA-256 хеш содержимого ==========
        file_hash = hashlib.sha256(file_content).hexdigest()
        file_size = len(file_content)
        logger.debug(
            f"[{upload_id or '-'}] Хеш: {file_hash[:16]}..., размер: {file_size} байт"
        )

        # ========== Этап 2: проверяем дубликаты по хешу ==========
        if not force_new:
            existing = self._find_by_hash(file_hash)
            if existing:
                logger.info(
                    f"[{upload_id or '-'}] 🔁 Дубликат: {filename} "
                    f"(хеш {file_hash[:12]}...) уже есть как "
                    f"{existing.document_id[:12]} v{existing.version}"
                )
                return existing

        # ========== Этап 3: создаём запись о документе ==========
        doc_id = str(uuid.uuid4())
        version = 1
        previous_hash = None
        original_text = None
        upload_id = upload_id or doc_id  # Если upload_id не передан, используем document_id

        if force_new:
            prev = self._find_by_hash(file_hash)
            if prev:
                prev_path = self._find_file(prev.document_id, prev.filename)
                if prev_path:
                    backup_path = prev_path.with_suffix(prev_path.suffix + f'.v{prev.version}.bak')
                    try:
                        import shutil
                        shutil.copy2(prev_path, backup_path)
                        logger.info(f"[{upload_id}] 📦 Бэкап: {backup_path.name}")
                    except Exception as e:
                        logger.warning(f"[{upload_id}] Не удалось создать бэкап: {e}")
                version = prev.version + 1
                previous_hash = prev.file_hash
                original_text = prev.original_text or self._load_original_text(prev.document_id)

        # ========== Этап 4: определяем тип файла по расширению, если не передан ==========
        if not file_type:
            ext = Path(filename).suffix.lower()
            file_type = ext

        # ========== Этап 5: сохраняем файл на диск ==========
        target_path = self._upload_dir / f"{doc_id}_{filename}"
        with open(target_path, 'wb') as f:
            f.write(file_content)
        logger.info(
            f"[{upload_id}] 💾 Файл сохранён: {target_path.name} ({file_size} байт)"
        )

        # ========== Этап 5.5: создаём миниатюру (первая страница для PDF, ресайз для изображений) ==========
        self._create_thumbnail(str(target_path), doc_id, filename)

        # ========== Этап 6: создаём запись о документе в памяти ==========
        record = DocumentRecord(
            document_id=doc_id,
            filename=filename,
            file_type=file_type,
            file_size=file_size,
            file_hash=file_hash,
            version=version,
            previous_hash=previous_hash,
            original_text=original_text,
            status="pending",
            uploaded_by=uploaded_by,
            group_ids=group_ids or []
        )

        self._documents[doc_id] = record
        logger.info(
            f"[{upload_id}] ✅ Документ загружен: {doc_id[:12]} v{version} | "
            f"хеш {file_hash[:12]}... | {filename} ({file_size} байт)"
        )

        # Сохраняем метаданные в БД (хеш используется для поиска дубликатов)
        self._save_document_to_db(doc_id)

        return record

    def _find_by_hash(self, file_hash: str) -> Optional[DocumentRecord]:
        """Найти документ по SHA-256 хешу содержимого.
        
        Сначала ищем в оперативной памяти (быстро), затем в БД.
        Используется для обнаружения дубликатов при загрузке.
        """
        # Поиск в памяти
        for record in self._documents.values():
            if record.file_hash == file_hash:
                return record
        # Поиск через SQL DocumentRepository (надёжнее после рестарта)
        try:
            from src.api.services.document_repository import get_doc_repo
            doc = get_doc_repo().find_by_hash(file_hash)
            if doc:
                return DocumentRecord(
                    document_id=doc.id,
                    filename=doc.filename,
                    file_hash=file_hash,
                    version=doc.version or 1,
                    status=doc.status or "completed",
                )
        except Exception:
            pass
        return None

    def _load_original_text(self, document_id: str) -> Optional[str]:
        """Загрузить извлечённый текст оригинала документа для сравнения версий.
        
        Собирает текст всех чанков документа из Qdrant.
        """
        try:
            import asyncio
            from src.indexing.embeddings_service import embeddings_service

            async def _get():
                if embeddings_service._qdrant_client is None:
                    await embeddings_service.initialize()
                chunks = await embeddings_service.get_document_chunks(document_id)
                return "\n\n".join([c.get("content", "") for c in chunks])

            # Запускаем асинхронно (если уже в event loop) или создаём новый
            try:
                loop = asyncio.get_running_loop()
                # Уже в event loop — используем create_task
                import concurrent.futures
                future = asyncio.run_coroutine_threadsafe(_get(), loop)
                return future.result(timeout=30)
            except RuntimeError:
                # Нет event loop — создаём
                return asyncio.run(_get())
        except Exception as e:
            logger.warning(f"Не удалось загрузить текст оригинала {document_id}: {e}")
            return None

    def compare_versions(self, document_id: str) -> Dict[str, Any]:
        """Сравнить версии документа: текущую и предыдущую.
        
        Returns:
            {
                "current_hash": "...",
                "previous_hash": "...", 
                "version": N,
                "original_text": "текст предыдущей версии",
                "current_text": "текст текущей версии" (если уже обработан),
                "has_changes": True/False,
                "diff_summary": "краткое описание изменений"
            }
        """
        record = self._documents.get(document_id)
        if not record:
            return {"error": "Документ не найден"}

        result = {
            "document_id": document_id,
            "filename": record.filename,
            "version": record.version,
            "current_hash": record.file_hash,
            "previous_hash": record.previous_hash,
            "original_text": record.original_text,
            "current_text": None,
            "has_changes": False,
            "diff_summary": ""
        }

        # Загружаем текущий текст
        current_text = self._load_original_text(document_id)
        if current_text:
            result["current_text"] = current_text[:10000]  # первые 10К символов

        # Сравниваем с оригиналом
        if current_text and record.original_text:
            result["has_changes"] = current_text != record.original_text
            if result["has_changes"]:
                # Простой diff: что добавилось/удалилось
                orig_words = set(record.original_text.split())
                curr_words = set(current_text.split())
                added = curr_words - orig_words
                removed = orig_words - curr_words
                result["diff_summary"] = (
                    f"Добавлено слов: {len(added)}, "
                    f"Удалено слов: {len(removed)}, "
                    f"Изменений: {abs(len(current_text) - len(record.original_text))} символов"
                )
            else:
                result["diff_summary"] = "Текст не изменился"

        return result

    async def process_document(self, document_id: str, force: bool = False) -> Dict[str, Any]:
        """
        Обработать документ: распарсить, разбить на чанки, векторизовать.

        Args:
            document_id: ID документа
            force: True — принудительная переобработка (reindex). Перед обработкой
                удаляются старые векторы из Qdrant и старые узлы графа Neo4j,
                чтобы не оставались «осиротевшие» точки (важно при изменении
                chunk_size/overlap — число чанков меняется, и без явной очистки
                старые точки с тем же document_id висели бы в Qdrant вечно).

        Returns:
            Результат обработки
        """
        # При force-переобработке сначала снимаем старые данные (Qdrant + Neo4j).
        # Это гарантирует, что после переиндексации в хранилищах не останется
        # чанков/узлов от предыдущего прогона с другими параметрами чанкинга.
        if force:
            try:
                # ВАЖНО: сначала инициализируем клиент — delete_document при
                # _qdrant_client=None молча возвращает False (ловит AttributeError
                # внутри), и старые точки остались бы висеть. Проверяем результат.
                await embeddings_service.initialize()
                deleted = await embeddings_service.delete_document(document_id)
                if deleted:
                    logger.info(f"Удалены старые векторы из Qdrant (force): {document_id}")
                else:
                    logger.warning(f"delete_document вернул False — старые векторы могли остаться: {document_id}")
            except Exception as e:
                logger.warning(f"Не удалось удалить старые векторы из Qdrant: {e}")
            try:
                from src.indexing.knowledge_graph import kg_service
                kg_service.clear_document(document_id)
                logger.info(f"Очищен граф Neo4j (force): {document_id}")
            except Exception as e:
                logger.warning(f"Не удалось очистить граф Neo4j: {e}")

        # Последовательная обработка: только 1 документ за раз
        async with self._processing_lock:
            return await self._process_document_impl(document_id)

    async def _process_document_impl(self, document_id: str) -> Dict[str, Any]:
        """Реализация обработки (вызывается под семафором)."""
        record = self._documents.get(document_id)
        if not record:
            raise ValueError(f"Документ не найден: {document_id}")
        
        try:
            # Инициализируем логгер процесса
            from src.indexing.process_logger import ProcessLogger
            plog = ProcessLogger(document_id)
            plog.log("start", {
                "filename": record.filename,
                "file_type": record.file_type,
                "file_size": record.file_size,
                "uploaded_by": record.uploaded_by
            })
            
            # Обновляем статус
            record.status = "processing"
            record.progress = 10
            record.updated_at = datetime.utcnow()
            self._save_document_to_db(document_id)

            # Находим файл
            file_path = self._find_file(document_id, record.filename)
            if not file_path:
                plog.log_error("find_file", "Файл не найден")
                raise FileNotFoundError(f"Файл не найден для документа {document_id}")
            plog.log("find_file", {"path": str(file_path)})

            # Шаг 1: Парсинг (30%)
            logger.info(f"Парсинг документа: {document_id}")
            record.progress = 30
            self._save_document_to_db(document_id)
            
            # Парсинг: PyMuPDF (текстовый слой) → Occular-ocr (сканы) → DocumentParser.
            # PyMuPDF мгновенно извлекает текстовый слой электронных PDF
            # (без OOM); Occular — для сканов (рендер+нейросеть).
            try:
                from src.indexing.hybrid_parser import get_hybrid_parser
                hybrid = get_hybrid_parser()
                parsed = hybrid.parse_pymupdf_first(str(file_path)) or hybrid.parse_ocular_only(str(file_path))
                if not parsed:
                    raise ValueError("PyMuPDF/Occular недоступны")
                segments = []
                for page in parsed.pages:
                    if page.text and page.text.strip():
                        segments.append({
                            "type": "text",
                            "content": page.text,
                            "page": page.page_num,
                            "metadata": {}
                        })
                parsed_metadata = parsed.metadata
                parser_name = parsed.parse_method
                if not segments:
                    raise ValueError("Парсер вернул пустой результат")
                plog.log("parse", {"segments": len(segments), "parser": parser_name})
                # Сохраняем полный текст
                ocr_path = self._ocr_dir / record.filename
                ocr_path.write_text(parsed.full_text, encoding="utf-8")
                logger.info(f"OCR сохранён: {ocr_path}")
                
                # Сохраняем Markdown-версию (с таблицами и структурой)
                try:
                    md_text = parsed.to_markdown()
                    md_path = self._ocr_dir / f"{record.filename}.md"
                    md_path.write_text(md_text, encoding="utf-8")
                    logger.info(f"Markdown сохранён: {md_path} ({len(md_text)} симв)")
                except Exception as e:
                    logger.warning(f"Markdown не создан: {e}")
            except Exception as e:
                logger.warning(f"Occular-ocr failed ({e}), fallback to DocumentParser")
                from src.indexing.parsers import document_parser
                parsed_doc = document_parser.parse(str(file_path), record.file_type)
                segments = parsed_doc.get("segments", [])
                parsed_metadata = parsed_doc.get("metadata", {})
                parser_name = "DocumentParser"
                plog.log("parse", {"segments": len(segments), "parser": parser_name})

            # Шаг 1.5: Суммаризация (опционально, 40%)
            summarization_enabled = False
            try:
                ocr_cfg = config_store.get("ocr", "settings") or {}
                summarization_enabled = ocr_cfg.get("enable_summarization", False)
            except Exception:
                pass
            
            if summarization_enabled and parsed_text:
                try:
                    logger.info(f"Суммаризация документа: {document_id}")
                    record.progress = 40
                    self._save_document_to_db(document_id)
                    summary = await self._summarize_text(parsed_text, record.filename)
                    if summary:
                        record.summary = summary
                        plog.log("summarize", {"summary_length": len(summary)})
                        logger.info(f"Суммаризация выполнена: {len(summary)} симв")
                except Exception as e:
                    logger.warning(f"Суммаризация не удалась: {e}")

            # ── Типизация (эвристика, БЕЗ LLM — бесплатно) ──────────────
            # Раньше тип определялся отдельным процессом (type_watchdog — LLM
            # батч) или на лету при просмотре /details (auto-tagger, не
            # сохранялся). Теперь — сразу при обработке: regex-эвристика
            # (мгновенно, без LLM-запросов), результат сохраняется в БД и
            # попадает в payload чанков Qdrant. LLM-уточнение (type_watchdog)
            # остаётся только для сложных случаев по кнопке.
            if not getattr(record, 'document_type', None) or record.document_type in ('unknown', '', 'pending'):
                try:
                    parsed_text_full = parsed.full_text if hasattr(parsed, 'full_text') else parsed_text
                    from src.indexing.auto_tagger import get_auto_tagger
                    classification = get_auto_tagger().classify(
                        (parsed_text_full or parsed_text or "")[:5000], record.filename)
                    if classification.confidence > 0.3 and classification.document_type:
                        record.document_type = classification.document_type.value
                        logger.info(f"Тип определён эвристикой: {record.document_type} (conf {classification.confidence:.2f})")
                except Exception as e:
                    logger.warning(f"Типизация эвристикой не удалась: {e}")

            # Шаг 2: Чанкинг (50%)
            logger.info(f"Чанкинг документа: {document_id}")
            record.progress = 50
            self._save_document_to_db(document_id)
            
            # Загружаем настройки чанкинга из Redis (или используем default)
            from src.api.services.config_store import config_store
            chunking_config = config_store.get("chunking", "default", {
                "chunk_size": 1000,
                "chunk_overlap": 200
            })
            
            # Создаём чанкер с настройками из Redis
            from src.indexing.parsers import TextChunker
            chunker = TextChunker(
                chunk_size=chunking_config.get("chunk_size", 1000),
                chunk_overlap=chunking_config.get("chunk_overlap", 200)
            )
            
            logger.info(f"Чанкинг (из Redis): размер={chunking_config.get('chunk_size')}, перекрытие={chunking_config.get('chunk_overlap')}")
            
            chunks = chunker.chunk_document(segments)
            plog.log("chunking", {
                "chunk_size": chunking_config.get("chunk_size", 1000),
                "chunk_overlap": chunking_config.get("chunk_overlap", 200),
                "chunks_count": len(chunks),
                "total_chars": sum(len(c.get("content", "")) for c in chunks)
            })
            
            # Шаг 3: Векторизация и сохранение в Qdrant (90%)
            logger.info(f"Векторизация документа: {document_id}")
            record.progress = 90
            
            # Инициализируем embeddings сервис
            await embeddings_service.initialize()
            
            vectors_count = await embeddings_service.embed_and_store(
                document_id=document_id,
                chunks=chunks,
                metadata={
                    "filename": record.filename,
                    "file_type": record.file_type,
                    "file_size": record.file_size,
                    "document_type": getattr(record, 'document_type', '') or "unknown",
                    **parsed_metadata,
                    "source_metadata": record.source_metadata or {},
                },
                group_ids=record.group_ids
            )
            # Реальная модель и размерность из активного embedding-клиента (не хардкод)
            _emb_client = getattr(embeddings_service, "_embedding_client", None)
            plog.log("vectorize", {
                "vectors_stored": vectors_count,
                "embedding_model": getattr(_emb_client, "model", "unknown"),
                "dimensions": getattr(_emb_client, "_dimensions", None) or 0
            })

            # Генерируем миниатюру
            try:
                self._generate_thumbnail(document_id, file_path, getattr(record, 'document_type', '') or '')
            except Exception as e:
                logger.warning(f"Миниатюра не создана: {e}")

            # Шаг 4: Анализ первого чанка (типизация, title, summary).
            # ВАЖНО: await, а не create_task — в Celery process_document выполняется
            # внутри asyncio.run(), и create_task-задачи отменяются при его завершении,
            # поэтому типизация молча не выполнялась.
            # Метка тайминга: plog.log("analyze", ...) фиксирует длительность LLM-вызова
            # типизации — это один из кандидатов на «медленное» место (внешний Ollama).
            # Перенесён ПЕРЕД plog.log("completed")/plog.save(), чтобы метка успела
            # попасть в сохранённый лог (иначе save() вызвался бы раньше analyze).
            if chunks and len(chunks) > 0:
                try:
                    first_text = chunks[0].get("content", "")
                    _t_analyze = time.monotonic()
                    await self._analyze_document_async(
                        document_id, first_text, record.filename
                    )
                    plog.log("analyze", {
                        "duration_ms": round((time.monotonic() - _t_analyze) * 1000, 1)
                    })
                except Exception as e:
                    logger.debug(f"Не удалось выполнить анализ: {e}")

            # Шаг 5: Граф знаний — документ + чанки + извлечение сущностей.
            # ВАЖНО: await, а не create_task — в Celery process_document выполняется
            # внутри asyncio.run(), и create_task-задачи отменяются при его завершении,
            # поэтому граф молча не строился (Neo4j был пуст при 102 документах).
            # Внутри _build_knowledge_graph_async есть свой try/except — ошибка
            # графа не уронит завершение документа.
            try:
                await self._build_knowledge_graph_async(
                    document_id, record.filename, chunks
                )
                # Entity Resolution: слияние дубликатов сущностей после построения
                # графа (lexical + embedding + топология). Снижает дубли на 30-40%,
                # улучшает точность связей. См. docs/guides/graph-precision-architecture.md
                try:
                    from src.indexing.knowledge_graph import kg_service
                    await asyncio.to_thread(kg_service.resolve_duplicate_entities)
                    # Версии документов: связываем цепочкой SUPERSEDED_BY
                    # (старые редакции → новые), актуальная помечается is_current.
                    await asyncio.to_thread(kg_service.link_document_versions)
                except Exception as e:
                    logger.debug(f"Entity resolution пропущен: {e}")
            except Exception as e:
                logger.debug(f"Не удалось построить граф знаний: {e}")

            # Завершено (100%)
            record.status = "completed"
            record.progress = 100
            record.chunks_count = len(chunks)
            record.updated_at = datetime.utcnow()
            self._save_document_to_db(document_id)
            plog.log("completed", {
                "chunks_count": len(chunks),
                "vectors_count": vectors_count,
                "thumbnail": True
            })
            plog.save()
            
            logger.info(
                f"Документ обработан: {document_id}, "
                f"чанков: {len(chunks)}, векторов: {vectors_count}"
            )
            
            return {
                "document_id": document_id,
                "status": "completed",
                "chunks_count": len(chunks),
                "vectors_count": vectors_count,
                "filename": record.filename
            }
            
        except Exception as e:
            logger.error(f"Ошибка обработки документа {document_id}: {e}")
            import traceback
            logger.error(traceback.format_exc())
            
            record.status = "failed"
            record.error = str(e)
            record.progress = 0
            record.updated_at = datetime.utcnow()
            
            raise

    def get_document_status(self, document_id: str) -> Optional[DocumentRecord]:
        """Получить статус обработки документа"""
        return self._documents.get(document_id)

    def list_documents(self, limit: int = 100) -> List[DocumentRecord]:
        """Получить список всех документов (из БД, с обновлением кэша)"""
        self._load_documents_from_db()  # Всегда читаем свежие данные из PostgreSQL
        return list(self._documents.values())[-limit:]

    async def delete_document(self, document_id: str) -> bool:
        """
        Удалить документ и его векторы.
        
        Args:
            document_id: ID документа
            
        Returns:
            True если успешно
        """
        record = self._documents.get(document_id)
        if not record:
            return False
        
        # Удаляем файл
        file_path = self._find_file(document_id, record.filename)
        if file_path and file_path.exists():
            file_path.unlink()
        
        # Удаляем из Qdrant
        await embeddings_service.delete_document(document_id)
        
        # Удаляем файлы OCR, Markdown и миниатюры
        ocr_dir = self._ocr_dir
        for suffix in ["", ".md"]:
            ocr_path = ocr_dir / f"{record.filename}{suffix}"
            if ocr_path.exists():
                try:
                    ocr_path.unlink()
                    logger.debug(f"Удалён {ocr_path}")
                except Exception as e:
                    logger.warning(f"Не удалось удалить {ocr_path}: {e}")
        thumb_path = self._thumb_dir / f"{document_id}.webp"
        if thumb_path.exists():
            try:
                thumb_path.unlink()
            except Exception as e:
                logger.warning(f"Не удалось удалить миниатюру: {e}")
        
        # Удаляем из Neo4j (граф знаний)
        try:
            from src.indexing.knowledge_graph import kg_service
            kg_service.clear_document(document_id)
        except Exception as e:
            logger.warning(f"Не удалось удалить из Neo4j: {e}")
        
        # Удаляем запись
        del self._documents[document_id]
        
        # Отзываем Celery задачи для этого документа (если висят в очереди)
        try:
            from src.indexing.tasks import revoke_document_tasks
            revoked = revoke_document_tasks(document_id)
            if revoked:
                logger.info(f"Отозвано {revoked} Celery задач для {document_id}")
        except Exception as e:
            logger.warning(f"Не удалось отозвать задачи: {e}")

        # Удаляем из БД
        try:
            from src.api.services.document_repository import get_doc_repo
            get_doc_repo().delete(document_id)
        except Exception as e:
            logger.warning(f"Не удалось удалить документ {document_id} из БД: {e}")
        
        logger.info(f"Документ удален: {document_id}")
        return True

    def _generate_thumbnail(self, document_id: str, file_path: Path, document_type: str = "") -> Optional[Path]:
        """Сгенерировать WebP-миниатюру: первая страница PDF или текстовая карточка.
        
        Args:
            document_id: ID документа
            file_path: Путь к файлу
            document_type: Тип документа (отображается на миниатюре)
        """
        from PIL import Image, ImageDraw, ImageFont
        
        thumb_dir = Path("/app/data/thumbnails")
        thumb_dir.mkdir(parents=True, exist_ok=True)
        thumb_path = thumb_dir / f"{document_id}.webp"
        
        try:
            if file_path.suffix.lower() == '.pdf':
                import fitz
                doc = fitz.open(file_path)
                page = doc[0]
                mat = fitz.Matrix(300/72, 300/72)  # 300 DPI
                pix = page.get_pixmap(matrix=mat)
                doc.close()
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            elif file_path.suffix.lower() in ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.tiff', '.bmp'):
                img = Image.open(file_path)
                if img.mode in ('RGBA', 'P'):
                    img = img.convert('RGB')
            else:
                # Текстовые документы: генерируем текстовую карточку
                img = self._generate_text_thumbnail(file_path)
                if img is None:
                    return None
            
            # Resize to max 500px wide
            max_width = 500
            if img.width > max_width:
                ratio = max_width / img.width
                img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)
            
            # Отрисовываем тип документа на миниатюре
            if document_type and document_type not in ('unknown', '', 'pending'):
                draw = ImageDraw.Draw(img)
                try:
                    font_type = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14)
                except Exception:
                    font_type = ImageFont.load_default()
                # Карта русских названий типов
                type_labels = {
                    'invoice': 'Счёт', 'contract': 'Договор', 'report': 'Отчёт',
                    'letter': 'Письмо', 'form': 'Форма', 'identity': 'Удостоверение',
                    'medical': 'Медицинский', 'legal': 'Юридический', 'financial': 'Финансы',
                    'technical': 'Технический', 'certificate': 'Сертификат',
                    'order': 'Приказ', 'policy': 'Политика', 'standard': 'Стандарт',
                    'news': 'Новость', 'other': 'Прочее',
                }
                label = type_labels.get(document_type, document_type)
                # Прямоугольник с типом в правом верхнем углу
                bbox = draw.textbbox((0, 0), label, font=font_type)
                tw = bbox[2] - bbox[0] + 20
                th = bbox[3] - bbox[1] + 12
                x1, y1 = img.width - tw - 8, 8
                x2, y2 = img.width - 8, 8 + th
                draw.rectangle([x1, y1, x2, y2], fill='#5e6ad2')
                draw.text((x1 + 10, y1 + 6), label, fill='#ffffff', font=font_type)
            
            img.save(thumb_path, format="WebP", quality=82)
            logger.info(f"Миниатюра создана: {thumb_path}")
            return thumb_path
        except Exception as e:
            logger.warning(f"Ошибка генерации миниатюры {document_id}: {e}")
            return None

    def _generate_text_thumbnail(self, file_path: Path) -> Optional[Any]:
        """Создать текстовую миниатюру для docx/txt/md/csv."""
        from PIL import Image, ImageDraw, ImageFont
        
        # Extract text from file
        suffix = file_path.suffix.lower()
        filename = file_path.name
        
        try:
            if suffix == '.docx':
                from docx import Document
                doc = Document(str(file_path))
                text = '\n'.join(p.text for p in doc.paragraphs[:30])
            elif suffix == '.csv':
                text = file_path.read_text(encoding='utf-8', errors='replace')
            else:  # .txt, .md
                text = file_path.read_text(encoding='utf-8', errors='replace')
        except Exception:
            try:
                text = file_path.read_text(encoding='latin-1', errors='replace')
            except Exception:
                text = file_path.read_text(errors='replace')
        
        if not text or not text.strip():
            return None
        
        # Truncate
        text_preview = text[:800].replace('\t', '    ')
        lines = text_preview.split('\n')[:25]
        
        # Canvas: A4 ratio (1:√2), ~500px wide, white bg
        W, H = 500, 700
        img = Image.new('RGB', (W, H), '#ffffff')
        draw = ImageDraw.Draw(img)
        
        # Fonts
        try:
            font_title = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 16)
            font_body = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 11)
        except Exception:
            font_title = ImageFont.load_default()
            font_body = ImageFont.load_default()
        
        # Header bar
        y = 0
        draw.rectangle([0, 0, W, 52], fill='#f0f0f0')
        draw.line([(0, 52), (W, 52)], fill='#e0e0e0')
        draw.text((16, 16), f"📄 {filename}", fill='#1a1a1a', font=font_title)
        
        # Type badge
        badge = suffix.upper().replace('.', '')
        badge_w = len(badge) * 9 + 14
        draw.rectangle([W - badge_w - 14, 12, W - 14, 38], fill='#5e6ad2')
        draw.text((W - badge_w - 7, 16), badge, fill='#ffffff', font=font_body)
        
        # Text content
        y = 60
        for line in lines:
            if y > H - 16:
                break
            display_line = line[:85]
            color = '#1a1a1a' if line.strip() else '#aaaaaa'
            draw.text((14, y), display_line, fill=color, font=font_body)
            y += 18
        
        return img

    def _find_file(self, document_id: str, filename: str) -> Optional[Path]:
        """Найти файл документа в директории uploads"""
        # Ищем по шаблону: {doc_id}_{filename}
        for f in self._upload_dir.iterdir():
            if f.name.startswith(document_id):
                return f
        return None

    async def _analyze_document_async(self, document_id: str, first_chunk_text: str, filename: str):
        """Фоновый анализ документа через LLM."""
        try:
            from src.api.services.document_analyzer import document_analyzer
            await document_analyzer.analyze_and_save(document_id, first_chunk_text, filename)
        except Exception as e:
            logger.warning(f"Фоновый анализ не удался для {document_id}: {e}")

    async def _build_knowledge_graph_async(self, document_id: str, filename: str, chunks: list):
        """Фоновое построение графа знаний.
        
        Страховка от зависания (документ 10fce2f1 висел на этом этапе >60 мин):
        - Синхронные Neo4j-операции выполняются через asyncio.to_thread с
          таймаутом — зависший Neo4j не блокирует event loop worker'а.
        - Каждое LLM-извлечение сущностей ограничено CHUNK_TIMEOUT.
        - Весь граф ограничен GRAPH_TOTAL_TIMEOUT — если не уложились, граф
          пропускается, документ продолжает обрабатываться (граф вторичен).
        """
        # Таймауты: на одну Neo4j-операцию, на один чанк, на весь граф.
        NEO4J_OP_TIMEOUT = 20
        CHUNK_TIMEOUT = 60
        # GRAPH_TOTAL_TIMEOUT увеличен (было 300с) — теперь обрабатываются ВСЕ
        # чанки документа (не первые 10), а не только начало. При 161 чанке
        # и 2 LLM-вызовах на чанк (entities→relations) даже с параллельностью
        # нужно больше времени. Граф вторичен — при превышении пропускается,
        # документ завершается.
        GRAPH_TOTAL_TIMEOUT = 1800
        # Параллельные LLM-вызовы при извлечении сущностей (адаптивный граф).
        # 3-4 одновременных запроса: не упираемся в rate-limit DeepSeek и не
        # вешаем worker; для будущего кластера это число = число реплик модели.
        MAX_PARALLEL_LLM = 3
        # Метка тайминга: граф идёт в фоне (create_task) и НЕ попадает в plog,
        # поэтому меряем здесь через time.monotonic() и пишем в logger — это
        # второй кандидат на «медленное» место (LLM-извлечение сущностей + Neo4j).
        _t_graph = time.monotonic()
        try:
            from src.indexing.knowledge_graph import kg_service
            from src.indexing.entity_extractor import entity_extractor

            async def _neo4j_op(fn, *args, label: str = ""):
                """Выполнить синхронную Neo4j-операцию в потоке с таймаутом.
                
                create_document_node/create_chunk_node — СИНХРОННЫЕ вызовы
                драйвера neo4j. Если Neo4j недоступен/завис, они блокируют
                event loop навсегда (worker solo-пул замирает целиком).
                asyncio.to_thread уводит вызов в отдельный поток, wait_for
                отдаёт управление через NEO4J_OP_TIMEOUT даже если поток
                продолжает висеть в фоне.
                """
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(fn, *args),
                        timeout=NEO4J_OP_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        f"[graph] Neo4j таймаут {label or fn.__name__} "
                        f"({NEO4J_OP_TIMEOUT}с) для {document_id} — пропуск"
                    )
                except Exception as e:
                    logger.warning(
                        f"[graph] Neo4j ошибка {label or fn.__name__} "
                        f"для {document_id}: {e}"
                    )

            async def _build():
                # Создаём узел документа
                await _neo4j_op(kg_service.create_document_node, document_id, filename, label="create_document_node")

                # ВАЖНО (зачем так сделано): обрабатываем ВСЕ чанки, а не первые 10.
                # Раньше chunks[:10] при среднем 161 чанке давал покрытие ~6% текста —
                # сущности из хвоста документа (таблицы, приложения) терялись, граф
                # был неполным. Параллельность (Semaphore) компенсирует рост числа
                # вызовов: LLM-запросы независимы, запускаем до MAX_PARALLEL_LLM
                # одновременно. Neo4j-записи — последовательно (они дёшевы).
                sem = asyncio.Semaphore(MAX_PARALLEL_LLM)

                async def _process_chunk(i: int, chunk: dict):
                    chunk_id = chunk.get("chunk_id", f"{document_id}_chunk_{i}")
                    chunk_text = chunk.get("content", "")
                    chunk_seq = chunk.get("metadata", {}).get("chunk_seq", i + 1)

                    # Узел чанка в графе
                    await _neo4j_op(
                        kg_service.create_chunk_node,
                        chunk_id, document_id, chunk_text, chunk_seq,
                        label="create_chunk_node",
                    )

                    # Извлечение сущностей (LLM) — ограничено семафором и таймаутом
                    async with sem:
                        try:
                            await asyncio.wait_for(
                                entity_extractor.extract_and_store(
                                    document_id, chunk_id, chunk_text, chunk_seq, filename
                                ),
                                timeout=CHUNK_TIMEOUT,
                            )
                        except asyncio.TimeoutError:
                            logger.warning(
                                f"[graph] Извлечение сущностей таймаут для {chunk_id} "
                                f"({CHUNK_TIMEOUT}с) — пропуск чанка"
                            )

                # Параллельно обрабатываем все чанки (не только первые 10)
                tasks = [_process_chunk(i, chunk) for i, chunk in enumerate(chunks)]
                await asyncio.gather(*tasks)

            # Весь граф — в общий таймаут: если LLM/Neo4j висят суммарно
            # дольше GRAPH_TOTAL_TIMEOUT, граф пропускается, но документ
            # продолжает обрабатываться (завершение не блокируется).
            await asyncio.wait_for(_build(), timeout=GRAPH_TOTAL_TIMEOUT)

            logger.info(
                f"Граф знаний построен для {document_id}: {len(chunks)} чанков обработано "
                f"(+{round((time.monotonic() - _t_graph) * 1000, 1)}ms)"
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"[graph] Общий таймаут {GRAPH_TOTAL_TIMEOUT}с для {document_id} — "
                f"граф пропущен, документ продолжает обработку"
            )
        except Exception as e:
            logger.warning(f"Ошибка построения графа для {document_id}: {e}")

    def _create_thumbnail(self, file_path: Path, doc_id: str, filename: str):
        """Создать миниатюру документа (первая страница/ресайз)."""
        try:
            from PIL import Image
            suffix = file_path.suffix.lower()

            if suffix == '.pdf':
                from pdf2image import convert_from_path
                images = convert_from_path(str(file_path), first_page=1, last_page=1, dpi=72)
                if images:
                    img = images[0]
                else:
                    return
            elif suffix in ('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.tiff'):
                img = Image.open(file_path)
            else:
                return  # Не поддерживаемый формат

            # Ресайз до 300px по ширине
            img.thumbnail((300, 400), Image.LANCZOS)
            thumb_path = self._thumb_dir / f"{doc_id}_{filename}.thumb.jpg"
            img.convert("RGB").save(str(thumb_path), "JPEG", quality=75)
            logger.info(f"Миниатюра создана: {thumb_path}")

        except Exception as e:
            logger.warning(f"Миниатюра не создана для {filename}: {e}")

    # ============================================================
    # Cleanup — удаление старых temp-файлов
    # ============================================================
    @staticmethod
    def cleanup_stale_temp_files(
        temp_dir: str = "/tmp/uploads",
        max_age_minutes: int = 30
    ) -> int:
        """
        Удалить temp-файлы старше N минут.
        
        Защита от засорения /tmp/ при обрыве соединения или падении.
        Вызывается:
        - Фоновым таймером из lifespan (каждые 10 минут)
        - Перед каждым upload (как предочистка)
        
        Args:
            temp_dir: Путь к temp-директории
            max_age_minutes: Максимальный возраст файла в минутах
        
        Returns:
            Количество удалённых файлов
        """
        temp_path = Path(temp_dir)
        if not temp_path.exists():
            return 0
        
        now = datetime.utcnow().timestamp()
        max_age_seconds = max_age_minutes * 60
        deleted = 0
        
        for f in temp_path.iterdir():
            if not f.is_file():
                continue
            try:
                # Считаем возраст по mtime (время последнего изменения)
                file_age = now - f.stat().st_mtime
                if file_age > max_age_seconds:
                    f.unlink()
                    deleted += 1
                    logger.debug(f"🧹 Temp-файл удалён (старше {max_age_minutes}мин): {f.name}")
            except OSError as e:
                logger.warning(f"Не удалось удалить temp-файл {f.name}: {e}")
        
        if deleted:
            logger.info(f"🧹 Очистка temp: удалено {deleted} файлов")
        return deleted


# Глобальный экземпляр

    async def _summarize_text(self, text: str, filename: str) -> str:
        """Создать краткую суммаризацию документа через LLM (provider_service → doc_analysis/chat)."""
        try:
            from src.api.services.provider_service import provider_service
            cfg = provider_service.get_function_llm_config("doc_analysis") or \
                  provider_service.get_function_llm_config("chat")
            if not cfg or not cfg.get("model"):
                return ""
            prompt = f"""Создай краткую аннотацию документа (3-5 предложений) на русском языке.
Название: {filename}
Текст (начало): {text[:3000]}

Аннотация:"""

            import httpx
            url = f"{cfg['url']}/v1/chat/completions" if cfg.get("provider") != "ollama" else f"{cfg['url']}/api/generate"
            headers = {"Content-Type": "application/json"}
            if cfg.get("api_key"):
                headers["Authorization"] = f"Bearer {cfg['api_key']}"

            if cfg.get("provider") == "ollama":
                payload = {"model": cfg["model"], "prompt": prompt, "stream": False,
                           "options": {"temperature": 0.3, "max_tokens": 300}}
                async with httpx.AsyncClient(timeout=120.0) as client:
                    resp = await client.post(url, json=payload, headers=headers)
                    if resp.status_code != 200:
                        return ""
                    return (resp.json().get("response", "") or "").strip()
            else:
                payload = {"model": cfg["model"], "messages": [{"role": "user", "content": prompt}],
                           "temperature": 0.3, "max_tokens": 300, "stream": False}
                async with httpx.AsyncClient(timeout=120.0) as client:
                    resp = await client.post(url, json=payload, headers=headers)
                    if resp.status_code != 200:
                        return ""
                    data = resp.json()
                    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                    return (content or "").strip()
        except Exception as e:
            logger.warning(f"LLM summarization failed: {e}")
            return ""

document_service = DocumentService()
