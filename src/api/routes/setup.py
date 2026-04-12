"""
Setup Wizard API для KAG

Обрабатывает первоначальную настройку системы:
- Тест подключения к БД
- Тест подключения к LLM
- Тест SSH
- Сохранение всех настроек в PostgreSQL
"""

from typing import Dict, Any, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from loguru import logger
import httpx
import asyncio
import subprocess

from src.api.services.config_store import config_store
from src.security.gost_crypto import GOSTCrypto

router = APIRouter(prefix="/setup", tags=["setup"])


# ===========================================
# Модели запросов
# ===========================================

class DatabaseConfig(BaseModel):
    host: str = Field(..., description="Хост базы данных")
    port: int = Field(default=5432, description="Порт")
    name: str = Field(..., description="Имя базы данных")
    user: str = Field(..., description="Пользователь")
    password: str = Field(..., description="Пароль")


class LlmConfig(BaseModel):
    type: str = Field(default="ollama", description="Тип бэкенда (ollama/vllm)")
    host: str = Field(..., description="Адрес сервера")
    port: int = Field(default=11434, description="Порт")
    model: str = Field(..., description="Название модели")


class EmbeddingConfig(BaseModel):
    model: str = Field(..., description="Название embedding модели")
    dimensions: int = Field(default=4096, description="Размерность вектора")


class SshConfig(BaseModel):
    username: str = Field(..., description="SSH пользователь")
    password: str = Field(..., description="SSH пароль")
    sudo_password: Optional[str] = Field(default=None, description="Пароль sudo")
    llm_host: str = Field(..., description="Хост LLM для теста")
    llm_port: int = Field(default=11434, description="Порт LLM для теста")


class FullSetupRequest(BaseModel):
    database: DatabaseConfig
    llm: LlmConfig
    embedding: EmbeddingConfig
    ssh: SshConfig


# ===========================================
# Тестовые endpoints
# ===========================================

@router.post("/test-db", summary="Тест подключения к базе данных")
async def test_database(config: DatabaseConfig):
    """
    Проверяет подключение к PostgreSQL базе данных.
    """
    try:
        # Используем psycopg2 для теста
        import psycopg2
        
        conn = psycopg2.connect(
            host=config.host,
            port=config.port,
            dbname=config.name,
            user=config.user,
            password=config.password,
            connect_timeout=10
        )
        
        conn.close()
        
        return {
            "success": True,
            "message": f"Подключено к {config.host}:{config.port}/{config.name}"
        }
    except ImportError:
        return {
            "success": False,
            "message": "psycopg2 не установлен. Установите: pip install psycopg2-binary"
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"Ошибка подключения: {str(e)}"
        }


@router.post("/test-llm", summary="Тест подключения к LLM бэкенду")
async def test_llm(config: LlmConfig):
    """
    Проверяет доступность LLM сервера и наличие модели.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Для Ollama проверяем через /api/tags
            if config.type == "ollama":
                response = await client.get(f"http://{config.host}:{config.port}/api/tags")
                if response.status_code == 200:
                    models = response.json().get("models", [])
                    model_names = [m["name"] for m in models]
                    
                    if config.model in model_names:
                        return {
                            "success": True,
                            "message": f"Модель {config.model} доступна"
                        }
                    else:
                        return {
                            "success": False,
                            "message": f"Модель {config.model} не найдена. Доступны: {', '.join(model_names[:5])}"
                        }
                else:
                    return {
                        "success": False,
                        "message": f"Ollama вернул ошибку: {response.status_code}"
                    }
            else:
                # Для vLLM проверяем health endpoint
                response = await client.get(f"http://{config.host}:{config.port}/health")
                if response.status_code == 200:
                    return {
                        "success": True,
                        "message": f"vLLM сервер доступен"
                    }
                else:
                    return {
                        "success": False,
                        "message": f"vLLM вернул ошибку: {response.status_code}"
                    }
    except httpx.ConnectError:
        return {
            "success": False,
            "message": f"Не удалось подключиться к {config.host}:{config.port}"
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"Ошибка: {str(e)}"
        }


@router.post("/test-ssh", summary="Тест SSH подключения")
async def test_ssh(config: SshConfig):
    """
    Проверяет SSH подключение к серверу LLM.
    """
    try:
        # Формируем команду с sshpass
        ssh_cmd = f"sshpass -p '{config.password}' ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 {config.username}@{config.llm_host}"
        
        # Тестируем подключение
        test_cmd = f"{ssh_cmd} 'echo OK'"
        
        result = await asyncio.to_thread(
            subprocess.run,
            test_cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=15
        )
        
        if result.returncode == 0:
            # Проверяем Ollama
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.get(f"http://{config.llm_host}:{config.llm_port}/")
                    ollama_ok = response.status_code == 200
            except:
                ollama_ok = False
            
            message = "SSH подключено"
            if ollama_ok:
                message += " • Ollama работает"
            
            return {
                "success": True,
                "message": message
            }
        else:
            return {
                "success": False,
                "message": f"SSH ошибка: {result.stderr.strip()[:200]}"
            }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "message": "Таймаут подключения (15 секунд)"
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"Ошибка: {str(e)}"
        }


# ===========================================
# Сохранение настроек
# ===========================================

@router.post("/save", summary="Сохранить все настройки")
async def save_setup(request: FullSetupRequest):
    """
    Сохраняет все настройки системы в PostgreSQL.
    """
    try:
        crypto = GOSTCrypto()
        
        # 1. Сохраняем настройки БД
        db_config = request.database.model_dump()
        db_config['password'] = crypto.encrypt_to_base64(db_config['password'])
        config_store.set("database", "default", db_config)
        
        # 2. Сохраняем настройки LLM
        llm_config = request.llm.model_dump()
        config_store.set("llm", "default", llm_config)
        
        # 3. Сохраняем настройки Embedding
        embed_config = request.embedding.model_dump()
        config_store.set("embedding", "default", embed_config)
        
        # 4. Сохраняем настройки SSH
        ssh_config = request.ssh.model_dump()
        ssh_config['password'] = crypto.encrypt_to_base64(ssh_config['password'])
        if ssh_config.get('sudo_password'):
            ssh_config['sudo_password'] = crypto.encrypt_to_base64(ssh_config['sudo_password'])
        ssh_config['host'] = request.llm.host
        ssh_config['port'] = 22
        ssh_config['ollama_port'] = request.llm.port
        ssh_config['ollama_service_name'] = 'ollama'
        ssh_config['updated_at'] = __import__('datetime').datetime.utcnow().isoformat()
        
        config_store.set("ssh", "default", ssh_config)
        
        # 5. Помечаем систему как настроенную
        config_store.set("setup", "status", {"configured": True, "timestamp": __import__('datetime').datetime.utcnow().isoformat()})
        
        logger.info("Система успешно настроена через Setup Wizard")
        
        return {
            "success": True,
            "message": "Настройки сохранены. Перезапустите приложение для применения."
        }
        
    except Exception as e:
        logger.error(f"Ошибка сохранения настроек: {e}")
        import traceback
        logger.error(traceback.format_exc())
        
        raise HTTPException(status_code=500, detail=f"Ошибка сохранения: {str(e)}")


# ===========================================
# Проверка статуса настройки
# ===========================================

@router.get("/check", summary="Проверить, настроена ли система")
async def check_setup_status():
    """
    Проверяет, была ли уже выполнена первоначальная настройка.
    """
    setup_status = config_store.get("setup", "status", {})
    
    return {
        "configured": setup_status.get("configured", False),
        "timestamp": setup_status.get("timestamp")
    }
