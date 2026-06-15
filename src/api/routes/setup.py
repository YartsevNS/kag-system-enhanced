"""
Setup Wizard API для KAG
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
    username: str
    password: str
    llm_host: str
    llm_port: int = 11434


class FullSetupPayload(BaseModel):
    database: Optional[DatabaseSetup] = None
    llm: LlmSetup
    embedding: EmbeddingSetup
    ssh: SshSetup


# ==========================================
# Генератор паролей
# ==========================================

def _generate_password(length: int = 24) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return ''.join(secrets.choice(alphabet) for _ in range(length))


# ==========================================
# Тестовые эндпоинты
# ==========================================

@router.post("/test-db")
async def test_db(cfg: DatabaseSetup):
    try:
        import psycopg2
        conn = psycopg2.connect(host=cfg.host, port=cfg.port, dbname=cfg.name, user=cfg.user, password=cfg.password, connect_timeout=10)
        conn.close()
        return {"success": True, "message": f"Подключено к {cfg.host}:{cfg.port}/{cfg.name}"}
    except ImportError:
        return {"success": False, "message": "psycopg2 не установлен"}
    except Exception as e:
        return {"success": False, "message": str(e)}


@router.post("/test-llm")
async def test_llm(cfg: LlmSetup):
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
                return {"success": False, "message": f"vLLM ошибка: {resp.status_code}"}
    except httpx.ConnectError:
        return {"success": False, "message": f"Не удалось подключиться к {cfg.host}:{cfg.port}"}
    except Exception as e:
        return {"success": False, "message": str(e)}


@router.post("/test-ssh")
async def test_ssh(cfg):
    try:
        username = cfg.username
        password = cfg.password
        llm_host = cfg.llm_host
        llm_port = cfg.llm_port
        cmd = f"sshpass -p '{password}' ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 {username}@{llm_host} 'echo OK'"
        result = await asyncio.to_thread(subprocess.run, cmd, shell=True, capture_output=True, text=True, timeout=15)
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
# Создание БД (старые эндпоинты)
# ==========================================

@router.post("/create-pg-db")
async def create_pg_database():
    import os
    try:
        import psycopg2
        existing = config_store.get("database", "default")
        if existing and existing.get("auto_created"):
            return {"success": False, "message": "База данных уже создана"}
        db_name = f"kag_{secrets.token_hex(4)}"
        db_user = f"kag_user_{secrets.token_hex(4)}"
        db_password = _generate_password(24)
        conn = psycopg2.connect(host="kag-db", port=5432, dbname="keycloak",
            user=os.environ.get("KC_DB_USERNAME","keycloak"),
            password=os.environ.get("KC_DB_PASSWORD","keycloak_password"), connect_timeout=10)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(f"CREATE USER {db_user} WITH PASSWORD '{db_password}'")
        cur.execute(f"CREATE DATABASE {db_name} OWNER {db_user}")
        cur.execute(f"GRANT ALL PRIVILEGES ON DATABASE {db_name} TO {db_user}")
        cur.close(); conn.close()
        now = datetime.utcnow().isoformat()
        config_store.set("database","default",{"host":"kag-db","port":5432,"name":db_name,"user":db_user,"password":db_password,"auto_created":True,"created_at":now})
        return {"success":True,"message":"База создана","credentials":{"host":"kag-db","name":db_name,"user":db_user,"password":db_password}}
    except Exception as e:
        if "already exists" in str(e):
            return {"success":True,"message":"Уже существует"}
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/create-qdrant-db")
async def create_qdrant():
    import os
    dims = int(os.environ.get("EMBEDDING_DIMENSIONS","1024"))
    try:
        async with httpx.AsyncClient(timeout=10.0) as cl:
            r = await cl.get("http://qdrant:6333/collections/kag_documents")
            if r.status_code != 200:
                await cl.put("http://qdrant:6333/collections/kag_documents", json={"vectors":{"size":dims,"distance":"Cosine"}})
        config_store.set("qdrant","default",{"host":"qdrant","port":6333,"collection":"kag_documents","vector_size":dims,"auto_created":True})
        return {"success":True,"message":"Коллекция создана"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/create-neo4j-db")
async def create_neo4j_database():
    try:
        import os
        from neo4j import GraphDatabase
        password = os.environ.get("NEO4J_PASSWORD","kagneo4j2026")
        driver = GraphDatabase.driver("bolt://neo4j:7687", auth=("neo4j",password))
        with driver.session() as s:
            s.run("CREATE CONSTRAINT IF NOT EXISTS FOR (d:Document) REQUIRE d.id IS UNIQUE")
            s.run("CREATE CONSTRAINT IF NOT EXISTS FOR (e:Entity) REQUIRE e.id IS UNIQUE")
        driver.close()
        return {"success":True,"message":"Neo4j готов"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/save")
async def save_setup(payload: FullSetupPayload):
    logger.info("=== НАЧАЛО СОХРАНЕНИЯ ===")
    try:
        crypto = GOSTCrypto()
        now = datetime.utcnow().isoformat()
        db_data = payload.database.model_dump()
        db_data['password'] = crypto.encrypt_to_base64(db_data['password'])
        db_data['saved_at'] = now
        config_store.set("database","default",db_data)
        llm_data = payload.llm.model_dump()
        llm_data['saved_at'] = now
        config_store.set("llm","default",llm_data)
        emb_data = payload.embedding.model_dump()
        emb_data['saved_at'] = now
        config_store.set("embedding","default",emb_data)
        ssh_data = payload.ssh.model_dump()
        ssh_data['password'] = crypto.encrypt_to_base64(ssh_data['password'])
        if ssh_data.get('sudo_password'):
            ssh_data['sudo_password'] = crypto.encrypt_to_base64(ssh_data['sudo_password'])
        ssh_data['host'] = payload.llm.host
        ssh_data['ollama_port'] = payload.llm.port
        ssh_data['saved_at'] = now
        config_store.set("ssh","default",ssh_data)
        config_store.set("setup","status",{"configured":True,"timestamp":now,"llm_model":payload.llm.model,"db_host":payload.database.host})
        return {"success":True,"message":"Настройки сохранены"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==========================================
# Initialize ALL — одной кнопкой
# ==========================================

@router.post("/init-all")
async def initialize_all():
    import os, uuid, time, hashlib, psycopg2
    from datetime import datetime
    from sqlalchemy import create_engine, text

    logger.info("=== INIT-ALL ===")

    try:
        existing = config_store.get("setup","status",{})
        if existing.get("configured"):
            return {"success":True,"message":"Уже настроено","already_configured":True}

        credentials = {}
        pg_pass = "kagpass123"
        ne_pass = "kagneo4j2026"
        ad_pass = "admin123456"

        # 1. PostgreSQL
        try:
            conn = psycopg2.connect(host="kag-db",port=5432,dbname="keycloak",
                user=os.environ.get("KC_DB_USERNAME","keycloak"),
                password=os.environ.get("KC_DB_PASSWORD","keycloak_password"),connect_timeout=10)
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute("DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='kag') THEN CREATE USER kag WITH PASSWORD '"+pg_pass+"'; END IF; END $$")
            cur.execute("SELECT 1 FROM pg_database WHERE datname='kag'")
            if not cur.fetchone():
                cur.execute("CREATE DATABASE kag OWNER kag")
                cur.execute("GRANT ALL PRIVILEGES ON DATABASE kag TO kag")
            cur.close(); conn.close()
            config_store.set("database","default",{"host":"kag-db","port":5432,"name":"kag","user":"kag","password":pg_pass,"auto_created":True,"saved_at":datetime.utcnow().isoformat()})
            credentials["postgresql"] = {"host":"kag-db","name":"kag","user":"kag","password":pg_pass}
        except Exception as e:
            credentials["postgresql"] = {"host":"kag-db","name":"kag","user":"kag","password":"(see .env)"}

        # 2. Qdrant
        dims = int(os.environ.get("EMBEDDING_DIMENSIONS","1024"))
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as cl:
                r = await cl.get("http://qdrant:6333/collections/kag_documents")
                if r.status_code != 200:
                    await cl.put("http://qdrant:6333/collections/kag_documents",json={"vectors":{"size":dims,"distance":"Cosine"}})
            credentials["qdrant"] = {"collection":"kag_documents","dimensions":dims}
        except Exception as e:
            credentials["qdrant"] = {"collection":"kag_documents","note":str(e)[:100]}

        # 3. Neo4j
        try:
            from neo4j import GraphDatabase
            ne_pass = os.environ.get("NEO4J_PASSWORD","kagneo4j2026")
            drv = GraphDatabase.driver("bolt://neo4j:7687",auth=("neo4j",ne_pass))
            with drv.session() as s:
                s.run("CREATE INDEX entity_name IF NOT EXISTS FOR (e:Entity) ON (e.name)")
                s.run("CREATE CONSTRAINT entity_id_unique IF NOT EXISTS FOR (e:Entity) REQUIRE e.id IS UNIQUE")
            drv.close()
            credentials["neo4j"] = {"host":"neo4j","http":"http://neo4j:7474","bolt":"bolt://neo4j:7687","user":"neo4j","password":ne_pass}
        except Exception as e:
            credentials["neo4j"] = {"host":"neo4j","note":str(e)[:100]}

        # 4. Admin
        try:
            db_url = os.environ.get("KAG_DB_URL","postgresql://kag:kagpass123@kag-db:5432/kag")
            engine = None
            for a in range(5):
                try:
                    engine = create_engine(db_url,pool_pre_ping=True)
                    with engine.connect() as c:
                        c.execute(text("SELECT 1"))
                    break
                except: time.sleep(3)
            if engine:
                with engine.connect() as c:
                    c.execute(text("CREATE TABLE IF NOT EXISTS users (id SERIAL PRIMARY KEY, username VARCHAR(255) UNIQUE NOT NULL, password_hash VARCHAR(255) NOT NULL, role VARCHAR(50) DEFAULT 'user', is_active BOOLEAN DEFAULT true, created_at TIMESTAMP DEFAULT NOW())"))
                    for col,dt in [("full_name","VARCHAR(255)"),("email","VARCHAR(255)"),("updated_at","TIMESTAMP"),("password_hash","VARCHAR(255)")]:
                        try: c.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS "+col+" "+dt))
                        except: pass
                    c.commit()
                    r = c.execute(text("SELECT id FROM users WHERE username = 'admin'"))
                    if not r.fetchone():
                        h = hashlib.sha256(ad_pass.encode()).hexdigest()
                        c.execute(text("INSERT INTO users (username,password_hash,role,full_name,is_active) VALUES ('admin',:p,'admin','Administrator',true)"),{"p":h})
                        c.commit()
            credentials["admin"] = {"login":"admin","password":ad_pass}
        except Exception as e:
            credentials["admin"] = {"login":"admin","password":ad_pass,"note":str(e)[:100]}

        config_store.set("setup","status",{"configured":True,"timestamp":datetime.utcnow().isoformat()})
        return {"success":True,"message":"Система готова к работе","credentials":credentials}
    except Exception as e:
        logger.error(f"INIT-ALL: {e}")
        raise HTTPException(status_code=500,detail=str(e))


@router.post("/save-llm")
async def save_llm(payload: dict):
    try:
        d = payload.get("llm",{})
        d["saved_at"] = datetime.now().isoformat()
        config_store.set("llm","default",d)
        return {"success":True,"message":"LLM saved"}
    except Exception as e:
        raise HTTPException(status_code=500,detail=str(e))


# ==========================================
# Проверка статуса
# ==========================================

@router.get("/check")
async def check_status():
    try:
        status = config_store.get("setup","status",{})
        return {"configured":status.get("configured",False),"timestamp":status.get("timestamp")}
    except Exception as e:
        return {"configured":False,"timestamp":None}


@router.post("/mark-creds-shown")
async def mark_creds_shown():
    for key in ["database","qdrant"]:
        cfg = config_store.get(key,"default")
        if cfg and cfg.get("auto_created"):
            cfg["creds_shown"] = True
            config_store.set(key,"default",cfg)
    return {"success":True,"message":"Креды помечены как показанные"}


@router.post("/complete")
async def complete_setup():
    status = config_store.get("setup","status",{})
    status["configured"] = True
    status["timestamp"] = datetime.now().isoformat()
    config_store.set("setup","status",status)
    return {"success":True,"message":"Setup завершён"}
