"""
Setup Wizard API для KAG

Инициализация системы одной кнопкой:
- Создаёт БД (PostgreSQL, Qdrant, Neo4j) — пароли из .env, не генерирует
- Создаёт admin-пользователя со сгенерированным паролем
- Пароли БД НЕ сохраняются — только в ответе и в скачанном файле
- Пароль admin сохраняется в config_store (нужен для входа)
"""

from fastapi import APIRouter, HTTPException
from loguru import logger
from datetime import datetime
import os
import secrets
import string
import passlib.hash as hash_methods
from urllib.parse import urlparse

from src.api.services.config_store import config_store

router = APIRouter(prefix="/setup", tags=["setup"])


# ── Генератор ──────────────────────────────────────────────────────────────

def _gen_password(length: int = 12) -> str:
    """Сгенерировать пароль без спецсимволов."""
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def _parse_db_url(url: str) -> dict:
    """Извлечь user, password, host, port, dbname из postgresql:// URL."""
    parsed = urlparse(url)
    return {
        "user": parsed.username or "kag",
        "password": parsed.password or "",
        "host": parsed.hostname or "kag-db",
        "port": parsed.port or 5432,
        "dbname": parsed.path.lstrip("/") if parsed.path else "kag",
    }


# ── POST /init-all ─────────────────────────────────────────────────────────

@router.post("/init-all")
async def initialize_all():
    """
    Полная инициализация системы.

    1. PostgreSQL — создаёт БД kag + роль kag (если нет). Пароль из KAG_DB_URL.
    2. Qdrant — создаёт коллекцию kag_documents (если нет).
    3. Neo4j — создаёт индексы. Пароль из NEO4J_PASSWORD.
    4. Admin — создаёт пользователя admin со сгенерированным паролем.

    Пароли БД читаются из .env (не генерируются, не меняются).
    Пароль admin генерируется и сохраняется в config_store.
    """
    logger.info("=== SETUP: INIT-ALL ===")

    # Проверка: уже настроено?
    existing = config_store.get("setup", "status", {})
    if existing.get("configured"):
        return {"success": True, "message": "Система уже настроена", "already_configured": True}

    credentials = {}
    ad_password = _gen_password(12)

    # ── 1. PostgreSQL ───────────────────────────────────────────────────────

    try:
        import psycopg2

        # Пароль PG — из KAG_DB_URL (задан в .env при деплое)
        db_url_env = os.environ.get("KAG_DB_URL", "")
        if not db_url_env:
            raise RuntimeError("KAG_DB_URL не задан. Укажите его в .env")

        pg_info = _parse_db_url(db_url_env)
        pg_password = pg_info["password"]
        if not pg_password:
            raise RuntimeError("Пароль не найден в KAG_DB_URL")

        # Подключаемся как superuser (keycloak) для создания роли/БД
        conn = psycopg2.connect(
            host=pg_info["host"],
            port=pg_info["port"],
            dbname="keycloak",
            user=os.environ.get("KC_DB_USERNAME", "keycloak"),
            password=os.environ.get("KC_DB_PASSWORD", "keycloak_password"),
            connect_timeout=10,
        )
        conn.autocommit = True
        cur = conn.cursor()

        # Роль kag — только создать если нет, пароль НЕ меняем
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname='kag'")
        if not cur.fetchone():
            cur.execute(f"CREATE USER kag WITH PASSWORD '{pg_password}'")
            logger.info("SETUP: PG user kag created")
        else:
            logger.info("SETUP: PG user kag already exists — skipped")

        # БД kag — создать если нет
        cur.execute("SELECT 1 FROM pg_database WHERE datname='kag'")
        if not cur.fetchone():
            cur.execute("CREATE DATABASE kag OWNER kag")
            cur.execute("GRANT ALL PRIVILEGES ON DATABASE kag TO kag")
            logger.info("SETUP: PG database kag created")
        else:
            logger.info("SETUP: PG database kag already exists — skipped")

        cur.close()
        conn.close()

        # Обновляем KAG_DB_URL в config_store
        full_db_url = f"postgresql://kag:{pg_password}@{pg_info['host']}:{pg_info['port']}/kag"
        os.environ["KAG_DB_URL"] = full_db_url
        config_store._db_url = full_db_url
        config_store._engine = None
        config_store._Session = None

        credentials["postgresql"] = {
            "host": pg_info["host"],
            "port": pg_info["port"],
            "name": "kag",
            "user": "kag",
            "password": pg_password,
        }
        logger.info("SETUP: PostgreSQL OK")
    except Exception as e:
        logger.error(f"SETUP: PostgreSQL failed: {e}")
        credentials["postgresql"] = {"error": str(e)[:200]}

    # ── 2. Qdrant ──────────────────────────────────────────────────────────

    try:
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as cl:
            r = await cl.get("http://qdrant:6333/collections/kag_documents")
            if r.status_code == 200:
                info = r.json()
                real_dims = info.get("result", {}).get("config", {}).get("params", {}).get("vectors", {}).get("size", {})
                logger.info(f"SETUP: Qdrant collection exists (dims={real_dims})")
            else:
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

    # ── 3. Neo4j ───────────────────────────────────────────────────────────

    try:
        from neo4j import GraphDatabase

        ne_password = os.environ.get("NEO4J_PASSWORD")
        if not ne_password:
            raise RuntimeError(
                "NEO4J_PASSWORD не задан. Укажите его в docker-compose.yml "
                "или .env: NEO4J_PASSWORD=..."
            )

        # Подключаемся с паролем из env, НЕ меняем его
        drv = GraphDatabase.driver("bolt://neo4j:7687", auth=("neo4j", ne_password))
        with drv.session() as s:
            s.run("RETURN 1")
        logger.info("SETUP: Neo4j connected with env password")

        # Создаём индексы
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
            "password": ne_password,
        }
        logger.info("SETUP: Neo4j OK")
    except Exception as e:
        logger.error(f"SETUP: Neo4j failed: {e}")
        credentials["neo4j"] = {"error": str(e)[:100]}

    # ── 4. Admin-пользователь ──────────────────────────────────────────────

    try:
        from sqlalchemy import create_engine, text as sa_text
        from src.database.models import Base

        db_url_env = os.environ.get("KAG_DB_URL", "")
        if not db_url_env:
            raise RuntimeError("KAG_DB_URL не задан")

        # Извлекаем пароль для KAG_DB_URL (с паролем из env)
        pg_info = _parse_db_url(db_url_env)
        admin_db_url = f"postgresql://kag:{pg_info['password']}@{pg_info['host']}:{pg_info['port']}/kag"

        engine = None
        for attempt in range(5):
            try:
                engine = create_engine(admin_db_url, pool_pre_ping=True)
                with engine.connect() as c:
                    c.execute(sa_text("SELECT 1"))
                break
            except Exception:
                import time
                time.sleep(2)

        if engine is None:
            raise RuntimeError("Не удалось подключиться к PostgreSQL после 5 попыток")

        Base.metadata.create_all(bind=engine)
        hashed = hash_methods.pbkdf2_sha256.hash(ad_password)

        with engine.begin() as conn:
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
                conn.execute(
                    sa_text("UPDATE users SET hashed_password = :pwd WHERE username = 'admin'"),
                    {"pwd": hashed},
                )
                logger.info("SETUP: Admin password updated")

        credentials["admin"] = {
            "login": "admin",
            "password": ad_password,
        }

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
    """
    status = config_store.get("setup", "status", {})
    status["configured"] = True
    status["timestamp"] = datetime.utcnow().isoformat()
    config_store.set("setup", "status", status)
    return {"success": True, "message": "Setup завершён"}
