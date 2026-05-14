"""
Document Service для KAG

Отвечает за:
- Загрузку документов
- Парсинг и чанкинг
- Векторизацию через Embeddings
- Сохранение в Qdrant
- Отслеживание статуса обработки
"""

from typing import Dict, Any, List, Optional
from datetime import datetime
import uuid
import os
import shutil
from pathlib import Path
from loguru import logger
from pydantic import BaseModel, Field

from src.indexing.parsers import document_parser, text_chunker
from src.indexing.embeddings_service import embeddings_service
from src.config import get_settings


class DocumentRecord(BaseModel):
    """Запись о документе"""
    document_id: str = Field(..., description="ID документа")
    filename: str = Field(..., description="Имя файла")
    file_type: str = Field(..., description="Тип файла")
    file_size: int = Field(default=0, description="Размер файла")
    status: str = Field(default="pending", description="Статус: pending, processing, completed, failed")
    progress: float = Field(default=0.0, description="Прогресс обработки (0-100)")
    chunks_count: int = Field(default=0, description="Количество чанков")
    error: Optional[str] = Field(default=None, description="Ошибка если есть")
    uploaded_by: Optional[str] = Field(default=None, description="ID пользователя, загрузившего документ")
    group_ids: Optional[List[str]] = Field(default=None, description="ID групп, имеющих доступ к документу")
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
        
        # Используем ./user_data/uploads в рабочей директории
        upload_base = Path("/home/nick/kagproject/user_data")
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
        group_ids: Optional[List[str]] = None
    ) -> DocumentRecord:
        """
        Загрузить документ.

        Args:
            filename: Имя файла
            file_content: Содержимое файла
            file_type: MIME тип (опционально)
            uploaded_by: ID пользователя, загрузившего документ
            group_ids: Список group_id для контроля доступа

        Returns:
            Запись о документе
        """
        doc_id = str(uuid.uuid4())
        
        # Определяем тип файла
        if not file_type:
            ext = Path(filename).suffix.lower()
            file_type = ext
        
        # Сохраняем файл
        file_path = self._upload_dir / f"{doc_id}_{filename}"
        with open(file_path, 'wb') as f:
            f.write(file_content)
        
        # Создаём запись
        record = DocumentRecord(
            document_id=doc_id,
            filename=filename,
            file_type=file_type,
            file_size=len(file_content),
            status="pending",
            uploaded_by=uploaded_by,
            group_ids=group_ids or []
        )
        
        self._documents[doc_id] = record
        logger.info(f"Документ загружен: {doc_id}, {filename}, groups={group_ids}")

        # Сохраняем метаданные в БД
        self._save_document_to_db(doc_id)

        return record

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
            # Обновляем статус
            record.status = "processing"
            record.progress = 10
            record.updated_at = datetime.utcnow()
            self._save_document_to_db(document_id)

            # Находим файл
            file_path = self._find_file(document_id, record.filename)
            if not file_path:
                raise FileNotFoundError(f"Файл не найден для документа {document_id}")

            # Шаг 1: Парсинг (30%)
            logger.info(f"Парсинг документа: {document_id}")
            record.progress = 30
            self._save_document_to_db(document_id)
            parsed_doc = document_parser.parse(str(file_path), record.file_type)

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
        """Сгенерировать WebP-миниатюру первой страницы документа."""
        from PIL import Image
        
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
                # Не-PDF и не-изображение — генерируем placeholder
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

    def _find_file(self, document_id: str, filename: str) -> Optional[Path]:
        """Найти файл документа в директории uploads"""
        # Ищем по шаблону: {doc_id}_{filename}
        for f in self._upload_dir.iterdir():
            if f.name.startswith(document_id):
                return f
        return None


# Глобальный экземпляр
document_service = DocumentService()
