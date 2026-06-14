# src/auth/jwt_validator.py (рекомендуемая реализация)
from functools import lru_cache
import httpx
from jose import jwt, JWTError
from src.core.config import settings

class JWTValidator:
    def __init__(self, keycloak_url: str, realm: str):
        self.jwks_uri = f"{keycloak_url}/realms/{realm}/protocol/openid-connect/certs"
        self.audience = settings.KEYCLOAK_CLIENT_ID
        self.issuer = f"{keycloak_url}/realms/{realm}"
    
    @lru_cache(maxsize=1)
    def _get_jwks(self) -> dict:
        """Кэширование публичных ключей (обновление при старте)"""
        response = httpx.get(self.jwks_uri, timeout=10.0)
        response.raise_for_status()
        return response.json()
    
    def verify(self, token: str) -> dict:
        """Валидация токена с проверкой подписи, exp, iss, aud"""
        from jose import jwk
        from jose.utils import base64url_decode
        
        # Извлечение kid из заголовка токена
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header.get("kid")
        
        # Поиск соответствующего ключа в JWKS
        public_key = None
        for key in self._get_jwks()["keys"]:
            if key["kid"] == kid:
                public_key = jwk.construct(key)
                break
        
        if not public_key:
            raise JWTError("Public key not found")
        
        # Валидация с явными параметрами
        return jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],  # Только асимметричная подпись
            audience=self.audience,
            issuer=self.issuer,
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_nbf": True,
                "verify_iat": True,
                "verify_aud": True,
                "require": ["exp", "sub", "iat"]
            }
        )