"""
Модели данных
"""

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime
from enum import Enum


class UserRole(str, Enum):
    """Роли пользователей"""
    ADMIN = "admin"
    USER = "user"
    ANNOTATOR = "annotator"
    VIEWER = "viewer"


class User(BaseModel):
    """Модель пользователя"""
    id: str
    username: str
    email: str
    roles: List[UserRole] = []
    is_active: bool = True


class ChatMessage(BaseModel):
    """Сообщение чата"""
    role: str = Field(..., description="Роль: user, assistant, system")
    content: str = Field(..., description="Содержимое сообщения")
    metadata: Optional[Dict[str, Any]] = None


class ChatRequest(BaseModel):
    """Запрос к чату"""
    messages: List[Dict[str, str]]  # Принимает [{"role": "...", "content": "..."}]
    session_id: Optional[str] = None
    stream: bool = False
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = 4096


class ChatResponse(BaseModel):
    """Ответ чата"""
    id: str
    session_id: Optional[str]
    response: str
    sources: Optional[List[Dict[str, Any]]] = None
    metadata: Optional[Dict[str, Any]] = None


class DocumentUpload(BaseModel):
    """Загрузка документа"""
    filename: str
    file_type: str
    metadata: Optional[Dict[str, Any]] = None
    processing_options: Optional[Dict[str, Any]] = None


class DocumentStatus(BaseModel):
    """Статус обработки документа"""
    document_id: str
    status: str  # pending, processing, completed, failed
    progress: float = 0.0
    error: Optional[str] = None
    upload_id: Optional[str] = None  # UUID загрузки (для связывания логов)
    created_at: datetime
    updated_at: datetime


class SystemStatus(BaseModel):
    """Статус системы"""
    service: str
    version: str
    status: str
    uptime: float
    components: Dict[str, Any]


class HealthCheck(BaseModel):
    """Проверка работоспособности"""
    status: str
    timestamp: datetime
    version: str
