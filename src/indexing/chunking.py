"""
Чанкинг документов

Разбивает текст на смысловые фрагменты с сохранением:
- Контекста
- Метаданных
- Ссылок между чанками
"""

from typing import Dict, Any, List
from loguru import logger


class DocumentChunker:
    """
    Разбиение документов на чанки для векторизации.
    
    Стратегии:
    - По размеру (фиксированное количество токенов/символов)
    - По структуре (абзацы, секции)
    - Семантический (по смысловым границам)
    """
    
    def __init__(
        self,
        chunk_size: int = 500,
        chunk_overlap: int = 50
    ):
        """
        Args:
            chunk_size: Размер чанка в символах
            chunk_overlap: Перекрытие между чанками
        """
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
    
    def chunk(
        self,
        document: Dict[str, Any],
        file_type: str
    ) -> List[Dict[str, Any]]:
        """
        Разбить документ на чанки.
        
        Args:
            document: Распарсенный документ
            file_type: Тип файла
            
        Returns:
            Список чанков с метаданными
        """
        segments = document.get("segments", [])
        chunks = []
        
        for i, segment in enumerate(segments):
            content = segment.get("content", "")
            
            if len(content) <= self.chunk_size:
                # Сегмент помещается в один чанк
                chunks.append({
                    "chunk_id": f"chunk_{i}_0",
                    "content": content,
                    "metadata": {
                        **segment.get("metadata", {}),
                        "segment_index": i,
                        "chunk_index": 0,
                        "total_chunks": 1
                    }
                })
            else:
                # Разбиваем на несколько чанков
                segment_chunks = self._split_content(content, i, segment.get("metadata", {}))
                chunks.extend(segment_chunks)
        
        logger.info(f"Документ разбит на {len(chunks)} чанков")
        
        return chunks
    
    def _split_content(
        self,
        content: str,
        segment_index: int,
        metadata: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Разбить длинный контент на чанки с перекрытием"""
        
        chunks = []
        start = 0
        chunk_index = 0
        
        while start < len(content):
            end = start + self.chunk_size
            
            # Пытаемся разбить по границе слова
            if end < len(content):
                space_pos = content.rfind(" ", start + self.chunk_size // 2, end)
                if space_pos > start:
                    end = space_pos + 1
            
            chunk_text = content[start:end].strip()
            
            if chunk_text:
                chunks.append({
                    "chunk_id": f"chunk_{segment_index}_{chunk_index}",
                    "content": chunk_text,
                    "metadata": {
                        **metadata,
                        "segment_index": segment_index,
                        "chunk_index": chunk_index,
                        "total_chunks": (len(content) + self.chunk_size - 1) // self.chunk_size,
                        "start_pos": start,
                        "end_pos": end
                    }
                })
                chunk_index += 1
            
            start = end - self.chunk_overlap
        
        return chunks
