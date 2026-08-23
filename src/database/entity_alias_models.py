"""SQLAlchemy model: словарь известных алиасов сущностей (entity resolution).

Зачем: LLM/embedding не всегда различают «ЦБ»=«Банк России» (алиас) от
«Сбербанк»=«Банк России» (разные). Для известных пар (организации, стандарты,
термины) задаём их ЯВНО в таблице entity_aliases — это детерминированно и
быстро, без LLM. Таблица пополняется через скрипт/админку.

Структура:
- canonical_name — каноническое имя (остаётся узлом в Neo4j)
- alias — вариант написания (сливается в canonical)
- entity_type — тип (organization, document_ref, legal_term, person)
- source — откуда пара (manual, dictionary, llm_verified)
"""

from sqlalchemy import Column, String, DateTime, Text
from datetime import datetime, timezone

from src.database.models import Base


class EntityAlias(Base):
    __tablename__ = "entity_aliases"

    id = Column(String, primary_key=True)
    canonical_name = Column(String, nullable=False, index=True)
    alias = Column(String, nullable=False, index=True)
    entity_type = Column(String, default="organization", index=True)
    source = Column(String, default="manual")
    comment = Column(Text, default="")
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        return {
            "id": self.id,
            "canonical_name": self.canonical_name,
            "alias": self.alias,
            "entity_type": self.entity_type,
            "source": self.source,
            "comment": self.comment,
        }
