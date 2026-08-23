"""SQLAlchemy model: таблицы документов для точных запросов (table RAG).

Зачем: таблицы в чанках Qdrant теряют структуру (плоский текст). Для точных
запросов («что в строке X колонки Y», «какие объекты КИИ относятся к ОКВЭД
61.10.1») храним таблицы структурно: rows (2D массив), markdown, html.

Поиск: семантический по чанкам (Qdrant) находит документ, затем точный —
по document_tables (SQL: значение в колонке/строке). Рендер в чате из html.
"""

from sqlalchemy import Column, String, Integer, DateTime, Text, Index
from datetime import datetime, timezone

from src.database.models import Base


class DocumentTable(Base):
    __tablename__ = "document_tables"
    __table_args__ = (
        Index("ix_dt_document", "document_id"),
    )

    id = Column(String, primary_key=True)
    document_id = Column(String, nullable=False, index=True)
    page_num = Column(Integer, default=0)
    table_index = Column(Integer, default=0)          # номер таблицы на странице
    # Представления одной таблицы
    rows_json = Column(Text, default="[]")            # 2D массив ячеек (JSON)
    headers_json = Column(Text, default="[]")         # строка заголовков (JSON)
    markdown = Column(Text, default="")               # markdown-таблица
    html = Column(Text, default="")                   # HTML-таблица (рендер в чате)
    bbox = Column(Text, default="")                   # координаты на странице (JSON)
    model = Column(String, default="pymupdf")         # чем распознана (pymupdf/docling/granite)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        import json
        try:
            rows = json.loads(self.rows_json or "[]")
            headers = json.loads(self.headers_json or "[]")
        except Exception:
            rows, headers = [], []
        return {
            "id": self.id,
            "document_id": self.document_id,
            "page_num": self.page_num,
            "table_index": self.table_index,
            "rows": rows,
            "headers": headers,
            "markdown": self.markdown,
            "html": self.html,
            "model": self.model,
        }
