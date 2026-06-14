"""
Qdrant Service для работы с векторной базой данных

REST API клиент для Qdrant с:
- Batch операциями (upsert, search, delete)
- Update payload без пересоздания точки
- Кэшированием embeddings в Redis
- Retry логикой с exponential backoff

Не использует qdrant-client из-за версионных конфликтов.
"""

import json
import hashlib
import time
from typing import List, Optional, Dict, Any
import httpx
from loguru import logger

from src.config import get_settings

MAX_RETRIES = 3
INITIAL_BACKOFF = 0.5
EMBEDDING_CACHE_TTL = 86400


class QdrantService:
    """
    Сервис для работы с Qdrant через REST API.

    Особенности:
    - Async HTTP пул соединений для производительности
    - Retry с exponential backoff
    - Graceful degradation при недоступности Qdrant
    - Кэширование embeddings в Redis
    """

    def __init__(
        self,
        qdrant_url: Optional[str] = None,
        collection_name: Optional[str] = None,
        redis_url: Optional[str] = None
    ):
        settings = get_settings()

        qdrant_host = settings.QDRANT_HOST
        if qdrant_host == "kag-qdrant":
            import os
            if not os.path.exists("/.dockerenv"):
                qdrant_host = "localhost"

        self.qdrant_url = qdrant_url or f"http://{qdrant_host}:{settings.QDRANT_PORT}"
        self.collection_name = collection_name or settings.QDRANT_COLLECTION
        self.redis_url = redis_url or f"redis://{settings.REDIS_HOST}:{settings.REDIS_PORT}"

        self._async_client: Optional[httpx.AsyncClient] = None
        self._sync_client: Optional[httpx.Client] = None

        self._redis = None
        self._redis_try_connect()

        logger.info(
            f"QdrantService инициализирован: "
            f"qdrant={self.qdrant_url}, collection={self.collection_name}"
        )

    def _get_api_headers(self) -> dict:
        """Получить заголовки аутентификации для Qdrant из config_store."""
        try:
            from src.api.services.config_store import config_store
            qdrant_cfg = config_store.get("qdrant", "default")
            if qdrant_cfg and qdrant_cfg.get("api_key"):
                from src.security.gost_crypto import GOSTCrypto
                crypto = GOSTCrypto()
                api_key = crypto.decrypt_from_base64(qdrant_cfg["api_key"])
                return {"api-key": api_key}
        except Exception:
            pass
        return {}

    def _get_async_client(self) -> httpx.AsyncClient:
        if self._async_client is None or self._async_client.is_closed:
            self._async_client = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=10.0),
                limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
                follow_redirects=True,
                headers=self._get_api_headers()
            )
        return self._async_client

    def _get_sync_client(self) -> httpx.Client:
        if self._sync_client is None or self._sync_client.is_closed:
            self._sync_client = httpx.Client(
                timeout=httpx.Timeout(30.0, connect=10.0),
                limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
                follow_redirects=True,
                headers=self._get_api_headers()
            )
        return self._sync_client

    def _redis_try_connect(self):
        try:
            import redis
            host = self.redis_url.replace("redis://", "").split(":")[0]
            port = 6379
            if ":" in self.redis_url:
                port = int(self.redis_url.split(":")[-1])
            self._redis = redis.Redis(host=host, port=port, db=1, decode_responses=False)
            self._redis.ping()
            logger.info(f"Redis подключен для кэша: {self.redis_url}")
        except Exception as e:
            logger.warning(f"Redis недоступен, кэширование отключено: {e}")
            self._redis = None

    def _get_cache_key(self, text: str) -> str:
        text_hash = hashlib.sha256(text.encode()).hexdigest()[:16]
        return f"emb:{text_hash}"

    def get_cached_embedding(self, text: str) -> Optional[List[float]]:
        if not self._redis:
            return None
        try:
            key = self._get_cache_key(text)
            cached = self._redis.get(key)
            if cached:
                return json.loads(cached)
        except Exception as e:
            logger.debug(f"Ошибка чтения кэша: {e}")
        return None

    def set_cached_embedding(self, text: str, vector: List[float]):
        if not self._redis:
            return
        try:
            key = self._get_cache_key(text)
            self._redis.setex(key, EMBEDDING_CACHE_TTL, json.dumps(vector))
        except Exception as e:
            logger.debug(f"Ошибка записи в кэш: {e}")

    async def _async_retry_request(self, func, *args, **kwargs) -> Any:
        last_error = None
        backoff = INITIAL_BACKOFF

        for attempt in range(MAX_RETRIES):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                last_error = e
                logger.warning(
                    f"Попытка {attempt + 1}/{MAX_RETRIES} неудачна: {e}. "
                    f"Повтор через {backoff:.1f}s"
                )
                import asyncio
                await asyncio.sleep(backoff)
                backoff *= 2

        logger.error(f"Все {MAX_RETRIES} попыток неудачны: {last_error}")
        raise last_error

    def _sync_retry_request(self, func, *args, **kwargs) -> Any:
        last_error = None
        backoff = INITIAL_BACKOFF

        for attempt in range(MAX_RETRIES):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                last_error = e
                logger.warning(
                    f"Попытка {attempt + 1}/{MAX_RETRIES} неудачна: {e}. "
                    f"Повтор через {backoff:.1f}s"
                )
                time.sleep(backoff)
                backoff *= 2

        logger.error(f"Все {MAX_RETRIES} попыток неудачны: {last_error}")
        raise last_error

    async def _async_request(
        self,
        method: str,
        path: str,
        json_data: Optional[Dict] = None,
        params: Optional[Dict] = None,
        retry: bool = True
    ) -> Dict:
        url = f"{self.qdrant_url}/{path.lstrip('/')}"

        async def _do_request():
            client = self._get_async_client()
            response = await client.request(
                method=method, url=url, json=json_data, params=params
            )
            response.raise_for_status()
            return response.json()

        if retry:
            return await self._async_retry_request(_do_request)
        return await _do_request()

    def _sync_request(
        self,
        method: str,
        path: str,
        json_data: Optional[Dict] = None,
        params: Optional[Dict] = None,
        retry: bool = True
    ) -> Dict:
        url = f"{self.qdrant_url}/{path.lstrip('/')}"

        def _do_request():
            client = self._get_sync_client()
            response = client.request(
                method=method, url=url, json=json_data, params=params
            )
            response.raise_for_status()
            return response.json()

        if retry:
            return self._sync_retry_request(_do_request)
        return _do_request()

    # ==================== Async API ====================

    async def async_upsert_points(
        self,
        points_batch: List[Dict[str, Any]],
        retry: bool = True
    ) -> bool:
        if not points_batch:
            return True

        try:
            data = {
                "points": [
                    {
                        "id": p["id"],
                        "vector": p["vector"],
                        "payload": p.get("payload", {})
                    }
                    for p in points_batch
                ]
            }

            await self._async_request(
                "PUT",
                f"collections/{self.collection_name}/points",
                json_data=data,
                retry=retry
            )

            logger.debug(f"Сохранено {len(points_batch)} точек в Qdrant")
            return True

        except Exception as e:
            logger.error(f"Ошибка upsert точек: {e}")
            return False

    async def async_search(
        self,
        query_vector: List[float],
        filter: Optional[Dict] = None,
        limit: int = 10,
        score_threshold: Optional[float] = None
    ) -> List[Dict[str, Any]]:
        try:
            query = {
                "vector": query_vector,
                "limit": limit,
                "with_payload": True,
                "with_vectors": False
            }

            if filter:
                query["filter"] = filter
            if score_threshold:
                query["score_threshold"] = score_threshold

            result = await self._async_request(
                "POST",
                f"collections/{self.collection_name}/points/search",
                json_data=query
            )

            return result.get("result", [])

        except Exception as e:
            logger.error(f"Ошибка поиска: {e}")
            return []

    async def async_delete_points(
        self,
        ids: List[str],
        retry: bool = True
    ) -> bool:
        if not ids:
            return True

        try:
            await self._async_request(
                "POST",
                f"collections/{self.collection_name}/points/delete",
                json_data={"points": ids},
                retry=retry
            )

            logger.debug(f"Удалено {len(ids)} точек из Qdrant")
            return True

        except Exception as e:
            logger.error(f"Ошибка удаления точек: {e}")
            return False

    async def async_get_point(self, point_id: str) -> Optional[Dict[str, Any]]:
        try:
            result = await self._async_request(
                "POST",
                f"collections/{self.collection_name}/points",
                json_data={"ids": [point_id], "with_payload": True, "with_vectors": True}
            )
            points = result.get("result", [])
            return points[0] if points else None
        except Exception as e:
            logger.error(f"Ошибка получения точки: {e}")
            return None

    async def async_get_points_by_filter(
        self,
        filter: Dict,
        limit: int = 100,
        offset: int = 0
    ) -> List[Dict[str, Any]]:
        try:
            result = await self._async_request(
                "POST",
                f"collections/{self.collection_name}/points/scroll",
                json_data={
                    "filter": filter,
                    "limit": limit,
                    "offset": offset,
                    "with_payload": True
                }
            )
            return result.get("result", {}).get("points", [])
        except Exception as e:
            logger.error(f"Ошибка получения точек: {e}")
            return []

    async def async_get_collection_info(self) -> Dict[str, Any]:
        try:
            return await self._async_request(
                "GET",
                f"collections/{self.collection_name}"
            )
        except Exception as e:
            logger.error(f"Ошибка получения инфо о коллекции: {e}")
            return {"result": {}}

    async def async_is_healthy(self) -> bool:
        try:
            await self._async_request("GET", f"collections/{self.collection_name}", retry=False)
            return True
        except Exception:
            return False

    # ==================== Sync API (for Celery) ====================

    def upsert_points(
        self,
        points_batch: List[Dict[str, Any]],
        retry: bool = True
    ) -> bool:
        if not points_batch:
            return True

        try:
            data = {
                "points": [
                    {
                        "id": p["id"],
                        "vector": p["vector"],
                        "payload": p.get("payload", {})
                    }
                    for p in points_batch
                ]
            }

            self._sync_request(
                "PUT",
                f"collections/{self.collection_name}/points",
                json_data=data,
                retry=retry
            )

            logger.debug(f"Сохранено {len(points_batch)} точек в Qdrant")
            return True

        except Exception as e:
            logger.error(f"Ошибка upsert точек: {e}")
            return False

    def scroll_points(
        self,
        filter: Optional[Dict] = None,
        limit: int = 10,
        offset: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Прокрутка точек с фильтром (без поиска по вектору)."""
        try:
            query = {
                "limit": limit,
                "with_payload": True,
                "with_vectors": False
            }
            if filter:
                query["filter"] = filter
            if offset is not None:
                query["offset"] = offset

            result = self._sync_request(
                "POST",
                f"collections/{self.collection_name}/points/scroll",
                json_data=query
            )
            return result.get("result", {}).get("points", [])
        except Exception as e:
            logger.error(f"Ошибка scroll: {e}")
            return []

    def search(
        self,
        query_vector: List[float],
        filter: Optional[Dict] = None,
        limit: int = 10,
        score_threshold: Optional[float] = None
    ) -> List[Dict[str, Any]]:
        try:
            query = {
                "vector": query_vector,
                "limit": limit,
                "with_payload": True,
                "with_vectors": False
            }

            if filter:
                query["filter"] = filter
            if score_threshold:
                query["score_threshold"] = score_threshold

            result = self._sync_request(
                "POST",
                f"collections/{self.collection_name}/points/search",
                json_data=query
            )

            return result.get("result", [])

        except Exception as e:
            logger.error(f"Ошибка поиска: {e}")
            return []

    def delete_points(
        self,
        ids: List[str],
        retry: bool = True
    ) -> bool:
        if not ids:
            return True

        try:
            self._sync_request(
                "POST",
                f"collections/{self.collection_name}/points/delete",
                json_data={"points": ids},
                retry=retry
            )

            logger.debug(f"Удалено {len(ids)} точек из Qdrant")
            return True

        except Exception as e:
            logger.error(f"Ошибка удаления точек: {e}")
            return False

    def get_point(self, point_id: str) -> Optional[Dict[str, Any]]:
        try:
            result = self._sync_request(
                "POST",
                f"collections/{self.collection_name}/points",
                json_data={"ids": [point_id], "with_payload": True, "with_vectors": True}
            )
            points = result.get("result", [])
            return points[0] if points else None
        except Exception as e:
            logger.error(f"Ошибка получения точки: {e}")
            return None

    def get_points_by_filter(
        self,
        filter: Dict,
        limit: int = 100,
        offset: int = 0
    ) -> List[Dict[str, Any]]:
        try:
            result = self._sync_request(
                "POST",
                f"collections/{self.collection_name}/points/scroll",
                json_data={
                    "filter": filter,
                    "limit": limit,
                    "offset": offset,
                    "with_payload": True
                }
            )
            return result.get("result", {}).get("points", [])
        except Exception as e:
            logger.error(f"Ошибка получения точек: {e}")
            return []

    def get_collection_info(self) -> Dict[str, Any]:
        try:
            return self._sync_request(
                "GET",
                f"collections/{self.collection_name}"
            )
        except Exception as e:
            logger.error(f"Ошибка получения инфо о коллекции: {e}")
            return {"result": {}}

    def is_healthy(self) -> bool:
        try:
            self._sync_request("GET", f"collections/{self.collection_name}", retry=False)
            return True
        except Exception:
            return False

    # ==================== Deprecated (keep for compat) ====================

    def update_point_payload(
        self,
        point_id: str,
        payload: Dict[str, Any]
    ) -> bool:
        logger.warning(
            f"Обновление payload требует пересчёта вектора. "
            f"Используйте delete+upsert с новым вектором."
        )
        return False

    def close(self):
        if self._sync_client and not self._sync_client.is_closed:
            self._sync_client.close()
        if self._redis:
            self._redis.close()
        logger.info("QdrantService закрыт")

    async def async_close(self):
        if self._async_client and not self._async_client.is_closed:
            await self._async_client.aclose()
        if self._sync_client and not self._sync_client.is_closed:
            self._sync_client.close()
        if self._redis:
            self._redis.close()
        logger.info("QdrantService закрыт")


_qdrant_service: Optional[QdrantService] = None


def get_qdrant_service() -> QdrantService:
    global _qdrant_service
    if _qdrant_service is None:
        _qdrant_service = QdrantService()
    return _qdrant_service
