"""
SQLAlchemy модели для базы данных PostgreSQL

Используется для хранения конфигураций, пользователей и других постоянных данных.
"""

from sqlalchemy import Column, String, Text, Integer, DateTime, Boolean
from sqlalchemy.ext.declarative import declarative_base
from datetime import datetime

Base = declarative_base()


class SystemConfig(Base):
    """
    Таблица для хранения системных конфигураций.
    
    Здесь хранятся:
    - Настройки SSH подключения
    - Параметры чанкинга
    - Другие глобальные настройки
    """
    __tablename__ = "system_configs"

    id = Column(String, primary_key=True, index=True)  # Например: "ssh:default"
    category = Column(String, index=True)              # Например: "ssh", "chunking"
    key = Column(String, index=True)                   # Например: "default"
    value = Column(Text)                               # JSON строка с данными
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
