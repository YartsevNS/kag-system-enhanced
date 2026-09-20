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

from typing import Dict, Any, List, Optional
from loguru import logger

from src.indexing.standard_parser import extract_structure

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
        # ВАЖНО: `chunk_overlap or settings.CHUNK_OVERLAP` делал 0 неотличимым от «не задан»,
        # а 0 здесь осмысленное значение — «без перекрытия» (нужно для проверок и для
        # таблиц, которые склеивать с соседним текстом нельзя).
        self.chunk_overlap = (
            chunk_overlap if chunk_overlap is not None else settings.CHUNK_OVERLAP
        )

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

        # Табличный стек: атомарность табличных сегментов — управляемая настройка
        # (`tables.config.atomic_chunks`). Ошибка чтения настроек не должна ломать
        # чанкинг, поэтому по умолчанию ведём себя как раньше, но помечаем таблицы.
        try:
            from src.indexing.tables_settings import get_tables_config

            atomic_tables = bool(get_tables_config().get("atomic_chunks", True))
        except Exception:
            atomic_tables = True

        # Поля таблицы, которые переносим в разметку чанка (а оттуда — в payload Qdrant).
        # Без них строку/таблицу нельзя отличить от обычного текста: именно на этом
        # ломались вопросы вида «какая цена у X» — таблица склеивалась с текстом страницы.
        table_keys = (
            "table_id", "table_index", "row_count", "col_count", "quality", "columns",
        )

        def _finalize(text: str, metas: List[Dict[str, Any]], tail_in: str,
                      extra: Optional[Dict[str, Any]] = None,
                      chunk_type: str = "text") -> dict:
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
                    "splitter": "segment_based" if chunk_type == "text" else "table_segment",
                    "chunk_type": chunk_type,
                    "is_partial": False,
                    "overlap_applied": bool(tail_in),
                    "pages": pages,
                    "segment_types": types,
                    # Табличные поля (пусто для обычного текста)
                    **(extra or {}),
                    # Структурные поля (стандарт/пункт/раздел) — для payload-фильтров
                    # поиска («ГОСТ 57580.1-2017, п. 5.2.1»). Пусто, если не найдено.
                    **extract_structure(text),
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
            # Страница может лежать на верхнем уровне сегмента (document_service
            # кладёт "page": page_num, а metadata оставляет пустым) ИЛИ в metadata
            # ("page_number"/"page"). Собираем в общий meta, чтобы pages попали
            # в metadata чанка (для перехода к странице из поиска).
            page = seg.get("page") or meta.get("page") or meta.get("page_number")
            meta_merged = dict(meta)
            if page is not None:
                meta_merged["page"] = page

            # ── Табличный сегмент: атомарный чанк ────────────────────────────────
            # Закрываем накопленный текст, таблицу отдаём ОТДЕЛЬНЫМ чанком и обнуляем хвост
            # перекрытия: иначе таблица склеивается с текстом страницы (и с соседней
            # таблицей), и в одном векторе оказываются строки разных таблиц — ровно то,
            # что делает поиск по строкам бессмысленным.
            if atomic_tables and meta_merged.get("chunk_type") == "table":
                if buffer:
                    text = "\n\n".join(s["content"] for s in buffer)
                    chunks.append(_finalize(text, [s["meta"] for s in buffer], tail))
                    buffer, buffer_len, tail = [], 0, ""

                extra = {
                    key: meta_merged[key] for key in table_keys
                    if meta_merged.get(key) not in (None, "", [], 0)
                }

                if len(content) > self.chunk_size and LANGCHAIN_AVAILABLE and self.text_splitter:
                    # Таблица длиннее лимита: режем по строкам (разделитель — перевод строки),
                    # каждой части оставляем тот же table_id и помечаем как частичную.
                    # Строковые векторы для таких таблиц делает этап 3 (по строкам).
                    pieces = [p.strip() for p in self.text_splitter.split_text(content) if p.strip()]
                    for piece in pieces:
                        chunk = _finalize(piece, [meta_merged], "",
                                          extra={**extra, "is_partial": len(pieces) > 1},
                                          chunk_type="table")
                        chunks.append(chunk)
                else:
                    chunks.append(_finalize(content, [meta_merged], "", extra=extra,
                                            chunk_type="table"))
                continue

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
                    chunk = _finalize(piece, [meta_merged], tail)
                    tail = chunk["content"][-overlap:] if overlap else ""
                    chunks.append(chunk)
                continue

            buffer.append({"content": content, "meta": meta_merged})
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
