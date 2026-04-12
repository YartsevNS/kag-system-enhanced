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
# Тестовые эндпоинты
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
                # Проверяем наличие модели
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
                # vLLM
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
        # Извлекаем данные из запроса
        username = cfg.username
        password = cfg.password
        llm_host = cfg.llm_host
        llm_port = cfg.llm_port
        sudo_pass = cfg.sudo_password if hasattr(cfg, 'sudo_password') else None
        
        # Формируем команду
        cmd = f"sshpass -p '{password}' ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 {username}@{llm_host} 'echo OK'"
        
        result = await asyncio.to_thread(
            subprocess.run,
            cmd, shell=True, capture_output=True, text=True, timeout=15
        )
        
        if result.returncode != 0:
            return {"success": False, "message": f"SSH ошибка: {result.stderr.strip()[:200]}"}
        
        # Проверяем Ollama
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
        # Добавляем хост из LLM настроек
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
