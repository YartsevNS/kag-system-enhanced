"""
Чанкинг документов

Разбивает текст на смысловые фрагменты с сохранением:
- Контекста
- Метаданных
- Ссылок между чанками

Стратегия (с 2026-08-31): сегментный чанкинг — склеиваем ЦЕЛЫЕ сегменты
парсера (абзацы, таблицы, страницы) в чанки до chunk_size, не разрезая
границы сегментов. Ранее текст склеивался целиком и резался заново —
заголовки отрывались от тел, таблицы резались пополам.
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

    Сегментный чанкинг: буфер накапливает сегменты (абзац/таблица/страница)
    до chunk_size (символы), затем закрывается. Сегмент-гигант (длиннее
    chunk_size) режется RecursiveCharacterTextSplitter по внутренним
    разделителям (абзацы → строки → предложения → слова).
    Overlap: хвост предыдущего чанка подставляется в начало следующего.
    """

    def __init__(
        self,
        chunk_size: int = None,
        chunk_overlap: int = None
    ):
        settings = get_settings()
        self.chunk_size = chunk_size or settings.CHUNK_SIZE
        self.chunk_overlap = chunk_overlap or settings.CHUNK_OVERLAP

        # RecursiveCharacterTextSplitter — только для сегментов-гигантов
        if LANGCHAIN_AVAILABLE:
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
        file_type: str,
        document_id: str = None
    ) -> List[Dict[str, Any]]:
        """Разбить документ на чанки (делегирует сегментному чанкингу)."""
        segments = document.get("segments", [])
        return self.chunk_segments(segments, document_id)

    def chunk_segments(
        self,
        segments: List[Dict[str, Any]],
        document_id: str = None
    ) -> List[Dict[str, Any]]:
        """
        Сегментный чанкинг: склеивает ЦЕЛЫЕ сегменты парсера в чанки до
        chunk_size, НЕ разрезая границы сегментов.

        Почему так (вместо склейки всего текста и резки заново):
        - заголовок не отрывается от своего абзаца;
        - таблица не режется пополам;
        - страница сохраняет целостность (page_number в метаданных чанка).

        Исключение — сегмент-гигант (абзац/таблица/страница длиннее
        chunk_size): его режем RecursiveCharacterTextSplitter'ом по внутренним
        разделителям. Служебный текст (пустые сегменты) пропускается.

        Overlap: хвост предыдущего чанка (последние chunk_overlap символов)
        подставляется в начало следующего — RecursiveCharacterTextSplitter сам
        этого не делает при разбиении по разделителям.
        """
        chunks: List[Dict[str, Any]] = []
        chunk_seq = 0
        overlap = self.chunk_overlap or 0

        def _finalize(text: str, metas: List[Dict[str, Any]], tail_in: str) -> dict:
            """Собрать чанк: приклеить хвост, обрезать страховочно, собрать metadata."""
            nonlocal chunk_seq
            if tail_in:
                text = tail_in + text
            # Страховка от аномально длинных кусков (сегмент-гигант без
            # разделителей): эмбеддинг всё равно обрежет до своего лимита.
            if len(text) > self.chunk_size + overlap:
                text = text[:self.chunk_size + overlap]
            chunk_seq += 1
            pages = sorted({
                (m.get("page_number") or m.get("page") or 1)
                for m in metas if m
            })
            types = sorted({
                (m.get("segment_type") or "text")
                for m in metas if m
            })
            return {
                "chunk_id": f"{document_id}_chunk_{chunk_seq:05d}" if document_id else f"chunk_{chunk_seq:05d}",
                "content": text,
                "metadata": {
                    "chunk_index": chunk_seq - 1,
                    "chunk_seq": chunk_seq,
                    "total_chunks": 0,  # заполняется после сборки
                    "splitter": "segment_based",
                    "is_partial": False,
                    "overlap_applied": bool(tail_in),
                    "pages": pages,
                    "segment_types": types,
                }
            }

        buffer: List[Dict[str, Any]] = []  # [{content, meta}]
        buffer_len = 0
        tail = ""

        for seg in segments:
            content = (seg.get("content") or "").strip()
            if not content:
                continue
            meta = seg.get("metadata") or {}

            # Очередной сегмент не влезает в буфер → закрываем текущий чанк
            if buffer and buffer_len + 2 + len(content) > self.chunk_size:
                text = "\n\n".join(s["content"] for s in buffer)
                chunk = _finalize(text, [s["meta"] for s in buffer], tail)
                tail = chunk["content"][-overlap:] if overlap else ""
                chunks.append(chunk)
                buffer, buffer_len = [], 0

            if not buffer and len(content) > self.chunk_size:
                # Сегмент-гигант: режем по внутренним разделителям
                if LANGCHAIN_AVAILABLE and self.text_splitter:
                    pieces = [p.strip() for p in self.text_splitter.split_text(content) if p.strip()]
                else:
                    pieces = [content]
                for piece in pieces:
                    chunk = _finalize(piece, [meta], tail)
                    tail = chunk["content"][-overlap:] if overlap else ""
                    chunks.append(chunk)
                continue

            buffer.append({"content": content, "meta": meta})
            buffer_len += len(content) + (2 if buffer_len else 0)

        if buffer:
            text = "\n\n".join(s["content"] for s in buffer)
            chunk = _finalize(text, [s["meta"] for s in buffer], tail)
            chunks.append(chunk)

        # total_chunks известен только после сборки
        total = len(chunks)
        for c in chunks:
            c["metadata"]["total_chunks"] = total

        logger.info(
            f"Сегментный чанкинг: {len(chunks)} чанков из {len(segments)} сегментов "
            f"(chunk_size={self.chunk_size}, overlap={overlap})"
        )
        return chunks
