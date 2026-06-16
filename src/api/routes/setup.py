"""
Setup Wizard API для KAG

Инициализация системы одной кнопкой:
- Создаёт БД (PostgreSQL, Qdrant, Neo4j) со сгенерированными паролями
- Создаёт admin-пользователя со сгенерированным паролем
- Все пароли записываются в .env на сервере (persist после перезапуска)
- Пароль admin сохраняется в config_store (нужен для входа)
"""

from fastapi import APIRouter, HTTPException
from loguru import logger
from datetime import datetime
import os
import secrets
import string
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher
from pwdlib.hashers.bcrypt import BcryptHasher

password_hash = PasswordHash([Argon2Hasher(), BcryptHasher()])

from src.api.services.config_store import config_store

router = APIRouter(prefix="/setup", tags=["setup"])


# ── Генератор ──────────────────────────────────────────────────────────────

def _gen_password(length: int = 12) -> str:
    """Сгенерировать пароль без спецсимволов."""
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))


# ── POST /init-all ─────────────────────────────────────────────────────────

@router.post("/init-all")
async def initialize_all():
    """
    Полная инициализация системы.

    1. PostgreSQL — создаёт БД kag + роль kag. Пароль генерируется.
    2. Qdrant — создаёт коллекцию kag_documents (если нет).
    3. Neo4j — создаёт индексы. Пароль генерируется.
    4. Admin — создаёт пользователя admin. Пароль генерируется.

    Все пароли записываются в .env на сервере.
    Пароль admin дополнительно сохраняется в config_store.
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

        # Генерируем пароль для PG
        pg_password = _gen_password(20)

        # Подключаемся как superuser (keycloak) для создания роли/БД
        conn = psycopg2.connect(
            host="kag-db",
            port=5432,
            dbname="keycloak",
            user=os.environ.get("KC_DB_USERNAME", "keycloak"),
            password=os.environ.get("KC_DB_PASSWORD", "keycloak_password"),
            connect_timeout=10,
        )
        conn.autocommit = True
        cur = conn.cursor()

        # Роль kag — создаём или обновляем пароль
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname='kag'")
        if cur.fetchone():
            cur.execute(f"ALTER USER kag WITH PASSWORD '{pg_password}'")
            logger.info("SETUP: PG user kag exists — password updated")
        else:
            cur.execute(f"CREATE USER kag WITH PASSWORD '{pg_password}'")
            logger.info("SETUP: PG user kag created")

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
        full_db_url = f"postgresql://kag:{pg_password}@kag-db:5432/kag"
        os.environ["KAG_DB_URL"] = full_db_url
        config_store._db_url = full_db_url
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

        # Генерируем новый пароль для Neo4j
        ne_new_pass = _gen_password(20)

        # Пароль из env (может быть задан в .env)
        ne_env_pass = os.environ.get("NEO4J_PASSWORD") or ""

        # Пробуем: env → дефолтный из docker-compose → neo4j/neo4j
        ne_found = False
        for attempt_pass in [ne_env_pass, "kagneo4j2026", "neo4j"]:
            if not attempt_pass:
                continue
            try:
                drv = GraphDatabase.driver("bolt://neo4j:7687", auth=("neo4j", attempt_pass))
                with drv.session() as s:
                    s.run("RETURN 1")
                # Меняем пароль на сгенерированный
                with drv.session() as s:
                    s.run(f"ALTER CURRENT USER SET PASSWORD FROM '{attempt_pass}' TO '{ne_new_pass}'")
                drv.close()
                ne_found = True
                logger.info(f"SETUP: Neo4j password changed to generated")
                break
            except Exception:
                continue

        if not ne_found:
            raise RuntimeError(
                "Не удалось подключиться к Neo4j. Укажите NEO4J_PASSWORD в .env "
                "(пароль из docker-compose: NEO4J_AUTH=neo4j/<пароль>)"
            )

        # Создаём индексы (с новым паролем)
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

    # ── 4. Admin-пользователь ──────────────────────────────────────────────

    try:
        from sqlalchemy import create_engine, text as sa_text
        from src.database.models import Base

        db_url_env = os.environ.get("KAG_DB_URL", "")
        if not db_url_env:
            raise RuntimeError("KAG_DB_URL не задан")

        # KAG_DB_URL уже содержит актуальный пароль (обновлён в секции PG)
        admin_db_url = db_url_env

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
        hashed = password_hash.hash(ad_password)

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

    # ── 5. Обновляем .env на сервере ────────────────────────────────────────
    try:
        env_path = "/app/kag.env"
        if os.path.exists(env_path):
            with open(env_path, "r") as f:
                env_lines = f.readlines()

            new_lines = []
            # Собираем новые значения
            pg_pwd = credentials.get("postgresql", {}).get("password", "")
            ne_pwd = credentials.get("neo4j", {}).get("password", "")
            # Удаляем пароль из env-файла KAG_DB_URL при формировании
            db_url_clean = f"postgresql://kag:{pg_pwd}@kag-db:5432/kag" if pg_pwd else ""

            for line in env_lines:
                stripped = line.strip()
                if stripped.startswith("POSTGRES_PASSWORD=") and pg_pwd:
                    new_lines.append(f"POSTGRES_PASSWORD={pg_pwd}\n")
                elif stripped.startswith("DB_PASSWORD=") and pg_pwd:
                    new_lines.append(f"DB_PASSWORD={pg_pwd}\n")
                elif stripped.startswith("NEO4J_PASSWORD=") and ne_pwd:
                    new_lines.append(f"NEO4J_PASSWORD={ne_pwd}\n")
                elif stripped.startswith("KAG_DB_URL=") and db_url_clean:
                    new_lines.append(f"KAG_DB_URL={db_url_clean}\n")
                elif stripped.startswith("KEYCLOAK_ADMIN_PASSWORD="):
                    new_lines.append(f"KEYCLOAK_ADMIN_PASSWORD={ad_password}\n")
                else:
                    new_lines.append(line)

            with open(env_path, "w") as f:
                f.writelines(new_lines)
            logger.info("SETUP: .env updated with new passwords")
        else:
            logger.warning("SETUP: .env not found at /app/.env")
    except Exception as e:
        logger.error(f"SETUP: .env update failed: {e}")

    # 6. Помечаем setup как завершённый
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
