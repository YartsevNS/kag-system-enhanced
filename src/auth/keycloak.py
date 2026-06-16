"""
Интеграция с Keycloak для аутентификации

Модуль обеспечивает:
- Проверку JWT-токенов от Keycloak
- Валидацию подписи через JWKS
- Извлечение claims (роли, группы, права)
"""

from typing import Dict, Any, Optional
from functools import lru_cache
import httpx
from loguru import logger
import jwt
try:
    from jwt.exceptions import InvalidTokenError
except ImportError:
    InvalidTokenError = Exception

from src.config import get_settings


class KeycloakError(Exception):
    """Ошибка при работе с Keycloak"""
    pass


@lru_cache()
def get_jwks() -> Dict[str, Any]:
    """
    Получить JWKS (JSON Web Key Set) от Keycloak.
    
    Используется для проверки подписи JWT-токенов.
    Результат кэшируется для производительности.
    """
    settings = get_settings()
    jwks_uri = f"{settings.KEYCLOAK_URL}/realms/{settings.KEYCLOAK_REALM}/protocol/openid-connect/certs"
    
    try:
        response = httpx.get(jwks_uri, timeout=10.0)
        response.raise_for_status()
        logger.info("JWKS успешно получен от Keycloak")
        return response.json()
    except Exception as e:
        logger.error(f"Ошибка получения JWKS: {e}")
        raise KeycloakError(f"Не удалось получить JWKS: {e}")


def verify_token(token: str) -> Dict[str, Any]:
    """
    Проверить JWT-токен от Keycloak.
    
    Args:
        token: JWT-токен в формате строки
        
    Returns:
        Словарь с claims (пользователь, роли, группы)
        
    Raises:
        KeycloakError: При ошибке проверки токена
    """
    settings = get_settings()
    
    try:
        # Получаем JWKS для проверки подписи
        jwks = get_jwks()
        
        # Декодируем токен без проверки подписи для получения kid
        unverified_headers = jwt.get_unverified_header(token)
        kid = unverified_headers.get("kid")

        # Находим соответствующий ключ в JWKS
        rsa_key = {}
        for key in jwks.get("keys", []):
            if kid:
                if key.get("kid") == kid:
                    rsa_key = {
                        "kty": key.get("kty"),
                        "kid": key.get("kid"),
                        "use": key.get("use"),
                        "n": key.get("n"),
                        "e": key.get("e")
                    }
                    break
            elif not rsa_key:
                # Если kid нет в токене — берём первый ключ (Keycloak 24+)
                rsa_key = {
                    "kty": key.get("kty"),
                    "kid": key.get("kid"),
                    "use": key.get("use"),
                    "n": key.get("n"),
                    "e": key.get("e")
                }

        if not rsa_key:
            raise KeycloakError("Не найден подходящий ключ в JWKS")
        
        # Проверяем и декодируем токен
        audience = settings.KEYCLOAK_CLIENT_ID
        issuer = f"{settings.KEYCLOAK_URL}/realms/{settings.KEYCLOAK_REALM}"
        
        # Сначала декодируем без проверки audience, проверим вручную
        payload = jwt.decode(
            token,
            rsa_key,
            algorithms=["RS256"],
            audience=audience,
            issuer=issuer,
            options={
                "verify_aud": True,
                "verify_iat": True,
                "verify_exp": True,
                "verify_nbf": True,
                "verify_iss": True,
                "require": ["exp", "iat", "sub", "iss"]
            }
        )
        
        # Извлекаем роли и группы
        user_roles = _extract_roles(payload)
        payload["roles"] = user_roles
        
        logger.debug(f"Токен проверен: user={payload.get('preferred_username')}, roles={user_roles}")
        
        return payload
        
    except ExpiredSignatureError:
        raise KeycloakError("Токен истёк")
    except JWTError as e:
        raise KeycloakError(f"Ошибка JWT: {e}")
    except KeycloakError:
        raise
    except Exception as e:
        logger.error(f"Неожиданная ошибка при проверке токена: {e}")
        raise KeycloakError(f"Неизвестная ошибка: {e}")


def _extract_roles(payload: Dict[str, Any]) -> list:
    """
    Извлечь роли пользователя из payload токена.
    
    Keycloak может хранить роли в разных местах:
    - realm_access.roles (роли уровня realm)
    - resource_access.{client_id}.roles (роли уровня клиента)
    - groups (группы пользователя, маппятся в роли через Casbin)
    """
    roles = set()
    
    # Роли уровня realm
    realm_access = payload.get("realm_access", {})
    realm_roles = realm_access.get("roles", [])
    roles.update(realm_roles)
    
    # Роли уровня клиента
    settings = get_settings()
    resource_access = payload.get("resource_access", {})
    client_roles = resource_access.get(settings.KEYCLOAK_CLIENT_ID, {}).get("roles", [])
    roles.update(client_roles)
    
    # Группы
    groups = payload.get("groups", [])
    roles.update(groups)
    
    return list(roles)


def get_public_key() -> str:
    """
    Получить публичный ключ Keycloak для ручной проверки подписи.
    
    Returns:
        PEM-формат публичного ключа
    """
    settings = get_settings()
    realm_url = f"{settings.KEYCLOAK_URL}/realms/{settings.KEYCLOAK_REALM}"
    
    try:
        response = httpx.get(realm_url, timeout=10.0)
        response.raise_for_status()
        realm_info = response.json()
        public_key = realm_info.get("public_key")
        
        if not public_key:
            raise KeycloakError("Публичный ключ не найден в информации о realm")
        
        return f"-----BEGIN PUBLIC KEY-----\n{public_key}\n-----END PUBLIC KEY-----"
        
    except Exception as e:
        logger.error(f"Ошибка получения публичного ключа: {e}")
        raise KeycloakError(f"Не удалось получить публичный ключ: {e}")
