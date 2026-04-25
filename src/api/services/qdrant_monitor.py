"""
Qdrant Monitor Service для KAG

Отслеживает состояние векторной базы данных Qdrant:
- Список коллекций
- Количество документов/векторов
- Метаданные
- Информация о векторах
"""

from typing import Dict, Any, List, Optional
from datetime import datetime
from loguru import logger

try:
    from qdrant_client import QdrantClient
    QDRANT_AVAILABLE = True
except ImportError:
    QDRANT_AVAILABLE = False
    logger.warning("Qdrant client не установлен. Установите: pip install qdrant-client")


class QdrantMonitor:
    """
    Сервис мониторинга Qdrant базы данных.
    """

    def __init__(self, host: str = "qdrant", port: int = 6333):
        """Инициализация монитора"""
        self.host = host
        self.port = port
        self.url = f"http://{host}:{port}"
        self._client: Optional[QdrantClient] = None

    def _connect(self):
        """Подключиться к Qdrant"""
        if QDRANT_AVAILABLE:
            try:
                self._client = QdrantClient(url=self.url)
                logger.info(f"QdrantMonitor подключен: {self.url}")
                return True
            except Exception as e:
                logger.error(f"Ошибка подключения к Qdrant: {e}")
                return False
        return False

    def _ensure_client(self):
        """Убедиться что клиент подключён"""
        if not self._client:
            self._connect()
        return self._client is not None

    def get_collections_list(self) -> List[Dict[str, Any]]:
        """
        Получить список всех коллекций.

        Returns:
            Список коллекций с базовой информацией
        """
        try:
            if not self._ensure_client():
                return []

            collections = self._client.get_collections()
            result = []

            for col in collections.collections:
                info = self._client.get_collection(col.name)
                result.append({
                    "name": col.name,
                    "vectors_count": info.vectors_count,
                    "points_count": info.points_count,
                    "status": info.status,
                    "ayload_schema": info.payload_schema
                })

            return result
        except Exception as e:
            logger.error(f"Ошибка получения списка коллекций: {e}")
            return []

    def get_collection_info(self, collection_name: str) -> Dict[str, Any]:
        """
        Получить детальную информацию о коллекции.

        Args:
            collection_name: Имя коллекции

        Returns:
            Информация о коллекции
        """
        try:
            if not self._ensure_client():
                return {"error": "Не подключено к Qdrant"}

            info = self._client.get_collection(collection_name)

            return {
                "name": collection_name,
                "status": info.status,
                "vectors_count": info.vectors_count,
                "points_count": info.points_count,
                "segments_count": info.segments_count,
                "disk_size": info.disk_size_bytes,
                "ram_size": info.ram_size_bytes,
                "storage_version": info.storage_version,
                "vector_settings": {
                    "size": info.vectors_config.get("size") if info.vectors_config else None,
                    "distance": info.vectors_config.get("distance") if info.vectors_config else None
                } if hasattr(info, "vectors_config") and info.vectors_config else {},
                "payload_schema": self._format_payload_schema(info.payload_schema)
            }
        except Exception as e:
            logger.error(f"Ошибка получения информации о коллекции: {e}")
            return {"error": str(e)}

    def _format_payload_schema(self, schema) -> Dict[str, Any]:
        """Форматировать схему payload"""
        if not schema:
            return {}
        result = {}
        for field_name, field_info in schema.items():
            result[field_name] = {
                "type": field_info.get("data_type", "unknown"),
                "indexed": field_info.get("indexed", False)
            }
        return result

    def get_points_sample(self, collection_name: str, limit: int = 20) -> List[Dict[str, Any]]:
        """
        Получить пример точек (документов) из коллекции.

        Args:
            collection_name: Имя коллекции
            limit: Количество точек

        Returns:
            Список точек с метаданными
        """
        try:
            if not self._ensure_client():
                return []

            results, _ = self._client.scroll(
                collection_name=collection_name,
                limit=limit,
                with_payload=True,
                with_vectors=False
            )

            points = []
            for point in results:
                points.append({
                    "id": point.id,
                    "version": point.version,
                    "score": point.score,
                    "payload": point.payload
                })

            return points
        except Exception as e:
            logger.error(f"Ошибка получения точек: {e}")
            return []

    def get_payload_stats(self, collection_name: str) -> Dict[str, Any]:
        """
        Получить статистику по метаданным (payload).

        Args:
            collection_name: Имя коллекции

        Returns:
            Статистика по полям payload
        """
        try:
            if not self._ensure_client():
                return {"error": "Не подключено к Qdrant"}

            points_sample = self.get_points_sample(collection_name, limit=100)

            field_stats = {}
            for point in points_sample:
                if point.get("payload"):
                    for key, value in point["payload"].items():
                        if key not in field_stats:
                            field_stats[key] = {"count": 0, "types": set(), "samples": []}

                        field_stats[key]["count"] += 1
                        value_type = type(value).__name__
                        field_stats[key]["types"].add(value_type)

                        if len(field_stats[key]["samples"]) < 3:
                            field_stats[key]["samples"].append(
                                str(value)[:100] if value else None
                            )

            result = {}
            for field, stats in field_stats.items():
                result[field] = {
                    "occurrences": stats["count"],
                    "types": list(stats["types"]),
                    "samples": stats["samples"]
                }

            return result
        except Exception as e:
            logger.error(f"Ошибка получения статистики payload: {e}")
            return {"error": str(e)}

    def get_full_info(self, collection_name: str = "kag_documents") -> Dict[str, Any]:
        """
        Получить полную информацию о Qdrant.

        Args:
            collection_name: Имя коллекции (по умолчанию kag_documents)

        Returns:
            Полный отчёт о состоянии Qdrant
        """
        collections = self.get_collections_list()

        selected_collection = None
        for col in collections:
            if col["name"] == collection_name:
                selected_collection = self.get_collection_info(collection_name)
                break

        if not selected_collection or "error" in selected_collection:
            selected_collection = None

        points_sample = []
        payload_stats = {}
        if selected_collection:
            points_sample = self.get_points_sample(collection_name, limit=20)
            payload_stats = self.get_payload_stats(collection_name)

        return {
            "url": self.url,
            "collections": collections,
            "selected_collection": collection_name,
            "collection_info": selected_collection,
            "points_sample": points_sample,
            "payload_stats": payload_stats,
            "timestamp": datetime.utcnow().isoformat()
        }

    def get_collections_summary(self) -> List[Dict[str, Any]]:
        """
        Получить краткую сводку по всем коллекциям.

        Returns:
            Список коллекций с основной статистикой
        """
        try:
            if not self._ensure_client():
                return []

            collections = self._client.get_collections()
            result = []

            for col in collections.collections:
                info = self._client.get_collection(col.name)
                result.append({
                    "name": col.name,
                    "points_count": info.points_count,
                    "vectors_count": info.vectors_count,
                    "disk_size_mb": round(info.disk_size_bytes / 1024 / 1024, 2) if info.disk_size_bytes else 0,
                    "status": info.status
                })

            return result
        except Exception as e:
            logger.error(f"Ошибка получения сводки: {e}")
            return []


qdrant_monitor = QdrantMonitor()