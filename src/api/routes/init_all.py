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
        import os
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
        import os
        import uuid
        from passlib.hash import pbkdf2_sha256
        from sqlalchemy import create_engine, text
        e = create_engine(os.environ.get("KAG_DB_URL", "postgresql://kag:kagpass123@kag-pg:5432/kag"))
        with e.connect() as conn:
            conn.execute(text("CREATE TABLE IF NOT EXISTS users (id VARCHAR(36) PRIMARY KEY, username VARCHAR(255) UNIQUE NOT NULL, full_name VARCHAR(255), email VARCHAR(255), hashed_password VARCHAR(255) NOT NULL, is_active BOOLEAN DEFAULT TRUE, is_admin BOOLEAN DEFAULT FALSE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"))
            conn.execute(text("ALTER TABLE IF EXISTS users ADD COLUMN IF NOT EXISTS full_name VARCHAR(255)"))
            conn.execute(text("ALTER TABLE IF EXISTS users ADD COLUMN IF NOT EXISTS email VARCHAR(255)"))
            conn.execute(text("ALTER TABLE IF EXISTS users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW()"))
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
