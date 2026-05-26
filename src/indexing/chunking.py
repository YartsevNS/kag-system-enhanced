"""
Чанкинг документов

Разбивает текст на смысловые фрагменты с сохранением:
- Контекста
- Метаданных
- Ссылок между чанками

Использует RecursiveCharacterTextSplitter для русского языка с правильными разделителями.
"""

from typing import Dict, Any, List
from loguru import logger

try:
    from langchain_text_splitters import RecursiveCharacterTextSplitter
    LANGCHAIN_AVAILABLE = True
except ImportError:
    LANGCHAIN_AVAILABLE = False
    logger.warning("langchain-text-splitters не установлен. Используем fallback чанкер.")

from src.config import get_settings


class DocumentChunker:
    """
    Единый чанкер документов для векторизации.

    Стратегии:
    - RecursiveCharacterTextSplitter с разделителями для русского языка (приоритет)
    - Fallback: посимвольное разбиение с учетом структуры
    """

    def __init__(
        self,
        chunk_size: int = None,
        chunk_overlap: int = None
    ):
        settings = get_settings()
        self.chunk_size = chunk_size or settings.CHUNK_SIZE
        self.chunk_overlap = chunk_overlap or settings.CHUNK_OVERLAP
        
        # Инициализируем RecursiveCharacterTextSplitter для русского языка
        if LANGCHAIN_AVAILABLE:
            # Разделители по приоритету: абзацы, строки, предложения, слова, символы
            self.text_splitter = RecursiveCharacterTextSplitter(
                separators=["\n\n", "\n", ". ", " ", ""],
                chunk_size=self.chunk_size,
                chunk_overlap=self.chunk_overlap,
                length_function=len,
                keep_separator=True
            )
            logger.info(
                f"RecursiveCharacterTextSplitter инициализирован: "
                f"chunk_size={self.chunk_size}, overlap={self.chunk_overlap}"
            )
        else:
            self.text_splitter = None
            logger.warning("Используем fallback чанкер без langchain")

    def chunk(
        self,
        document: Dict[str, Any],
        file_type: str
    ) -> List[Dict[str, Any]]:
        """
        Разбить документ на чанки.

        Args:
            document: Распарсенный документ со списком segments
            file_type: Тип файла

        Returns:
            Список чанков с метаданными
        """
        segments = document.get("segments", [])
        chunks = []
        chunk_seq = 0

        # Объединяем весь текст из сегментов для правильного чанкинга
        full_text = "\n\n".join(seg.get("content", "") for seg in segments)
        
        if LANGCHAIN_AVAILABLE and self.text_splitter:
            # Используем RecursiveCharacterTextSplitter для качественного разбиения
            split_texts = self.text_splitter.split_text(full_text)
            
            for i, text in enumerate(split_texts):
                chunk_seq += 1
                chunks.append({
                    "chunk_id": f"chunk_{chunk_seq:05d}",
                    "content": text,
                    "metadata": {
                        "segment_index": i,
                        "chunk_index": i,
                        "total_chunks": len(split_texts),
                        "chunk_seq": chunk_seq,
                        "splitter": "recursive_character"
                    }
                })
            logger.info(f"Документ разбит на {len(chunks)} чанков (RecursiveCharacterTextSplitter)")
        else:
            # Fallback: старый метод посимвольного разбиения
            for i, segment in enumerate(segments):
                content = segment.get("content", "")

                if len(content) <= self.chunk_size:
                    chunk_seq += 1
                    chunks.append({
                        "chunk_id": f"chunk_{chunk_seq:05d}",
                        "content": content,
                        "metadata": {
                            **segment.get("metadata", {}),
                            "segment_index": i,
                            "chunk_index": 0,
                            "total_chunks": 1,
                            "chunk_seq": chunk_seq
                        }
                    })
                else:
                    segment_chunks = self._split_content(content, i, segment.get("metadata", {}), chunk_seq)
                    chunks.extend(segment_chunks)
                    chunk_seq += len(segment_chunks)
            logger.info(f"Документ разбит на {len(chunks)} чанков (fallback)")

        return chunks

    def chunk_segments(
        self,
        segments: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Разбить список сегментов на чанки.

        Args:
            segments: Список сегментов из парсера

        Returns:
            Список чанков для векторизации
        """
        chunks = []
        chunk_seq = 0

        # Объединяем весь текст для правильного чанкинга
        full_text = "\n\n".join(seg.get("content", "") for seg in segments)
        
        if LANGCHAIN_AVAILABLE and self.text_splitter:
            # Используем RecursiveCharacterTextSplitter
            split_texts = self.text_splitter.split_text(full_text)
            
            for i, text in enumerate(split_texts):
                chunk_seq += 1
                chunks.append({
                    "chunk_id": f"chunk_{chunk_seq:05d}",
                    "content": text,
                    "metadata": {
                        "chunk_index": i,
                        "chunk_seq": chunk_seq,
                        "total_chunks": len(split_texts),
                        "splitter": "recursive_character",
                        "is_partial": False
                    }
                })
            logger.info(f"Сегменты разбиты на {len(chunks)} чанков (RecursiveCharacterTextSplitter)")
        else:
            # Fallback: старый метод
            for i, segment in enumerate(segments):
                content = segment.get("content", "")
                metadata = segment.get("metadata", {})

                if len(content) <= self.chunk_size:
                    chunk_seq += 1
                    chunks.append({
                        "chunk_id": f"chunk_{chunk_seq:05d}",
                        "content": content,
                        "metadata": {
                            **metadata,
                            "chunk_index": chunk_seq,
                            "chunk_seq": chunk_seq,
                            "is_partial": False
                        }
                    })
                else:
                    segment_chunks = self._split_content(content, i, metadata, chunk_seq)
                    chunks.extend(segment_chunks)
                    chunk_seq += len(segment_chunks)
            logger.info(f"Сегменты разбиты на {len(chunks)} чанков (fallback)")

        return chunks

    def _split_content(
        self,
        content: str,
        segment_index: int,
        metadata: Dict[str, Any],
        start_seq: int = 0
    ) -> List[Dict[str, Any]]:
        """Разбить длинный контент на чанки с перекрытием"""

        chunks = []
        start = 0
        chunk_index = 0
        chunk_seq = start_seq

        while start < len(content):
            end = start + self.chunk_size

            if end < len(content):
                space_pos = content.rfind(" ", start + self.chunk_size // 2, end)
                if space_pos > start:
                    end = space_pos + 1

            chunk_text = content[start:end].strip()

            if chunk_text:
                chunk_seq += 1
                chunks.append({
                    "chunk_id": f"chunk_{chunk_seq:05d}",
                    "content": chunk_text,
                    "metadata": {
                        **metadata,
                        "segment_index": segment_index,
                        "chunk_index": chunk_index,
                        "chunk_seq": chunk_seq,
                        "total_chunks": (len(content) + self.chunk_size - 1) // self.chunk_size,
                        "start_pos": start,
                        "end_pos": end,
                        "is_partial": True
                    }
                })
                chunk_index += 1

            start = end - self.chunk_overlap

        return chunks
