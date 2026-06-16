"""
Setup Wizard API для KAG

Инициализация системы одной кнопкой:
- Создаёт БД (PostgreSQL, Qdrant, Neo4j) со сгенерированными паролями
- Создаёт admin-пользователя
- Пароли БД НЕ сохраняются — только в ответе и в скачанном файле
- Пароль admin сохраняется в config_store (нужен для входа)
"""

from fastapi import APIRouter, HTTPException
from loguru import logger
from datetime import datetime
import os
import secrets
import string
import hashlib
import passlib.hash as hash_methods

from src.api.services.config_store import config_store

router = APIRouter(prefix="/setup", tags=["setup"])


# ── Генератор ──────────────────────────────────────────────────────────────

def _gen_password(length: int = 20) -> str:
    """Сгенерировать пароль без спецсимволов, ломающих SQL."""
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))


# ── POST /init-all ─────────────────────────────────────────────────────────

@router.post("/init-all")
async def initialize_all():
    """
    Полная инициализация системы.

    1. PostgreSQL — создаёт БД kag + пользователя kag
    2. Qdrant — создаёт коллекцию kag_documents
    3. Neo4j — создаёт индексы
    4. Admin — создаёт пользователя admin

    Пароли БД возвращаются в ответе и НЕ сохраняются.
    Пароль admin сохраняется в config_store.
    """
    logger.info("=== SETUP: INIT-ALL ===")

    # Проверка: уже настроено?
    existing = config_store.get("setup", "status", {})
    if existing.get("configured"):
        return {"success": True, "message": "Система уже настроена", "already_configured": True}

    credentials = {}

    # Генерируем пароли
    pg_password = _gen_password(20)
    ne_password = _gen_password(20)
    ad_password = _gen_password(12)

    # 1. PostgreSQL
    try:
        import psycopg2

        conn = psycopg2.connect(
            host="kag-db", port=5432, dbname="keycloak",
            user=os.environ.get("KC_DB_USERNAME", "keycloak"),
            password=os.environ.get("KC_DB_PASSWORD", "keycloak_password"),
            connect_timeout=10,
        )
        conn.autocommit = True
        cur = conn.cursor()

        # Пользователь kag — если есть, меняем пароль; если нет — создаём
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname='kag'")
        if cur.fetchone():
            cur.execute(f"ALTER USER kag WITH PASSWORD '{pg_password}'")
            logger.info("SETUP: PG user kag exists — password updated")
        else:
            cur.execute(f"CREATE USER kag WITH PASSWORD '{pg_password}'")
            logger.info("SETUP: PG user kag created")

        # БД kag
        cur.execute("SELECT 1 FROM pg_database WHERE datname='kag'")
        if not cur.fetchone():
            cur.execute("CREATE DATABASE kag OWNER kag")
            cur.execute("GRANT ALL PRIVILEGES ON DATABASE kag TO kag")

        cur.close()
        conn.close()

        # Обновляем KAG_DB_URL в config_store с реальным паролем
        db_url = f"postgresql://kag:{pg_password}@kag-db:5432/kag"
        os.environ["KAG_DB_URL"] = db_url
        config_store._db_url = db_url
        config_store._engine = None
        config_store._Session = None

        credentials["postgresql"] = {
            "host": "kag-db",
            "port": 5432,
            "name": "kag",
            "user": "kag",
            "password": pg_password,
        }
        logger.info("SETUP: PostgreSQL OK")
    except Exception as e:
        logger.error(f"SETUP: PostgreSQL failed: {e}")
        credentials["postgresql"] = {"error": str(e)[:100]}

    # 2. Qdrant
    try:
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as cl:
            r = await cl.get("http://qdrant:6333/collections/kag_documents")
            if r.status_code == 200:
                # Коллекция существует — читаем реальную размерность
                info = r.json()
                real_dims = info.get("result", {}).get("config", {}).get("params", {}).get("vectors", {}).get("size", {})
                logger.info(f"SETUP: Qdrant collection exists (dims={real_dims})")
            else:
                # Создаём новую коллекцию
                dims = int(os.environ.get("EMBEDDING_DIMENSIONS", "1024"))
                await cl.put(
                    "http://qdrant:6333/collections/kag_documents",
                    json={
                        "vectors": {"size": dims, "distance": "Cosine"},
                        "optimizers_config": {"default_segment_number": 2},
                    },
                )
                real_dims = dims
                logger.info(f"SETUP: Qdrant collection created (dims={dims})")

        credentials["qdrant"] = {
            "url": "http://qdrant:6333",
            "collection": "kag_documents",
            "dimensions": real_dims,
        }
    except Exception as e:
        logger.error(f"SETUP: Qdrant failed: {e}")
        credentials["qdrant"] = {"error": str(e)[:100]}

    # 3. Neo4j
    try:
        from neo4j import GraphDatabase

        # Генерируем новый пароль для Neo4j
        ne_new_pass = _gen_password(20)

        # Пробуем подключиться: env пароль → default → ошибка
        ne_env_pass = os.environ.get("NEO4J_PASSWORD", "kagneo4j2026")
        ne_connected = False

        for attempt_pass, label in [(ne_env_pass, "env"), ("kagneo4j2026", "default"), ("neo4j", "neo4j/neo4j")]:
            try:
                drv = GraphDatabase.driver("bolt://neo4j:7687", auth=("neo4j", attempt_pass))
                with drv.session() as s:
                    s.run("RETURN 1")
                # Меняем пароль на наш сгенерированный
                with drv.session() as s:
                    s.run(f"ALTER CURRENT USER SET PASSWORD FROM '{attempt_pass}' TO '{ne_new_pass}'")
                drv.close()
                ne_connected = True
                logger.info(f"SETUP: Neo4j connected with {label} password — changed to new")
                break
            except Exception:
                continue

        if not ne_connected:
            # Не удалось подключиться — используем env пароль как есть
            ne_new_pass = ne_env_pass
            logger.warning("SETUP: Neo4j — unable to change password, using env password")

        # Создаём индексы (с новым или старым паролем)
        drv = GraphDatabase.driver("bolt://neo4j:7687", auth=("neo4j", ne_new_pass))
        with drv.session() as s:
            s.run("CREATE INDEX entity_name IF NOT EXISTS FOR (e:Entity) ON (e.name)")
            s.run(
                "CREATE CONSTRAINT entity_id_unique IF NOT EXISTS "
                "FOR (e:Entity) REQUIRE e.id IS UNIQUE"
            )
        drv.close()

        credentials["neo4j"] = {
            "host": "neo4j",
            "http": "http://neo4j:7474",
            "bolt": "bolt://neo4j:7687",
            "user": "neo4j",
            "password": ne_new_pass,
        }
        logger.info("SETUP: Neo4j OK")
    except Exception as e:
        logger.error(f"SETUP: Neo4j failed: {e}")
        credentials["neo4j"] = {"error": str(e)[:100]}

    # 4. Admin-пользователь
    try:
        from sqlalchemy import create_engine, text
        from src.database.models import Base

        db_url = os.environ.get(
            "KAG_DB_URL",
            f"postgresql://kag:{pg_password}@kag-db:5432/kag",
        )

        engine = None
        for attempt in range(5):
            try:
                engine = create_engine(db_url, pool_pre_ping=True)
                with engine.connect() as c:
                    c.execute(text("SELECT 1"))
                break
            except Exception:
                import time
                time.sleep(2)

        if engine is None:
            raise RuntimeError("Не удалось подключиться к PostgreSQL после 5 попыток")

        # Создаём таблицы (если нет)
        Base.metadata.create_all(bind=engine)

        # Создаём admin
        hashed = hash_methods.pbkdf2_sha256.hash(ad_password)
        from sqlalchemy import text as sa_text

        with engine.begin() as conn:
            # Проверяем, есть ли уже admin
            row = conn.execute(
                sa_text("SELECT id FROM users WHERE username = 'admin'")
            ).fetchone()
            if not row:
                import uuid
                conn.execute(
                    sa_text(
                        "INSERT INTO users (id, username, hashed_password, is_admin, is_active, created_at) "
                        "VALUES (:id, 'admin', :pwd, TRUE, TRUE, NOW())"
                    ),
                    {"id": str(uuid.uuid4()), "pwd": hashed},
                )
                logger.info("SETUP: Admin user created")
            else:
                # Обновляем пароль существующего admin
                conn.execute(
                    sa_text("UPDATE users SET hashed_password = :pwd WHERE username = 'admin'"),
                    {"pwd": hashed},
                )
                logger.info("SETUP: Admin password updated")

        credentials["admin"] = {
            "login": "admin",
            "password": ad_password,
        }

        # Сохраняем пароль admin в config_store (НЕ БД-пароли)
        config_store.set("admin", "credentials", {
            "login": "admin",
            "password": ad_password,
            "created_at": datetime.utcnow().isoformat(),
        })

        logger.info("SETUP: Admin OK")
    except Exception as e:
        logger.error(f"SETUP: Admin failed: {e}")
        credentials["admin"] = {"login": "admin", "password": ad_password, "error": str(e)[:100]}

    # 5. Помечаем setup как завершённый
    config_store.set("setup", "status", {
        "configured": True,
        "timestamp": datetime.utcnow().isoformat(),
    })

    return {
        "success": True,
        "message": "Система готова к работе",
        "credentials": credentials,
    }


# ── GET /check ─────────────────────────────────────────────────────────────

@router.get("/check")
async def check_status():
    """Проверка: настроена ли система."""
    try:
        status = config_store.get("setup", "status", {})
        return {
            "configured": status.get("configured", False),
            "timestamp": status.get("timestamp"),
        }
    except Exception as e:
        return {"configured": False, "timestamp": None}


# ── POST /complete ─────────────────────────────────────────────────────────

@router.post("/complete")
async def complete_setup():
    """
    Завершить setup.

    Вызывается из JS после нажатия «Перейти в админку».
    Пароли БД уже не в ответе — браузер их очищает.
    """
    status = config_store.get("setup", "status", {})
    status["configured"] = True
    status["timestamp"] = datetime.utcnow().isoformat()
    config_store.set("setup", "status", status)
    return {"success": True, "message": "Setup завершён"}
