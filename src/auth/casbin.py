"""
Интеграция с Casbin для RBAC (Role-Based Access Control)

Модуль обеспечивает:
- Загрузку политик из CSV файла
- Проверку прав доступа (пользователь, ресурс, действие)
- Интеграцию с Keycloak (маппинг групп в роли)
"""

from typing import Optional
import casbin
from loguru import logger

from src.config import get_settings


class CasbinEnforcer:
    """
    Обёртка над Casbin для управления правами доступа.
    
    Использует модель RBAC с поддержкой:
    - Ролей (admin, user, annotator, viewer)
    - Ресурсов (chat, upload, admin, metrics)
    - Действий (read, write, delete, manage)
    """
    
    def __init__(self):
        self._enforcer: Optional[casbin.Enforcer] = None
        self._initialized = False
    
    def initialize(self):
        """Инициализировать Casbin с моделью и политиками"""
        if self._initialized:
            return
        
        settings = get_settings()
        
        try:
            # Загружаем модель и политики
            self._enforcer = casbin.Enforcer(
                settings.CASBIN_MODEL_PATH,
                settings.CASBIN_POLICY_FILE
            )
            
            self._initialized = True
            logger.info("Casbin инициализирован успешно")
            
        except Exception as e:
            logger.error(f"Ошибка инициализации Casbin: {e}")
            raise
    
    def check_permission(
        self,
        user: str,
        resource: str,
        action: str
    ) -> bool:
        """
        Проверить право доступа пользователя.
        
        Args:
            user: Идентификатор или роль пользователя
            resource: Ресурс (например, /api/v1/admin)
            action: Действие (GET, POST, PUT, DELETE)
            
        Returns:
            True если доступ разрешён
        """
        if not self._initialized:
            self.initialize()
        
        try:
            result = self._enforcer.enforce(user, resource, action)
            
            if not result:
                logger.debug(f"Доступ запрещён: user={user}, resource={resource}, action={action}")
            
            return result
            
        except Exception as e:
            logger.error(f"Ошибка проверки прав Casbin: {e}")
            return False
    
    def add_policy(
        self,
        role: str,
        resource: str,
        action: str
    ) -> bool:
        """
        Добавить новое правило в политику.
        
        Args:
            role: Роль
            resource: Ресурс
            action: Действие
            
        Returns:
            True если правило добавлено
        """
        if not self._initialized:
            self.initialize()
        
        try:
            result = self._enforcer.add_policy(role, resource, action)
            
            if result:
                self._enforcer.save_policy()
                logger.info(f"Добавлена политика: {role}, {resource}, {action}")
            
            return result
            
        except Exception as e:
            logger.error(f"Ошибка добавления политики: {e}")
            return False
    
    def get_roles_for_user(self, user: str) -> list:
        """Получить роли пользователя"""
        if not self._initialized:
            self.initialize()
        
        return self._enforcer.get_roles_for_user(user)
    
    def get_permissions_for_user(self, user: str) -> list:
        """Получить все права пользователя"""
        if not self._initialized:
            self.initialize()
        
        return self._enforcer.get_implicit_permissions_for_user(user)


# Глобальный экземпляр
casbin_enforcer = CasbinEnforcer()


def check_permission(user: str, resource: str, action: str) -> bool:
    """Удобная функция для проверки прав"""
    return casbin_enforcer.check_permission(user, resource, action)
