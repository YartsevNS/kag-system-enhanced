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
import hashlib  # SHA-256 для контроля дубликатов и версионности
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
    file_type: str = Field(..., description="Тип файла (MIME)")
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

    Метаданные документов хранятся в PostgreSQL через config_store
    для сохранения между перезапусками.
    """

    def __init__(self, upload_dir: Optional[str] = None):
        """
        Инициализация сервиса.

        Args:
            upload_dir: Директория для загруженных файлов
        """
        settings = get_settings()
        
        # Используем /app/data/uploads (принадлежит kag, persistent)
        upload_base = Path("/app/data")
        self._upload_dir = upload_base / "uploads"
        
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
            
            from src.api.services.config_store import config_store
            stale = [did for did in self._documents if did not in existing]
            for did in stale:
                fname = self._documents[did].filename if did in self._documents else '?'
                config_store.delete('documents', did)
                del self._documents[did]
            
            if stale:
                logger.info(f"Автоочистка: удалено {len(stale)} stale-записей без файлов")
        except Exception as e:
            logger.warning(f"Автоочистка не выполнена: {e}")

    def _load_documents_from_db(self):
        """Загрузить метаданные документов из PostgreSQL"""
        try:
            from src.api.services.config_store import config_store
            all_data = config_store.get_all("documents")

            for doc_id, data in all_data.items():
                try:
                    if isinstance(data.get('created_at'), str):
                        data['created_at'] = datetime.fromisoformat(data['created_at'])
                    if isinstance(data.get('updated_at'), str):
                        data['updated_at'] = datetime.fromisoformat(data['updated_at'])

                    self._documents[doc_id] = DocumentRecord(**data)
                except Exception as e:
                    logger.warning(f"Ошибка загрузки документа {doc_id}: {e}")

            if self._documents:
                logger.info(f"Загружено {len(self._documents)} документов из БД")
        except Exception as e:
            logger.debug(f"БД недоступна, использую пустой кэш: {e}")

    def _save_document_to_db(self, document_id: str):
        """Сохранить метаданные документа в PostgreSQL (не блокирует обработку)"""
        try:
            from src.api.services.config_store import config_store
            record = self._documents.get(document_id)
            if not record:
                return

            data = record.model_dump()
            if isinstance(data.get('created_at'), datetime):
                data['created_at'] = data['created_at'].isoformat()
            if isinstance(data.get('updated_at'), datetime):
                data['updated_at'] = data['updated_at'].isoformat()

            config_store.set("documents", document_id, data)
        except Exception as e:
            logger.debug(f"БД недоступна, пропускаю сохранение: {e}")

    async def upload_document(
        self,
        filename: str,
        file_content: bytes,
        file_type: Optional[str] = None,
        uploaded_by: Optional[str] = None,
        group_ids: Optional[List[str]] = None,
        force_new: bool = False  # True = всегда создавать новый документ, игнорируя дубликаты
    ) -> DocumentRecord:
        """
        Загрузить документ с контролем дубликатов и версионностью.

        Логика:
        1. Вычисляется SHA-256 хеш содержимого файла
        2. Если хеш СОВПАДАЕТ с существующим документом → возвращается существующий (не дублируется)
        3. Если force_new=True → старая копия бэкапится как .v{N}.bak, создаётся новая версия
        4. Файл сохраняется как {doc_id}_{filename}

        Args:
            filename: Имя файла
            file_content: Содержимое файла (байты)
            file_type: MIME тип (опционально)
            uploaded_by: ID пользователя, загрузившего документ
            group_ids: Список group_id для контроля доступа
            force_new: Принудительно создать новый документ, даже если хеш совпадает

        Returns:
            Запись о документе (новая или существующая)
        """
        # Этап 1: вычисляем SHA-256 хеш содержимого
        file_hash = hashlib.sha256(file_content).hexdigest()

        # Этап 2: проверяем — нет ли уже документа с таким же хешем?
        if not force_new:
            existing = self._find_by_hash(file_hash)
            if existing:
                logger.info(
                    f"🔁 Дубликат обнаружен: {filename} (хеш {file_hash[:12]}...) "
                    f"уже существует как {existing.document_id} (v{existing.version})"
                )
                return existing  # Возвращаем существующий — не создаём дубликат

        # Этап 3: если force_new и есть документ с таким хешем — делаем бэкап
        doc_id = str(uuid.uuid4())
        version = 1
        previous_hash = None
        original_text = None

        if force_new:
            prev = self._find_by_hash(file_hash)
            if prev:
                # Бэкапим старый файл
                prev_path = self._find_file(prev.document_id, prev.filename)
                if prev_path:
                    backup_path = prev_path.with_suffix(prev_path.suffix + f'.v{prev.version}.bak')
                    try:
                        import shutil
                        shutil.copy2(prev_path, backup_path)
                        logger.info(f"📦 Бэкап создан: {backup_path.name}")
                    except Exception as e:
                        logger.warning(f"Не удалось создать бэкап: {e}")

                # Наследуем версию и текст оригинала
                version = prev.version + 1
                previous_hash = prev.file_hash
                original_text = prev.original_text or self._load_original_text(prev.document_id)

        # Этап 4: определяем тип файла
        if not file_type:
            ext = Path(filename).suffix.lower()
            file_type = ext

        # Этап 5: сохраняем файл на диск
        file_path = self._upload_dir / f"{doc_id}_{filename}"
        with open(file_path, 'wb') as f:
            f.write(file_content)

        # Этап 6: создаём запись о документе
        record = DocumentRecord(
            document_id=doc_id,
            filename=filename,
            file_type=file_type,
            file_size=len(file_content),
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
            f"📄 Документ загружен: {doc_id[:12]}... v{version} | "
            f"хеш {file_hash[:12]}... | {filename}"
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
        # Поиск в БД (если документ не в кэше)
        try:
            from src.api.services.config_store import config_store
            all_docs = config_store.get_all("documents") or {}
            for did, data in all_docs.items():
                if isinstance(data, dict) and data.get("file_hash") == file_hash:
                    return DocumentRecord(
                        document_id=did,
                        filename=data.get("filename", "unknown"),
                        file_hash=file_hash,
                        version=int(data.get("version", 1)),
                        status=data.get("status", "completed")
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

    async def process_document(self, document_id: str) -> Dict[str, Any]:
        """
        Обработать документ: распарсить, разбить на чанки, векторизовать.

        Args:
            document_id: ID документа

        Returns:
            Результат обработки
        """
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
            parsed_doc = document_parser.parse(str(file_path), record.file_type)
            plog.log("parse", {
                "segments": len(parsed_doc.get("segments", [])),
                "parser": str(type(document_parser).__name__)
            })

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
            
            segments = parsed_doc.get("segments", [])
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
                    **parsed_doc.get("metadata", {})
                },
                group_ids=record.group_ids
            )
            plog.log("vectorize", {
                "vectors_stored": vectors_count,
                "embedding_model": "nomic-embed-text",
                "dimensions": 768
            })

            # Генерируем миниатюру
            try:
                self._generate_thumbnail(document_id, file_path)
            except Exception as e:
                logger.warning(f"Миниатюра не создана: {e}")

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
            
            # Шаг 4: Фоновый анализ первого чанка (не блокирует)
            if chunks and len(chunks) > 0:
                try:
                    import asyncio
                    first_text = chunks[0].get("content", "")
                    asyncio.create_task(self._analyze_document_async(
                        document_id, first_text, record.filename
                    ))
                except Exception as e:
                    logger.debug(f"Не удалось запустить анализ: {e}")
            
            # Шаг 5: Граф знаний — документ + чанки + извлечение сущностей (фон)
            try:
                import asyncio
                asyncio.create_task(self._build_knowledge_graph_async(
                    document_id, record.filename, chunks
                ))
            except Exception as e:
                logger.debug(f"Не удалось запустить построение графа: {e}")
            
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
        """Получить список всех документов"""
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
        
        # Удаляем запись
        del self._documents[document_id]
        
        logger.info(f"Документ удален: {document_id}")
        return True

    def _generate_thumbnail(self, document_id: str, file_path: Path) -> Optional[Path]:
        """Сгенерировать WebP-миниатюру: первая страница PDF или текстовая карточка."""
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
        """Фоновое построение графа знаний."""
        try:
            from src.indexing.knowledge_graph import kg_service
            from src.indexing.entity_extractor import entity_extractor
            
            # Создаём узел документа
            kg_service.create_document_node(document_id, filename)
            
            # Обрабатываем чанки (первые 10 для скорости, остальные в фоне)
            for i, chunk in enumerate(chunks[:10]):
                chunk_id = chunk.get("chunk_id", f"chunk_{i}")
                chunk_text = chunk.get("content", "")
                chunk_seq = chunk.get("metadata", {}).get("chunk_seq", i + 1)
                
                # Узел чанка в графе
                kg_service.create_chunk_node(chunk_id, document_id, chunk_text, chunk_seq)
                
                # Извлечение сущностей
                await entity_extractor.extract_and_store(
                    document_id, chunk_id, chunk_text, chunk_seq, filename
                )
            
            logger.info(f"Граф знаний построен для {document_id}: {len(chunks[:10])} чанков обработано")
        except Exception as e:
            logger.warning(f"Ошибка построения графа для {document_id}: {e}")


# Глобальный экземпляр
document_service = DocumentService()
