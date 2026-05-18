"""
Валидатор безопасности для системы KAG

Отвечает за:
- Валидацию входных данных
- Санитизацию пользовательского ввода
- Защиту от XSS, инъекций и других атак
- Проверку загружаемых файлов
- Валидацию API запросов
"""

import re
import os
import mimetypes
from typing import Optional, Set, Any
from pathlib import Path
from loguru import logger
from pydantic import BaseModel, validator


# Опасные паттерны для защиты от инъекций
DANGEROUS_PATTERNS = [
    re.compile(r'(<script.*?>.*?</script>)', re.IGNORECASE),  # XSS
    re.compile(r'(javascript\s*:)', re.IGNORECASE),  # XSS
    re.compile(r'(on\w+\s*=\s*["\'])', re.IGNORECASE),  # XSS события
    re.compile(r'(\b(SELECT|INSERT|UPDATE|DELETE|DROP|UNION)\b.*\b(FROM|INTO|TABLE)\b)', re.IGNORECASE),  # SQL
    re.compile(r'(\.\./)', re.MULTILINE),  # Path traversal
    re.compile(r'(%2e%2e%2f)', re.IGNORECASE),  # URL-encoded path traversal
]

# Разрешенные MIME типы для загрузки
ALLOWED_MIME_TYPES = {
    'application/pdf',
    'text/plain',
    'text/markdown',
    'text/csv',
    'text/x-markdown',
    'application/msword',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/vnd.oasis.opendocument.text',
    'application/rtf',
    'audio/mpeg',
    'audio/wav',
    'audio/ogg',
    'audio/flac',
    'image/png',
    'image/jpeg',
    'image/gif',
}

# Разрешенные расширения файлов
ALLOWED_EXTENSIONS = {
    '.pdf', '.txt', '.md', '.csv', '.doc', '.docx', '.odt', '.rtf',
    '.mp3', '.wav', '.ogg', '.flac',
    '.png', '.jpg', '.jpeg', '.gif'
}

# Максимальные размеры файлов (в байтах)
MAX_FILE_SIZE = 100 * 1024 * 1024  # 100 MB


class SecurityValidationError(Exception):
    """Ошибка валидации безопасности"""

    def __init__(self, message: str, field_name: Optional[str] = None):
        self.message = message
        self.field_name = field_name
        super().__init__(self.message)


class SecurityValidator:
    """
    Валидатор безопасности.

    Проверяет входные данные на наличие угроз.
    """

    @staticmethod
    def sanitize_text(text: str) -> str:
        """
        Санитизировать текст (удалить опасные паттерны).

        Args:
            text: Исходный текст

        Returns:
            Очищенный текст
        """
        if not text:
            return text

        # Удаляем опасные паттерны
        for pattern in DANGEROUS_PATTERNS:
            text = pattern.sub('', text)

        logger.debug("Текст санитизирован")
        return text.strip()

    @staticmethod
    def validate_text_input(
        text: str,
        field_name: str = "input",
        max_length: int = 10000
    ) -> str:
        """
        Валидировать текстовый ввод.

        Args:
            text: Текст для проверки
            field_name: Имя поля (для сообщений об ошибках)
            max_length: Максимальная длина

        Returns:
            Валидированный текст

        Raises:
            SecurityValidationError: Если найдены угрозы
        """
        if not text:
            raise SecurityValidationError("Пустой ввод", field_name)

        if len(text) > max_length:
            raise SecurityValidationError(
                f"Превышена максимальная длина ({max_length})",
                field_name
            )

        # Проверка на опасные паттерны
        for pattern in DANGEROUS_PATTERNS:
            if pattern.search(text):
                logger.warning(f"Обнаружен опасный паттерн в {field_name}: {pattern.pattern}")
                raise SecurityValidationError(
                    f"Обнаружен потенциально опасный ввод",
                    field_name
                )

        return text.strip()

    @staticmethod
    def validate_file_upload(
        file_path: str,
        filename: str,
        file_size: int,
        mime_type: Optional[str] = None
    ) -> bool:
        """
        Валидировать загружаемый файл.

        Args:
            file_path: Путь к файлу
            filename: Имя файла
            file_size: Размер файла
            mime_type: MIME тип файла

        Returns:
            True если файл безопасен

        Raises:
            SecurityValidationError: Если файл небезопасен
        """
        # Проверка размера
        if file_size > MAX_FILE_SIZE:
            raise SecurityValidationError(
                f"Файл слишком большой (максимум {MAX_FILE_SIZE / 1024 / 1024} MB)",
                "file"
            )

        if file_size == 0:
            raise SecurityValidationError("Пустой файл", "file")

        ext = Path(filename).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise SecurityValidationError(
                f"Недопустимое расширение файла: {ext}",
                "file"
            )

        if mime_type and mime_type not in ALLOWED_MIME_TYPES:
            raise SecurityValidationError(
                f"Недопустимый MIME тип: {mime_type}",
                "file"
            )

        if re.search(r'[<>"\'|?*\\]', filename):
            raise SecurityValidationError(
                "Имя файла содержит недопустимые символы",
                "file"
            )

        logger.debug(f"Файл валидирован: {filename}, размер: {file_size}")
        return True

    @staticmethod
    def validate_user_id(user_id: str) -> str:
        """
        Валидировать ID пользователя.

        Args:
            user_id: ID пользователя

        Returns:
            Валидированный ID

        Raises:
            SecurityValidationError: Если ID невалиден
        """
        if not user_id or not user_id.strip():
            raise SecurityValidationError("Пустой ID пользователя", "user_id")

        # ID должен быть alphanumeric (или UUID)
        if not re.match(r'^[a-zA-Z0-9_-]+$', user_id):
            raise SecurityValidationError(
                "ID пользователя содержит недопустимые символы",
                "user_id"
            )

        return user_id.strip()

    @staticmethod
    def validate_query_params(params: dict) -> dict:
        """
        Валидировать параметры запроса.

        Args:
            params: Параметры запроса

        Returns:
            Валидированные параметры
        """
        validated = {}

        for key, value in params.items():
            # Санитизируем ключи
            clean_key = re.sub(r'[<>"\'\\]', '', key)

            # Санитизируем значения
            if isinstance(value, str):
                clean_value = SecurityValidator.sanitize_text(value)
            else:
                clean_value = value

            validated[clean_key] = clean_value

        return validated

    @staticmethod
    def validate_api_key(api_key: str) -> bool:
        """
        Валидировать API ключ.

        Args:
            api_key: API ключ

        Returns:
            True если ключ валиден
        """
        if not api_key:
            return False

        # API ключ должен быть достаточно длинным
        if len(api_key) < 32:
            return False

        # Должен содержать только alphanumeric символы
        if not re.match(r'^[a-zA-Z0-9_-]+$', api_key):
            return False

        return True

    @staticmethod
    def rate_limit_check(
        client_id: str,
        requests: list,
        window_seconds: int = 60,
        max_requests: int = 100
    ) -> bool:
        """
        Проверить соблюдение лимита запросов.

        Args:
            client_id: ID клиента
            requests: Список времен запросов
            window_seconds: Временное окно (секунды)
            max_requests: Максимум запросов в окне

        Returns:
            True если лимит не превышен
        """
        from datetime import datetime, timedelta

        now = datetime.utcnow()
        window_start = now - timedelta(seconds=window_seconds)

        # Считаем запросы в окне
        recent_requests = [r for r in requests if r > window_start]

        return len(recent_requests) < max_requests


# Pydantic валидаторы для моделей
class SecureInputMixin(BaseModel):
    """
    Миксин для Pydantic моделей с безопасной валидацией.

    Добавляет автоматическую санитизацию текстовых полей.
    """

    @validator('*', pre=True)
    def sanitize_strings(cls, v):
        """Автоматически санитизировать все строковые поля"""
        if isinstance(v, str):
            return SecurityValidator.sanitize_text(v)
        return v
