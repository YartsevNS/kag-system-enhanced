"""SQLAlchemy model: строки таблиц — строчный слой для точных запросов и вычислений.

Зачем: `document_tables` хранит таблицу ЦЕЛИКОМ (rows_json). По такому блобу нельзя ни
отфильтровать («позиции дороже 10 000»), ни посчитать («сумма по всем позициям НЦ-50»),
ни сравнить значения между строками — только вытащить всю таблицу и считать в питоне.

Строчный слой решает это: каждая строка — отдельная запись, привязанная к `table_id`.
`row_data` — точная копия ячеек из документа (строки, как их увидел парсер),
`row_num` — те же значения, но распознанные как числа (для сравнений и сумм),
`row_text` — представление «Ключ: значение; …» (для поиска и для контекста модели).

Связь с остальными слоями: `table_id` один и тот же в разметке чанка Qdrant и в
`document_tables` (см. src/indexing/table_ids.py) — поэтому найденная таблица, её строки
и её векторы всегда сходятся.
"""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Index, Integer, JSON, String, Text

from src.database.models import Base


class TableRow(Base):
    __tablename__ = "table_rows"
    __table_args__ = (
        # Поиск строк всегда идёт «таблица + номер строки» — составной индекс
        Index("ix_tr_table_row", "table_id", "row_index"),
        Index("ix_tr_document", "document_id"),
    )

    # Integer, а не BigInteger: нужна совместимость с SQLite (тесты), где автоинкремент
    # работает только у INTEGER PRIMARY KEY.
    id = Column(Integer, primary_key=True, autoincrement=True)
    document_id = Column(String, nullable=False)
    table_id = Column(String, nullable=False, index=True)
    row_index = Column(Integer, nullable=False)      # 0 — первая строка данных (без шапки)
    page_num = Column(Integer, default=0)
    # Оригинальные значения ячеек: {"Наименование": "Насос НЦ-50", "Цена": "15400,50"}
    row_data = Column(JSON, default=dict)
    # Распознанные числа по тем же ключам: {"Цена": 15400.5}. Пусто, если не число.
    row_num = Column(JSON, default=dict)
    # Представление для поиска и для контекста: «Наименование: Насос НЦ-50; Цена: 15400,50»
    row_text = Column(Text, default="")
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "document_id": self.document_id,
            "table_id": self.table_id,
            "row_index": self.row_index,
            "page_num": self.page_num,
            "row_data": self.row_data or {},
            "row_num": self.row_num or {},
            "row_text": self.row_text or "",
        }
