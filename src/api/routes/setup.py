"""
Setup Wizard API для KAG

Обрабатывает первоначальную настройку системы.
Все данные сохраняются в PostgreSQL через config_store.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from loguru import logger
from datetime import datetime
from typing import Optional
import httpx
import asyncio
import subprocess
import secrets
import string

from src.api.services.config_store import config_store
from src.security.gost_crypto import GOSTCrypto

router = APIRouter(prefix="/setup", tags=["setup"])


# ==========================================
# Pydantic модели
# ==========================================

class DatabaseSetup(BaseModel):
    host: str
    port: int = 5432
    name: str
    user: str
    password: str


class LlmSetup(BaseModel):
    type: str = "ollama"
    host: str
    port: int = 11434
    model: str
    system_prompt: Optional[str] = "Ты - AI-ассистент. Отвечай на вопросы точно и по существу."


class EmbeddingSetup(BaseModel):
    model: str = "qwen3-embedding:4b"
    dimensions: int = 4096


class SshSetup(BaseModel):
    username: str
    password: str
    sudo_password: Optional[str] = None


class FullSetupPayload(BaseModel):
    database: DatabaseSetup
    llm: LlmSetup
    embedding: EmbeddingSetup
    ssh: SshSetup


# ==========================================
# Генератор безопасных паролей
# ==========================================

def _generate_password(length: int = 24) -> str:
    """Генерирует криптографически безопасный пароль."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return ''.join(secrets.choice(alphabet) for _ in range(length))


# ==========================================
# Новые эндпоинты: создание БД
# ==========================================

@router.post("/create-pg-db")
async def create_pg_database():
    """
    Создаёт новую базу данных и пользователя PostgreSQL.
    
    Генерирует безопасный пароль, создаёт БД и пользователя,
    сохраняет учётные данные в config_store (зашифрованные).
    Возвращает сгенерированные данные (показать один раз!).
    """
    try:
        import psycopg2
        
        # Генерируем имя БД и пароль
        db_name = f"kag_{secrets.token_hex(4)}"
        db_user = f"kag_user_{secrets.token_hex(4)}"
        db_password = _generate_password(24)
        
        # Подключаемся к PostgreSQL с bootstrap-учёткой
        settings = __import__('src.config', fromlist=['get_settings']).get_settings()
        conn = psycopg2.connect(
            host="keycloak-db",
            port=5432,
            dbname="keycloak",
            user=settings.KC_DB_USERNAME,
            password=settings.KC_DB_PASSWORD,
            connect_timeout=10
        )
        conn.autocommit = True
        cur = conn.cursor()
        
        # Создаём пользователя и БД
        cur.execute(f"CREATE USER {db_user} WITH PASSWORD '{db_password}'")
        cur.execute(f"CREATE DATABASE {db_name} OWNER {db_user}")
        cur.execute(f"GRANT ALL PRIVILEGES ON DATABASE {db_name} TO {db_user}")
        cur.close()
        conn.close()
        
        # Сохраняем в config_store (пароль шифруется)
        crypto = GOSTCrypto()
        now = datetime.utcnow().isoformat()
        
        db_config = {
            "host": "keycloak-db",
            "port": 5432,
            "name": db_name,
            "user": db_user,
            "password": crypto.encrypt_to_base64(db_password),
            "saved_at": now,
            "auto_created": True
        }
        config_store.set("database", "default", db_config)
        
        logger.info(f"PostgreSQL БД создана: {db_name}, пользователь: {db_user}")
        
        return {
            "success": True,
            "message": "База данных PostgreSQL создана",
            "database": {
                "host": "keycloak-db",
                "port": 5432,
                "name": db_name,
                "user": db_user,
                "password": db_password  # Показать один раз!
            }
        }
    except ImportError:
        return {"success": False, "message": "psycopg2 не установлен"}
    except Exception as e:
        logger.error(f"Ошибка создания PG БД: {e}")
        return {"success": False, "message": str(e)}


@router.post("/create-qdrant-collection")
async def create_qdrant_collection():
    """
    Настраивает коллекцию Qdrant.
    
    Создаёт коллекцию для документов KAG.
    Генерирует API ключ (если Qdrant поддерживает) и сохраняет в config_store.
    """
    try:
        from qdrant_client import QdrantClient
        from qdrant_client.models import Distance, VectorParams, OptimizersConfigDiff
        
        settings = __import__('src.config', fromlist=['get_settings']).get_settings()
        
        client = QdrantClient(host="qdrant", port=6333)
        
        collection_name = "kag_documents"
        vector_size = settings.EMBEDDING_DIMENSIONS if hasattr(settings, 'EMBEDDING_DIMENSIONS') else 768
        
        # Проверяем, существует ли коллекция
        collections = client.get_collections().collections
        existing = [c.name for c in collections]
        
        if collection_name not in existing:
            client.create_collection(
                collection_name=collection_name,
                vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
                optimizers_config=OptimizersConfigDiff(default_segment_number=2)
            )
            logger.info(f"Qdrant коллекция создана: {collection_name}")
        else:
            logger.info(f"Qdrant коллекция уже существует: {collection_name}")
        
        # Генерируем API ключ для доступа (сохраняем как пароль)
        api_key = _generate_password(32)
        
        crypto = GOSTCrypto()
        now = datetime.utcnow().isoformat()
        
        qdrant_config = {
            "host": "qdrant",
            "port": 6333,
            "collection": collection_name,
            "vector_size": vector_size,
            "api_key": crypto.encrypt_to_base64(api_key),
            "saved_at": now,
            "auto_created": True
        }
        config_store.set("qdrant", "default", qdrant_config)
        
        logger.info(f"Qdrant настроен: коллекция {collection_name}, размер вектора {vector_size}")
        
        return {
            "success": True,
            "message": "Qdrant настроен",
            "qdrant": {
                "host": "qdrant",
                "port": 6333,
                "collection": collection_name,
                "vector_size": vector_size,
                "api_key": api_key  # Показать один раз!
            }
        }
    except ImportError:
        return {"success": False, "message": "qdrant-client не установлен"}
    except Exception as e:
        logger.error(f"Ошибка настройки Qdrant: {e}")
        return {"success": False, "message": str(e)}


@router.get("/status")
async def setup_status():
    """Возвращает полный статус настройки: что создано, что нет."""
    status = {
        "database": bool(config_store.get("database", "default")),
        "qdrant": bool(config_store.get("qdrant", "default")),
        "llm": bool(config_store.get("llm", "default")),
        "configured": config_store.get("setup", "status", {}).get("configured", False)
    }
    return {"success": True, "status": status}


# ==========================================
# Тестовые эндпоинты (существующие)
# ==========================================

@router.post("/test-db")
async def test_db(cfg: DatabaseSetup):
    """Тестирует подключение к PostgreSQL."""
    try:
        import psycopg2
        conn = psycopg2.connect(
            host=cfg.host,
            port=cfg.port,
            dbname=cfg.name,
            user=cfg.user,
            password=cfg.password,
            connect_timeout=10
        )
        conn.close()
        return {"success": True, "message": f"Подключено к {cfg.host}:{cfg.port}/{cfg.name}"}
    except ImportError:
        return {"success": False, "message": "psycopg2 не установлен"}
    except Exception as e:
        return {"success": False, "message": str(e)}


@router.post("/test-llm")
async def test_llm(cfg: LlmSetup):
    """Тестирует доступность LLM сервера."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            if cfg.type == "ollama":
                resp = await client.get(f"http://{cfg.host}:{cfg.port}/api/tags")
                if resp.status_code != 200:
                    return {"success": False, "message": f"Ollama ошибка: {resp.status_code}"}
                
                models = resp.json().get("models", [])
                model_names = [m["name"] for m in models]
                
                if cfg.model in model_names:
                    return {"success": True, "message": f"Модель {cfg.model} доступна"}
                else:
                    return {"success": False, "message": f"Модель не найдена. Доступны: {', '.join(model_names[:3])}..."}
            else:
                resp = await client.get(f"http://{cfg.host}:{cfg.port}/health")
                if resp.status_code == 200:
                    return {"success": True, "message": "vLLM сервер доступен"}
                else:
                    return {"success": False, "message": f"vLLM ошибка: {resp.status_code}"}
    except httpx.ConnectError:
        return {"success": False, "message": f"Не удалось подключиться к {cfg.host}:{cfg.port}"}
    except Exception as e:
        return {"success": False, "message": str(e)}


@router.post("/test-ssh")
async def test_ssh(cfg):
    """Тестирует SSH подключение."""
    try:
        username = cfg.username
        password = cfg.password
        llm_host = cfg.llm_host
        llm_port = cfg.llm_port
        sudo_pass = cfg.sudo_password if hasattr(cfg, 'sudo_password') else None
        
        cmd = f"sshpass -p '{password}' ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 {username}@{llm_host} 'echo OK'"
        
        result = await asyncio.to_thread(
            subprocess.run,
            cmd, shell=True, capture_output=True, text=True, timeout=15
        )
        
        if result.returncode != 0:
            return {"success": False, "message": f"SSH ошибка: {result.stderr.strip()[:200]}"}
        
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"http://{llm_host}:{llm_port}/")
                ollama_ok = resp.status_code == 200
        except:
            ollama_ok = False
        
        msg = "SSH подключено"
        if ollama_ok:
            msg += " • Ollama работает"
        
        return {"success": True, "message": msg}
    except Exception as e:
        return {"success": False, "message": str(e)}


# ==========================================
# Основной эндпоинт сохранения
# ==========================================

@router.post("/save")
async def save_setup(payload: FullSetupPayload):
    """
    Сохраняет все настройки системы в PostgreSQL.
    Это финальный шаг Setup Wizard.
    """
    logger.info("=== НАЧАЛО СОХРАНЕНИЯ НАСТРОЕК ===")
    
    try:
        crypto = GOSTCrypto()
        now = datetime.utcnow().isoformat()
        
        # 1. Сохраняем настройки БД
        logger.info("Сохраняем настройки БД...")
        db_data = payload.database.model_dump()
        db_data['password'] = crypto.encrypt_to_base64(db_data['password'])
        db_data['saved_at'] = now
        config_store.set("database", "default", db_data)
        
        # 2. Сохраняем настройки LLM
        logger.info("Сохраняем настройки LLM...")
        llm_data = payload.llm.model_dump()
        llm_data['saved_at'] = now
        config_store.set("llm", "default", llm_data)
        
        # 3. Сохраняем настройки Embedding
        logger.info("Сохраняем настройки Embedding...")
        emb_data = payload.embedding.model_dump()
        emb_data['saved_at'] = now
        config_store.set("embedding", "default", emb_data)
        
        # 4. Сохраняем настройки SSH
        logger.info("Сохраняем настройки SSH...")
        ssh_data = payload.ssh.model_dump()
        ssh_data['password'] = crypto.encrypt_to_base64(ssh_data['password'])
        if ssh_data.get('sudo_password'):
            ssh_data['sudo_password'] = crypto.encrypt_to_base64(ssh_data['sudo_password'])
        ssh_data['host'] = payload.llm.host
        ssh_data['ollama_port'] = payload.llm.port
        ssh_data['saved_at'] = now
        config_store.set("ssh", "default", ssh_data)
        
        # 5. Ставим маркер: система настроена
        logger.info("Ставим маркер настройки...")
        config_store.set("setup", "status", {
            "configured": True,
            "timestamp": now,
            "llm_model": payload.llm.model,
            "db_host": payload.database.host
        })
        
        logger.info("=== НАСТРОЙКИ УСПЕШНО СОХРАНЕНЫ ===")
        
        return {
            "success": True,
            "message": "Настройки сохранены. Система готова к работе."
        }
        
    except Exception as e:
        logger.error(f"!!! ОШИБКА СОХРАНЕНИЯ: {e}")
        import traceback
        logger.error(traceback.format_exc())
        
        raise HTTPException(status_code=500, detail=str(e))


# ==========================================
# Проверка статуса настройки
# ==========================================

@router.get("/check")
async def check_status():
    """Проверяет, была ли выполнена первоначальная настройка."""
    try:
        status = config_store.get("setup", "status", {})
        return {
            "configured": status.get("configured", False),
            "timestamp": status.get("timestamp")
        }
    except Exception as e:
        logger.warning(f"Ошибка проверки статуса: {e}")
        return {"configured": False, "timestamp": None}
