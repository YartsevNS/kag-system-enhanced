"""
S3-слой хранения оригиналов документов (ADR-006).

Оригиналы документов KAG переезжают с локального диска /app/data/uploads/
на S3-совместимое хранилище (прототип: silo, бакеты regulator-docs /
company-docs, versioning=ENABLED).

- Ключ объекта: {document_id}/{filename} (стабильный, из БД документа).
- Бакет: bucket_for(document) — пользовательская загрузка → company-docs,
  web_monitor/источник с сайта → regulator-docs.
- В МЕТАДАННЫХ S3-объекта обязательно: uploaded_by, filename, document_id,
  source_name, source_url, collection, file_hash.
- Конфигурация через env (как TYPEDB_*): S3_ENDPOINT, S3_ACCESS_KEY,
  S3_SECRET_KEY, S3_BUCKET_COMPANY, S3_BUCKET_REGULATOR. Без env —
  is_available()=False, контур молча живёт на диске (прод без S3 не падает).
- Ошибки сети не роняют обработку: logger.warning + фолбэк на диск.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
    _BOTO3_IMPORTED = True
except Exception:  # pragma: no cover
    _BOTO3_IMPORTED = False


def _cfg() -> Dict[str, str]:
    return {
        "endpoint": os.getenv("S3_ENDPOINT", ""),
        "access_key": os.getenv("S3_ACCESS_KEY", ""),
        "secret_key": os.getenv("S3_SECRET_KEY", ""),
        "bucket_company": os.getenv("S3_BUCKET_COMPANY", "company-docs"),
        "bucket_regulator": os.getenv("S3_BUCKET_REGULATOR", "regulator-docs"),
    }


def is_available() -> bool:
    """S3 включён, только если задан endpoint и ключи (как guarded import TypeDB)."""
    if not _BOTO3_IMPORTED:
        return False
    cfg = _cfg()
    return bool(cfg["endpoint"] and cfg["access_key"] and cfg["secret_key"])


def bucket_for(source_metadata: Optional[Dict[str, Any]] = None,
               collection: str = "") -> str:
    """Выбор бакета: пользовательская загрузка → company-docs, с сайта → regulator-docs."""
    sm = source_metadata or {}
    # Ориентир как collection_for_source в document_service:
    # web_monitor ставит source_name/source_url → такие документы регуляторные.
    if sm.get("source_name") or sm.get("source_url") or collection == "regulator":
        return _cfg()["bucket_regulator"]
    return _cfg()["bucket_company"]


class S3Storage:
    """Минимальный S3-клиент для оригиналов документов (silо/OpenMaxIO, MinIO)."""

    def __init__(self):
        self._client = None
        self._bucket_cache: Dict[str, bool] = {}

    def _ensure(self):
        if self._client is None:
            cfg = _cfg()
            self._client = boto3.client(
                "s3",
                endpoint_url=cfg["endpoint"],
                aws_access_key_id=cfg["access_key"],
                aws_secret_access_key=cfg["secret_key"],
                region_name="us-east-1",
                use_ssl=False,
            )
        return self._client

    def _key(self, document_id: str, filename: str) -> str:
        return f"{document_id}/{filename}"

    def _ensure_bucket(self, bucket: str) -> None:
        c = self._bucket_cache.get(bucket)
        if c:
            return
        try:
            self._ensure().head_bucket(Bucket=bucket)
            self._bucket_cache[bucket] = True
        except (BotoCoreError, ClientError) as e:
            logger.warning(f"S3: бакет {bucket} недоступен: {e}")

    def put_original(self, document_id: str, filename: str, content: bytes, *,
                     bucket: str,
                     metadata: Optional[Dict[str, str]] = None) -> Optional[str]:
        """Положить оригинал в S3. Возвращает ключ объекта или None при ошибке."""
        if not is_available():
            return None
        key = self._key(document_id, filename)
        # S3 metadata допускает только ASCII: значения URL-кодируем (кириллические
        # имена файлов/источников валились бы иначе), при чтении декодируем.
        from urllib.parse import quote, unquote
        meta = {k: quote(str(v)) for k, v in (metadata or {}).items() if v is not None}
        try:
            self._ensure_bucket(bucket)
            self._ensure().put_object(
                Bucket=bucket, Key=key, Body=content,
                Metadata=meta or None,
            )
            logger.debug(f"S3: загружен s3://{bucket}/{key} ({len(content)} байт)")
            return key
        except (BotoCoreError, ClientError) as e:
            logger.warning(f"S3: не удалось загрузить {key}: {e}")
            return None

    def get_bytes(self, document_id: str, filename: str, *,
                  bucket: Optional[str] = None) -> Optional[bytes]:
        """Прочитать оригинал из S3 (все бакеты, если bucket не указан)."""
        if not is_available():
            return None
        candidates = ([bucket] if bucket else [
            _cfg()["bucket_company"], _cfg()["bucket_regulator"],
        ])
        for b in candidates:
            try:
                resp = self._ensure().get_object(
                    Bucket=b, Key=self._key(document_id, filename))
                body = resp.get("Body")
                if body is None:
                    continue
                return body.read()
            except (BotoCoreError, ClientError):
                continue
        return None

    def get_metadata(self, document_id: str, filename: str, *,
                     bucket: Optional[str] = None) -> Optional[Dict[str, str]]:
        """Метаданные S3-объекта (uploaded_by и т.д.) без чтения тела."""
        if not is_available():
            return None
        from urllib.parse import unquote
        candidates = ([bucket] if bucket else [
            _cfg()["bucket_company"], _cfg()["bucket_regulator"],
        ])
        for b in candidates:
            try:
                resp = self._ensure().head_object(
                    Bucket=b, Key=self._key(document_id, filename))
                md = resp.get("Metadata") or {}
                return {k: unquote(str(v)) for k, v in md.items()}
            except (BotoCoreError, ClientError):
                continue
        return None

    def delete_original(self, document_id: str, filename: str, *,
                        bucket: Optional[str] = None) -> bool:
        """Удалить объект(ы) из S3. При versioning=ENABLED это delete marker."""
        if not is_available():
            return False
        candidates = ([bucket] if bucket else [
            _cfg()["bucket_company"], _cfg()["bucket_regulator"],
        ])
        deleted = False
        for b in candidates:
            try:
                self._ensure().delete_object(
                    Bucket=b, Key=self._key(document_id, filename))
                deleted = True
            except (BotoCoreError, ClientError):
                continue
        return deleted

    def exists(self, document_id: str, filename: str, *,
               bucket: Optional[str] = None) -> bool:
        resp = self.get_metadata(document_id, filename, bucket=bucket)
        return resp is not None


# Глобальный экземпляр
s3_storage = S3Storage()