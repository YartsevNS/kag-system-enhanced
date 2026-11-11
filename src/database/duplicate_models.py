"""Возможные дубли документов: очередь решений человека.

Точное совпадение байтов (SHA-256) отсекается на загрузке без вопросов. Но один и тот же
документ приходит из разных источников с разными байтами (другой скан, водяной знак,
пересохранённый PDF), и тогда хеш честно разный. Здесь — вторая линия: кандидат на дубль
с причиной и решением человека (новая редакция / копия из другого источника / отдельный
документ). Без решения записи не сливаются и версии не создаются.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Float, Integer, String, Text

from src.database.models import Base


def _now():
    return datetime.now(timezone.utc)


class DuplicateCandidate(Base):
    __tablename__ = "duplicate_candidates"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    # Новый документ (только что загруженный) и уже существующий, на который он похож.
    new_document_id = Column(String, index=True, nullable=False)
    existing_document_id = Column(String, index=True, nullable=False)
    # reason: key_match (издатель+вид+номер), text_similar (близость векторов),
    #         title_match (нормализованное название)
    reason = Column(String, default="")
    score = Column(Float, default=0.0)
    # pending | new_version | copy | distinct
    status = Column(String, default="pending", index=True)
    details = Column(Text, default="{}")          # JSON: ключи, заголовки, источники
    decided_by = Column(String, default="")
    decided_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_now)
