"""
Pydantic модели для LLM запросов и ответов

Определяет стандартизированный интерфейс для всех бэкендов.
"""

from typing import Optional, List, Dict, Any, AsyncGenerator
from enum import Enum
from datetime import datetime
from pydantic import BaseModel, Field, field_validator


class MessageRole(str, Enum):
    """Роли сообщений в чате"""
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ChatMessage(BaseModel):
    """Сообщение в чате"""
    role: MessageRole = Field(..., description="Роль отправителя")
    content: str = Field(..., description="Содержимое сообщения", min_length=1)
    name: Optional[str] = Field(default=None, description="Имя (для tool вызовов)")
    tool_calls: Optional[List[Dict[str, Any]]] = Field(
        default=None, description="Вызовы инструментов"
    )
    tool_call_id: Optional[str] = Field(
        default=None, description="ID вызова инструмента"
    )

    @field_validator('content')
    @classmethod
    def validate_content(cls, v: str) -> str:
        """Валидация содержимого сообщения"""
        if not v or not v.strip():
            raise ValueError("Содержимое сообщения не может быть пустым")
        return v.strip()


class LLMBackendType(str, Enum):
    """Типы LLM бэкендов"""
    VLLM = "vllm"
    OLLAMA = "ollama"
    OPENAI = "openai"


class LLMRequest(BaseModel):
    """Запрос к LLM"""
    messages: List[ChatMessage] = Field(
        ..., description="История сообщений", min_length=1
    )
    model: Optional[str] = Field(
        default=None, description="Модель (переопределяет настройки по умолчанию)"
    )
    temperature: float = Field(
        default=0.7, ge=0.0, le=2.0, description="Температура генерации"
    )
    max_tokens: int = Field(
        default=4096, gt=0, le=32768, description="Максимум токенов в ответе"
    )
    top_p: float = Field(
        default=1.0, ge=0.0, le=1.0, description="Top-p sampling"
    )
    top_k: int = Field(
        default=-1, description="Top-k sampling (-1 = отключено)"
    )
    stop: Optional[List[str]] = Field(
        default=None, description="Стоп-последовательности"
    )
    stream: bool = Field(
        default=False, description="Потоковая передача"
    )
    presence_penalty: float = Field(
        default=0.0, ge=-2.0, le=2.0, description="Штраф за присутствие"
    )
    frequency_penalty: float = Field(
        default=0.0, ge=-2.0, le=2.0, description="Штраф за частоту"
    )
    seed: Optional[int] = Field(
        default=None, description="Seed для воспроизводимости"
    )
    metadata: Optional[Dict[str, Any]] = Field(
        default_factory=dict, description="Метаданные запроса"
    )

    @field_validator('messages')
    @classmethod
    def validate_messages(cls, v: List[ChatMessage]) -> List[ChatMessage]:
        """Валидация последовательности сообщений"""
        if not v:
            raise ValueError("Список сообщений не может быть пустым")

        # Проверяем что последнее сообщение от пользователя
        if v[-1].role != MessageRole.USER:
            raise ValueError("Последнее сообщение должно быть от пользователя")

        return v


class UsageInfo(BaseModel):
    """Информация об использовании токенов"""
    prompt_tokens: int = Field(default=0, description="Токенов в запросе")
    completion_tokens: int = Field(default=0, description="Токенов в ответе")
    total_tokens: int = Field(default=0, description="Всего токенов")

    def calculate_total(self):
        """Пересчитать общее количество"""
        self.total_tokens = self.prompt_tokens + self.completion_tokens
        return self


class LLMResponse(BaseModel):
    """Ответ от LLM"""
    id: str = Field(..., description="Уникальный ID ответа")
    model: str = Field(..., description="Использованная модель")
    choices: List[Dict[str, Any]] = Field(
        ..., description="Варианты ответов"
    )
    usage: Optional[UsageInfo] = Field(
        default=None, description="Информация об использовании"
    )
    created: datetime = Field(
        default_factory=datetime.utcnow, description="Время создания"
    )
    backend: LLMBackendType = Field(
        ..., description="Бэкенд который сгенерировал ответ"
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict, description="Дополнительные метаданные"
    )

    @property
    def generated_text(self) -> str:
        """Получить сгенерированный текст из первого choice"""
        if self.choices:
            return self.choices[0].get("message", {}).get("content", "")
        return ""

    @property
    def finish_reason(self) -> Optional[str]:
        """Причина завершения"""
        if self.choices:
            return self.choices[0].get("finish_reason")
        return None


class StreamChunk(BaseModel):
    """Чанк потокового ответа"""
    id: str = Field(..., description="ID ответа")
    model: str = Field(..., description="Модель")
    delta: str = Field(default="", description="Дельта текста")
    finish_reason: Optional[str] = Field(
        default=None, description="Причина завершения"
    )
    backend: LLMBackendType = Field(..., description="Бэкенд")


class LLMHealthStatus(BaseModel):
    """Статус здоровья LLM бэкенда"""
    backend: LLMBackendType = Field(..., description="Тип бэкенда")
    healthy: bool = Field(..., description="Работоспособен ли")
    model: Optional[str] = Field(default=None, description="Загруженная модель")
    response_time_ms: Optional[float] = Field(
        default=None, description="Время ответа в мс"
    )
    error: Optional[str] = Field(default=None, description="Ошибка если есть")
    last_check: datetime = Field(
        default_factory=datetime.utcnow, description="Время последней проверки"
    )


class LLMStats(BaseModel):
    """Статистика LLM"""
    backend: LLMBackendType = Field(..., description="Тип бэкенда")
    total_requests: int = Field(default=0, description="Всего запросов")
    successful_requests: int = Field(default=0, description="Успешных запросов")
    failed_requests: int = Field(default=0, description="Неуспешных запросов")
    total_tokens: int = Field(default=0, description="Всего токенов")
    avg_response_time_ms: float = Field(default=0.0, description="Среднее время ответа")
    last_error: Optional[str] = Field(default=None, description="Последняя ошибка")
    uptime_seconds: float = Field(default=0.0, description="Время работы")

    @property
    def success_rate(self) -> float:
        """Процент успешных запросов"""
        if self.total_requests == 0:
            return 100.0
        return (self.successful_requests / self.total_requests) * 100
