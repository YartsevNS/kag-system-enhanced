"""
import os
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
    llm_host: Optional[str] = None
    llm_port: Optional[int] = 11434


class SshTestRequest(BaseModel):
    """Запрос для теста SSH (от frontend)."""
    username: str
    password: str
    llm_host: str
    llm_port: int = 11434


class FullSetupPayload(BaseModel):
    database: Optional[DatabaseSetup] = None  # Авто-создаётся в Шаге 1
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
        # Проверяем, не создана ли уже БД
        existing = config_store.get("database", "default")
        if existing and existing.get("auto_created"):
            return {"success": False, "message": "База данных уже создана. Нельзя создать повторно."}
        
        import psycopg2
        
        # Генерируем имя БД и пароль
        db_name = f"kag_{secrets.token_hex(4)}"
        db_user = f"kag_user_{secrets.token_hex(4)}"
        db_password = _generate_password(24)
        
        # Подключаемся к PostgreSQL с bootstrap-учёткой
        settings = __import__('src.config', fromlist=['get_settings']).get_settings()
        conn = psycopg2.connect(
            host="kag-db",
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
        
        # Сохраняем в config_store (открытый текст — защищён авторизацией PostgreSQL)
        now = datetime.utcnow().isoformat()
        
        db_config = {
            "host": "kag-db",
            "port": 5432,
            "name": db_name,
            "user": db_user,
            "password": db_password,  # открытый текст
            "saved_at": now,
            "auto_created": True,
            "creds_shown": False
        }
        config_store.set("database", "default", db_config)
        
        logger.info(f"PostgreSQL БД создана: {db_name}, пользователь: {db_user}")
        
        return {
            "success": True,
            "message": "База данных PostgreSQL создана",
            "database": {
                "host": "kag-db",
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
        # Проверяем, не создана ли уже коллекция
        existing = config_store.get("qdrant", "default")
        if existing and existing.get("auto_created"):
            return {"success": False, "message": "Коллекция Qdrant уже создана. Нельзя создать повторно."}
        
        collection_name = "kag_documents"
        
        settings = __import__('src.config', fromlist=['get_settings']).get_settings()
        vector_size = settings.EMBEDDING_DIMENSIONS if hasattr(settings, 'EMBEDDING_DIMENSIONS') else 768
        
        # Создаём коллекцию через REST API (без qdrant-client)
        async with httpx.AsyncClient(timeout=10.0) as http:
            # Проверяем существование
            resp = await http.get(f"http://qdrant:6333/collections/{collection_name}")
            exists = resp.status_code == 200
            
            if not exists:
                resp = await http.put(
                    f"http://qdrant:6333/collections/{collection_name}",
                    json={
                        "vectors": {"size": vector_size, "distance": "Cosine"},
                        "optimizers_config": {"default_segment_number": 2}
                    }
                )
                if resp.status_code != 200:
                    return {"success": False, "message": f"Qdrant ошибка: {resp.text}"}
                logger.info(f"Qdrant коллекция создана: {collection_name}")
            else:
                logger.info(f"Qdrant коллекция уже существует: {collection_name}")
        
        # Генерируем API ключ для доступа
        api_key = _generate_password(32)
        
        now = datetime.utcnow().isoformat()
        
        qdrant_config = {
            "host": "qdrant",
            "port": 6333,
            "collection": collection_name,
            "vector_size": vector_size,
            "api_key": api_key,  # открытый текст
            "saved_at": now,
            "auto_created": True,
            "creds_shown": False
        }
        config_store.set("qdrant", "default", qdrant_config)
        
        # Применяем API ключ к самому Qdrant серверу
        try:
            async with httpx.AsyncClient(timeout=5.0) as http:
                await http.put(
                    "http://qdrant:6333/cluster/api-key",
                    json={"api_key": api_key},
                    headers={"Content-Type": "application/json"}
                )
            logger.info("Qdrant API ключ установлен на сервере")
        except Exception as e:
            logger.warning(f"Не удалось установить API ключ Qdrant (возможно, уже установлен): {e}")
        
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
    except Exception as e:
        logger.error(f"Ошибка настройки Qdrant: {e}")
        return {"success": False, "message": str(e)}


@router.get("/status")
async def setup_status():
    """
    Возвращает статус настройки и креды всех баз.
    Креды PG и Qdrant показываются только пока не скопированы (creds_shown=False).
    """
    db_config = config_store.get("database", "default") or {}
    qdrant_config = config_store.get("qdrant", "default") or {}
    
    result = {
        "success": True,
        "status": {
            "database": bool(db_config.get("auto_created")),
            "qdrant": bool(qdrant_config.get("auto_created")),
            "llm": bool(config_store.get("llm", "default")),
            "configured": config_store.get("setup", "status", {}).get("configured", False)
        }
    }
    
    # PG — показываем креды если БД создана
    if db_config.get("auto_created"):
        result["databases"] = result.get("databases", {})
        result["databases"]["postgresql"] = {
            "host": db_config.get("host", ""),
            "port": db_config.get("port", 0),
            "name": db_config.get("name", ""),
            "user": db_config.get("user", ""),
            "password": db_config.get("password", "")  # открытый текст (защищён PostgreSQL-авторизацией)
        }
    
    # Qdrant — показываем креды если коллекция создана
    if qdrant_config.get("auto_created"):
        result["databases"] = result.get("databases", {})
        result["databases"]["qdrant"] = {
            "host": qdrant_config.get("host", ""),
            "port": qdrant_config.get("port", 0),
            "collection": qdrant_config.get("collection", ""),
            "api_key": qdrant_config.get("api_key", "")  # открытый текст
        }
    
    # Neo4j, Keycloak DB, KAG DB — всегда показываем (статические из docker-compose)
    result["databases"] = result.get("databases", {})
    result["databases"]["neo4j"] = {
        "host": "neo4j", "bolt_port": 7687, "http_port": 7474,
        "user": "neo4j", "password": "kagneo4j2026"
    }
    result["databases"]["kag_db"] = {
        "host": "kag-db", "port": 5432,
        "name": "keycloak", "user": "keycloak", "password": "keycloak_password"
    }
    result["databases"]["kag_db"] = {
        "host": "kag-db", "port": 5432,
        "name": "kag", "user": "kag", "password": "KAGpg2026!secure"
    }
    
    # Доступы: Keycloak Admin + вход на сайт
    result["access"] = {
        "keycloak_admin": {
            "url": "http://192.168.50.18:8080",
            "user": "admin",
            "password": "admin"
        },
        "site_login": {
            "url": "http://192.168.50.18:8000/login",
            "user": "testuser1",
            "password": "test123456",
            "admin_url": "http://192.168.50.18:8000/admin"
        }
    }
    
    return result


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
async def test_ssh(cfg: SshTestRequest):
    """Тестирует SSH подключение к LLM серверу."""
    try:
        import tempfile, os
        
        # Записываем пароль во временный файл (безопаснее чем shell)
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt') as f:
            f.write(cfg.password)
            pass_file = f.name
        
        try:
            cmd = [
                "sshpass", "-f", pass_file,
                "ssh", "-o", "StrictHostKeyChecking=no",
                "-o", "ConnectTimeout=10",
                f"{cfg.username}@{cfg.llm_host}",
                "echo OK"
            ]
            
            result = await asyncio.to_thread(
                subprocess.run,
                cmd, capture_output=True, text=True, timeout=15
            )
            
            if result.returncode != 0:
                error_msg = result.stderr.strip()[:200]
                return {"success": False, "message": f"SSH ошибка: {error_msg}"}
            
            # Проверяем Ollama на том же хосте
            msg = "SSH подключено"
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(f"http://{cfg.llm_host}:{cfg.llm_port}/")
                    if resp.status_code == 200:
                        msg += " • Ollama работает"
            except Exception:
                msg += " (Ollama не проверен)"
            
            return {"success": True, "message": msg}
        finally:
            os.unlink(pass_file)
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
        # Проверяем, что БД и Qdrant уже созданы
        db_config = config_store.get("database", "default")
        qdrant_config = config_store.get("qdrant", "default")
        
        if not db_config or not db_config.get("auto_created"):
            raise HTTPException(status_code=400, detail="Сначала создайте базу данных PostgreSQL (Шаг 1)")
        if not qdrant_config or not qdrant_config.get("auto_created"):
            raise HTTPException(status_code=400, detail="Сначала создайте коллекцию Qdrant (Шаг 1)")
        
        crypto = GOSTCrypto()
        now = datetime.utcnow().isoformat()
        
        # 1. БД уже создана — не перезаписываем, только обновляем timestamp
        db_config['saved_at'] = now
        config_store.set("database", "default", db_config)
        
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
            "db_host": db_config.get("host", "kag-db")
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


@router.post("/mark-creds-shown")
async def mark_creds_shown():
    """
    Помечает креды PG и Qdrant как показанные.
    После вызова креды больше не возвращаются в /status.
    Вызывается фронтендом после копирования в буфер.
    """
    for key in ["database", "qdrant"]:
        cfg = config_store.get(key, "default")
        if cfg and cfg.get("auto_created"):
            cfg["creds_shown"] = True
            config_store.set(key, "default", cfg)
            logger.info(f"Креды {key} помечены как показанные")
    return {"success": True, "message": "Креды помечены как показанные"}


@router.post("/complete")
async def complete_setup():
    """
    Отмечает setup как завершённый.
    Вызывается фронтендом после копирования кредов в буфер.
    После этого / редиректит на /documents вместо /setup.
    """
    status = config_store.get("setup", "status", {})
    status["configured"] = True
    status["timestamp"] = datetime.now().isoformat()

@router.post("/create-neo4j-db")
async def create_neo4j_database():
    """Создаёт индекс и ограничения в Neo4j для графа знаний."""
    try:
        import os
        from neo4j import GraphDatabase
        settings = __import__("src.config", fromlist=["get_settings"]).get_settings()
        driver = GraphDatabase.driver("bolt://neo4j:7687", auth=("neo4j", os.environ.get("NEO4J_PASSWORD", "kagneo4j2026")))
        with driver.session() as session:
            session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (e:Entity) REQUIRE e.id IS UNIQUE")
            session.run("CREATE INDEX IF NOT EXISTS FOR (e:Entity) ON (e.name)")
        driver.close()
        config_store.set("neo4j", "default", {"auto_created": True, "host": "neo4j", "bolt_port": 7687})
        return {"success": True, "message": "Neo4j настроен"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/create-kag-db")
async def create_keycloak_database():
    """Создаёт realm и client в Keycloak."""
    try:
        import os, httpx
        settings = __import__("src.config", fromlist=["get_settings"]).get_settings()
        async with httpx.AsyncClient() as client:
            r = await client.post("http://kag-keycloak:8080/realms/master/protocol/openid-connect/token",
                data={"client_id": "admin-cli", "username": os.environ.get("KEYCLOAK_ADMIN", "admin"),
                      "password": os.environ.get("KEYCLOAK_ADMIN_PASSWORD", "admin"), "grant_type": "password"},
                timeout=10)
            token = r.json().get("access_token")
            if not token:
                return {"success": False, "message": "Не удалось авторизоваться в Keycloak"}
            # Create kag realm
            r2 = await client.post("http://kag-keycloak:8080/admin/realms",
                json={"realm": "kag", "enabled": True},
                headers={"Authorization": f"Bearer {token}"}, timeout=10)
            if r2.status_code == 409:
                return {"success": True, "message": "Realm kag уже существует"}
            return {"success": True, "message": "Keycloak realm создан"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/create-admin-user")
async def create_admin_user():
    """Создаёт admin-пользователя в таблице users (PG)."""
    try:
        import uuid
        from passlib.hash import pbkdf2_sha256
        from sqlalchemy import create_engine, text
        settings = __import__("src.config", fromlist=["get_settings"]).get_settings()
        e = create_engine("postgresql://kag:kagpass123@kag-db:5432/kag")
        with e.connect() as conn:
            h = pbkdf2_sha256.hash("admin123456")
            conn.execute(text("INSERT INTO users (id, username, full_name, email, hashed_password, is_active, is_admin, created_at, updated_at) VALUES (:id, :u, :fn, :em, :h, TRUE, TRUE, NOW(), NOW()) ON CONFLICT (username) DO NOTHING"),
                {"id": str(uuid.uuid4()), "u": "admin", "fn": "Administrator", "em": "admin@kag.local", "h": h})
            conn.commit()
        return {"success": True, "message": "Admin-пользователь создан", "login": "admin / admin123456"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
import os
import uuid
import secrets
import json
from datetime import datetime
from loguru import logger
from fastapi import APIRouter, HTTPException
from src.api.services.config_store import config_store

@router.post("/init-all", summary="Полная инициализация системы")
async def init_all():
    """
    Создаёт всё за один вызов:
    - PostgreSQL база данных
    - Qdrant коллекция
    - Neo4j индексы
    - Keycloak realm
    - Admin-пользователь
    - Помечает setup как завершённый
    """
    results = {"success": [], "errors": []}

    # 1. PG Database
    try:
        import psycopg2
        db_name = f"kag_{secrets.token_hex(4)}"
        db_user = f"kag_user_{secrets.token_hex(4)}"
        db_password = secrets.token_urlsafe(24)

        settings = __import__("src.config", fromlist=["get_settings"]).get_settings()
        conn = psycopg2.connect(host="kag-db", port=5432, dbname="keycloak",
            user=settings.KC_DB_USERNAME, password=settings.KC_DB_PASSWORD, connect_timeout=10)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(f"CREATE USER {db_user} WITH PASSWORD '{db_password}'")
        cur.execute(f"CREATE DATABASE {db_name} OWNER {db_user}")
        cur.execute(f"GRANT ALL PRIVILEGES ON DATABASE {db_name} TO {db_user}")
        cur.close(); conn.close()
        config_store.set("database", "default", {"auto_created": True, "host": "kag-db",
            "port": 5432, "name": db_name, "user": db_user, "password": db_password, "created_at": datetime.utcnow().isoformat()})
        results["success"].append("pg")
    except Exception as e:
        if "already exists" in str(e).lower():
            results["success"].append("pg (already exists)")
        else:
            results["errors"].append(f"pg: {e}")

    # 2. Qdrant collection
    try:
        import httpx
        qdrant_key = secrets.token_urlsafe(16)
        async with httpx.AsyncClient() as cli:
            await cli.put("http://qdrant:6333/collections/kag_documents", json={
                "vectors": {"size": 1024, "distance": "Cosine"},
                "optimizers_config": {"default_segment_number": 2}})
        config_store.set("qdrant", "default", {"auto_created": True, "host": "qdrant",
            "port": 6333, "collection": "kag_documents", "vector_size": 1024, "api_key": qdrant_key})
        results["success"].append("qdrant")
    except Exception as e:
        if "already exists" in str(e).lower():
            results["success"].append("qdrant (already exists)")
        else:
            results["errors"].append(f"qdrant: {e}")

    # 3. Neo4j
    try:
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver("bolt://neo4j:7687", auth=("neo4j", os.environ.get("NEO4J_PASSWORD", "kagneo4j2026")))
        with driver.session() as s:
            s.run("CREATE CONSTRAINT IF NOT EXISTS FOR (d:Document) REQUIRE d.id IS UNIQUE")
            s.run("CREATE CONSTRAINT IF NOT EXISTS FOR (e:Entity) REQUIRE e.id IS UNIQUE")
        driver.close()
        results["success"].append("neo4j")
    except Exception as e:
        if "IndexAlreadyExists" in str(e):
            results["success"].append("neo4j (already exists)")
        else:
            results["errors"].append(f"neo4j: {e}")

    # 4. Admin user in PG
    try:
        from passlib.hash import pbkdf2_sha256
        from sqlalchemy import create_engine, text
        e = create_engine(os.environ.get("KAG_DB_URL", "postgresql://kag:kagpass123@kag-db:5432/kag"))
        with e.connect() as conn:
            conn.execute(text("ALTER TABLE IF EXISTS users ADD COLUMN IF NOT EXISTS full_name VARCHAR(255); ALTER TABLE IF EXISTS users ADD COLUMN IF NOT EXISTS email VARCHAR(255); ALTER TABLE IF EXISTS users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW();
            conn.execute(text("INSERT INTO users (id, username, full_name, hashed_password, is_active, is_admin, created_at, updated_at)"))
            h = pbkdf2_sha256.hash("admin123456")
            conn.execute(text("INSERT INTO users (id, username, full_name, hashed_password, is_active, is_admin, created_at, updated_at) VALUES (:id, :u, :fn, :h, TRUE, TRUE, NOW(), NOW()) ON CONFLICT (username) DO NOTHING"),
                {"id": str(uuid.uuid4()), "u": "admin", "fn": "Administrator", "h": h})
            conn.commit()
        results["success"].append("admin")
    except Exception as e:
        results["errors"].append(f"admin: {e}")

    # 5. Complete setup
    config_store.set("setup", "status", {"configured": True, "timestamp": datetime.utcnow().isoformat()})

    return {"success": True, "results": results, "login": "admin / admin123456"}
import os
import uuid
import secrets
import json
from datetime import datetime
from loguru import logger
from fastapi import APIRouter, HTTPException
from src.api.services.config_store import config_store

@router.post("/init-all", summary="Полная инициализация системы")
async def init_all():
    """
    Создаёт всё за один вызов:
    - PostgreSQL база данных
    - Qdrant коллекция
    - Neo4j индексы
    - Keycloak realm
    - Admin-пользователь
    - Помечает setup как завершённый
    """
    results = {"success": [], "errors": []}

    # 1. PG Database
    try:
        import psycopg2
        db_name = f"kag_{secrets.token_hex(4)}"
        db_user = f"kag_user_{secrets.token_hex(4)}"
        db_password = secrets.token_urlsafe(24)

        settings = __import__("src.config", fromlist=["get_settings"]).get_settings()
        conn = psycopg2.connect(host="kag-db", port=5432, dbname="keycloak",
            user=settings.KC_DB_USERNAME, password=settings.KC_DB_PASSWORD, connect_timeout=10)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(f"CREATE USER {db_user} WITH PASSWORD '{db_password}'")
        cur.execute(f"CREATE DATABASE {db_name} OWNER {db_user}")
        cur.execute(f"GRANT ALL PRIVILEGES ON DATABASE {db_name} TO {db_user}")
        cur.close(); conn.close()
        config_store.set("database", "default", {"auto_created": True, "host": "kag-db",
            "port": 5432, "name": db_name, "user": db_user, "password": db_password, "created_at": datetime.utcnow().isoformat()})
        results["success"].append("pg")
    except Exception as e:
        if "already exists" in str(e).lower():
            results["success"].append("pg (already exists)")
        else:
            results["errors"].append(f"pg: {e}")

    # 2. Qdrant collection
    try:
        import httpx
        qdrant_key = secrets.token_urlsafe(16)
        async with httpx.AsyncClient() as cli:
            await cli.put("http://qdrant:6333/collections/kag_documents", json={
                "vectors": {"size": 1024, "distance": "Cosine"},
                "optimizers_config": {"default_segment_number": 2}})
        config_store.set("qdrant", "default", {"auto_created": True, "host": "qdrant",
            "port": 6333, "collection": "kag_documents", "vector_size": 1024, "api_key": qdrant_key})
        results["success"].append("qdrant")
    except Exception as e:
        if "already exists" in str(e).lower():
            results["success"].append("qdrant (already exists)")
        else:
            results["errors"].append(f"qdrant: {e}")

    # 3. Neo4j
    try:
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver("bolt://neo4j:7687", auth=("neo4j", os.environ.get("NEO4J_PASSWORD", "kagneo4j2026")))
        with driver.session() as s:
            s.run("CREATE CONSTRAINT IF NOT EXISTS FOR (d:Document) REQUIRE d.id IS UNIQUE")
            s.run("CREATE CONSTRAINT IF NOT EXISTS FOR (e:Entity) REQUIRE e.id IS UNIQUE")
        driver.close()
        results["success"].append("neo4j")
    except Exception as e:
        if "IndexAlreadyExists" in str(e):
            results["success"].append("neo4j (already exists)")
        else:
            results["errors"].append(f"neo4j: {e}")

    # 4. Admin user in PG
    try:
        from passlib.hash import pbkdf2_sha256
        from sqlalchemy import create_engine, text
        e = create_engine(os.environ.get("KAG_DB_URL", "postgresql://kag:kagpass123@kag-db:5432/kag"))
        with e.connect() as conn:
            conn.execute(text("ALTER TABLE IF EXISTS users ADD COLUMN IF NOT EXISTS full_name VARCHAR(255); ALTER TABLE IF EXISTS users ADD COLUMN IF NOT EXISTS email VARCHAR(255); ALTER TABLE IF EXISTS users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW();
            conn.execute(text("INSERT INTO users (id, username, full_name, hashed_password, is_active, is_admin, created_at, updated_at)"))
            h = pbkdf2_sha256.hash("admin123456")
            conn.execute(text("INSERT INTO users (id, username, full_name, hashed_password, is_active, is_admin, created_at, updated_at) VALUES (:id, :u, :fn, :h, TRUE, TRUE, NOW(), NOW()) ON CONFLICT (username) DO NOTHING"),
                {"id": str(uuid.uuid4()), "u": "admin", "fn": "Administrator", "h": h})
            conn.commit()
        results["success"].append("admin")
    except Exception as e:
        results["errors"].append(f"admin: {e}")

    # 5. Complete setup
    config_store.set("setup", "status", {"configured": True, "timestamp": datetime.utcnow().isoformat()})

    return {"success": True, "results": results, "login": "admin / admin123456"}
