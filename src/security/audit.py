"""
Аудит безопасности для системы KAG

Отвечает за:
- Логирование всех действий аутентификации
- Логирование изменений политик RBAC
- Логирование запросов к LLM
- Форматирование логов для SIEM
- Экспорт в Loki/Syslog
"""

from typing import Dict, Any, Optional
from datetime import datetime
from enum import Enum
import json
import os
from pathlib import Path
from loguru import logger


class AuditEventType(Enum):
    """Типы событий аудита"""
    AUTH_SUCCESS = "auth_success"
    AUTH_FAILURE = "auth_failure"
    PERMISSION_DENIED = "permission_denied"
    POLICY_CHANGE = "policy_change"
    DATA_ACCESS = "data_access"
    DATA_MODIFICATION = "data_modification"
    LLM_REQUEST = "llm_request"
    SYSTEM_ERROR = "system_error"
    CONFIG_CHANGE = "config_change"
    USER_LOGIN = "user_login"
    USER_LOGOUT = "user_logout"
    ADMIN_ACTION = "admin_action"


class AuditEvent:
    """Событие аудита"""

    def __init__(
        self,
        event_type: AuditEventType,
        user_id: Optional[str] = None,
        resource: Optional[str] = None,
        action: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        success: bool = True,
        error_message: Optional[str] = None
    ):
        """
        Инициализация события аудита.

        Args:
            event_type: Тип события
            user_id: ID пользователя
            resource: Ресурс
            action: Действие
            details: Дополнительные детали
            ip_address: IP адрес
            user_agent: User-Agent
            success: Успешность выполнения
            error_message: Сообщение об ошибке
        """
        self.timestamp = datetime.utcnow()
        self.event_type = event_type
        self.user_id = user_id
        self.resource = resource
        self.action = action
        self.details = details or {}
        self.ip_address = ip_address
        self.user_agent = user_agent
        self.success = success
        self.error_message = error_message

    def to_dict(self) -> Dict[str, Any]:
        """Преобразовать в словарь"""
        return {
            "timestamp": self.timestamp.isoformat(),
            "event_type": self.event_type.value,
            "user_id": self.user_id,
            "resource": self.resource,
            "action": self.action,
            "details": self.details,
            "ip_address": self.ip_address,
            "user_agent": self.user_agent,
            "success": self.success,
            "error_message": self.error_message
        }

    def to_json(self) -> str:
        """Преобразовать в JSON"""
        return json.dumps(self.to_dict(), ensure_ascii=False)


class AuditLogger:
    """
    Логгер аудита безопасности.

    Логирует все важные события системы в формате JSON
    для последующей передачи в Loki/SIEM.
    """

    def __init__(
        self,
        log_file: Optional[Path] = None,
        enable_console: bool = True
    ):
        """
        Инициализация логгера аудита.

        Args:
            log_file: Файл для сохранения логов
            enable_console: Выводить ли в консоль
        """
        self._log_file = log_file
        self._enable_console = enable_console

        # Создаем директорию если нужно
        if self._log_file:
            self._log_file.parent.mkdir(parents=True, exist_ok=True)

        logger.info(f"AuditLogger инициализирован, файл: {self._log_file}")

    def log(
        self,
        event_type: AuditEventType,
        user_id: Optional[str] = None,
        resource: Optional[str] = None,
        action: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        success: bool = True,
        error_message: Optional[str] = None
    ):
        """
        Записать событие аудита.

        Args:
            event_type: Тип события
            user_id: ID пользователя
            resource: Ресурс
            action: Действие
            details: Дополнительные детали
            ip_address: IP адрес
            user_agent: User-Agent
            success: Успешность выполнения
            error_message: Сообщение об ошибке
        """
        event = AuditEvent(
            event_type=event_type,
            user_id=user_id,
            resource=resource,
            action=action,
            details=details,
            ip_address=ip_address,
            user_agent=user_agent,
            success=success,
            error_message=error_message
        )

        # Форматируем в JSON
        json_log = event.to_json()

        # Выводим в консоль
        if self._enable_console:
            if success:
                logger.info(f"AUDIT: {json_log}")
            else:
                logger.warning(f"AUDIT: {json_log}")

        # Сохраняем в файл
        if self._log_file:
            self._write_to_file(json_log)

    def log_auth_success(
        self,
        user_id: str,
        method: str,
        ip_address: Optional[str] = None
    ):
        """Записать успешную аутентификацию"""
        self.log(
            event_type=AuditEventType.AUTH_SUCCESS,
            user_id=user_id,
            action=f"auth_{method}",
            ip_address=ip_address,
            details={"auth_method": method}
        )

    def log_auth_failure(
        self,
        user_id: str,
        method: str,
        ip_address: Optional[str] = None,
        reason: Optional[str] = None
    ):
        """Записать неудачную аутентификацию"""
        self.log(
            event_type=AuditEventType.AUTH_FAILURE,
            user_id=user_id,
            action=f"auth_{method}",
            ip_address=ip_address,
            success=False,
            error_message=reason,
            details={"auth_method": method, "failure_reason": reason}
        )

    def log_permission_denied(
        self,
        user_id: str,
        resource: str,
        action: str,
        ip_address: Optional[str] = None
    ):
        """Записать отказ в доступе"""
        self.log(
            event_type=AuditEventType.PERMISSION_DENIED,
            user_id=user_id,
            resource=resource,
            action=action,
            ip_address=ip_address,
            success=False,
            error_message=f"Permission denied: {action} on {resource}"
        )

    def log_policy_change(
        self,
        user_id: str,
        policy_name: str,
        old_value: Any,
        new_value: Any,
        ip_address: Optional[str] = None
    ):
        """Записать изменение политики"""
        self.log(
            event_type=AuditEventType.POLICY_CHANGE,
            user_id=user_id,
            resource=policy_name,
            action="policy_update",
            ip_address=ip_address,
            details={
                "policy_name": policy_name,
                "old_value": str(old_value),
                "new_value": str(new_value)
            }
        )

    def log_data_access(
        self,
        user_id: str,
        resource: str,
        ip_address: Optional[str] = None
    ):
        """Записать доступ к данным"""
        self.log(
            event_type=AuditEventType.DATA_ACCESS,
            user_id=user_id,
            resource=resource,
            action="data_read",
            ip_address=ip_address
        )

    def log_data_modification(
        self,
        user_id: str,
        resource: str,
        action: str,
        details: Optional[Dict[str, Any]] = None,
        ip_address: Optional[str] = None
    ):
        """Записать изменение данных"""
        self.log(
            event_type=AuditEventType.DATA_MODIFICATION,
            user_id=user_id,
            resource=resource,
            action=action,
            details=details,
            ip_address=ip_address
        )

    def log_llm_request(
        self,
        user_id: str,
        model: str,
        prompt_length: int,
        response_length: int,
        duration_seconds: float,
        ip_address: Optional[str] = None
    ):
        """Записать запрос к LLM"""
        self.log(
            event_type=AuditEventType.LLM_REQUEST,
            user_id=user_id,
            resource=model,
            action="llm_generate",
            ip_address=ip_address,
            details={
                "model": model,
                "prompt_length": prompt_length,
                "response_length": response_length,
                "duration_seconds": duration_seconds
            }
        )

    def log_admin_action(
        self,
        user_id: str,
        action: str,
        details: Optional[Dict[str, Any]] = None,
        ip_address: Optional[str] = None
    ):
        """Записать действие администратора"""
        self.log(
            event_type=AuditEventType.ADMIN_ACTION,
            user_id=user_id,
            action=action,
            details=details,
            ip_address=ip_address
        )

    def _write_to_file(self, json_log: str):
        """Записать лог в файл"""
        try:
            with open(self._log_file, 'a', encoding='utf-8') as f:
                f.write(json_log + '\n')
        except Exception as e:
            logger.error(f"Ошибка записи аудита в файл: {e}")


# Глобальный экземпляр
# В продакшене настроить log_file на постоянный путь
try:
    audit_logger = AuditLogger(
        log_file=Path("/app/data/audit/audit.log"),
        enable_console=True
    )
except PermissionError:
    # Fallback если нет прав на запись
    audit_logger = AuditLogger(
        log_file=None,
        enable_console=True
    )
