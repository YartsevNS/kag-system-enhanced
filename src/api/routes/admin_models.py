"""
Маршруты для управления моделями LLM и Embeddings

Админ-панель для:
- Просмотра доступных моделей
- Переключения между моделями
- Загрузки новых моделей
- Мониторинга статуса бэкендов
- Выбора embedding модели
"""

from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException

from loguru import logger
from pydantic import BaseModel, Field

from src.api.services.model_manager import model_manager
from src.api.services.ssh_manager import ssh_manager, SSHConnectionConfig
from src.api.services.docker_monitor import docker_monitor
from src.api.services.system_monitor import system_monitor
from src.llm import LLMBackendType

router = APIRouter(tags=["models"])


# ===========================================
# SSH подключения
# ===========================================

class SSHConfigRequest(BaseModel):
    """Запрос на сохранение SSH подключения"""
    host: str = Field(default="192.168.50.41", description="IP адрес или хост")
    port: int = Field(default=22, description="SSH порт", ge=1, le=65535)
    username: str = Field(default="nick", description="SSH пользователь")
    password: Optional[str] = Field(default=None, description="SSH пароль")
    use_key: bool = Field(default=False, description="Использовать SSH ключ")
    key_path: Optional[str] = Field(default=None, description="Путь к SSH ключу")
    sudo_password: Optional[str] = Field(default=None, description="Пароль для sudo")
    ollama_port: int = Field(default=11434, description="Порт Ollama API")
    ollama_service_name: str = Field(default="ollama", description="Имя сервиса Ollama")


@router.get("/ssh-config", summary="Получить настройки SSH подключения")
async def get_ssh_config(connection_id: str = "default"):
    """
    Получить настройки SSH подключения (без пароля).
    """
    try:
        config = ssh_manager.get_config(connection_id)
        return {
            "host": config.host,
            "port": config.port,
            "username": config.username,
            "use_key": config.use_key,
            "key_path": config.key_path,
            "ollama_port": config.ollama_port,
            "ollama_service_name": config.ollama_service_name,
            "has_password": bool(config.password),
            "has_sudo_password": bool(config.sudo_password)
        }
    except Exception as e:
        logger.error(f"Ошибка получения SSH конфигурации: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/ssh-config", summary="Сохранить настройки SSH подключения")
async def save_ssh_config(request: SSHConfigRequest, connection_id: str = "default"):
    """
    Сохранить настройки SSH подключения.

    Пароли шифруются перед сохранением.
    """
    logger.info(f"Запрос на сохранение SSH: host={request.host}, user={request.username}")
    logger.debug(f"SSH Config Request: {request.model_dump(exclude={'password', 'sudo_password'})}")
    
    try:
        config = SSHConnectionConfig(
            host=request.host,
            port=request.port,
            username=request.username,
            password=request.password,
            use_key=request.use_key,
            key_path=request.key_path,
            sudo_password=request.sudo_password,
            ollama_port=request.ollama_port,
            ollama_service_name=request.ollama_service_name
        )

        logger.info("Вызываю ssh_manager.save_config...")
        success = ssh_manager.save_config(config, connection_id)
        logger.info(f"ssh_manager.save_config вернул: {success}")

        if success:
            return {
                "status": "success",
                "message": "Настройки SSH сохранены"
            }
        else:
            logger.error("ssh_manager.save_config вернул False")
            raise HTTPException(status_code=500, detail="Ошибка сохранения")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Ошибка сохранения SSH конфигурации: {e}")
        import traceback
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/ssh-test", summary="Протестировать SSH подключение")
async def test_ssh_connection(connection_id: str = "default"):
    """
    Протестировать SSH подключение с текущими настройками.
    """
    try:
        config = ssh_manager.get_config(connection_id)
        result = ssh_manager.test_connection(config)
        return result
    except Exception as e:
        logger.error(f"Ошибка теста SSH: {e}")
        return {
            "success": False,
            "ssh_connected": False,
            "message": f"Ошибка: {str(e)}"
        }


# ===========================================
# Docker мониторинг
# ===========================================

@router.get("/docker/stats", summary="Получить статистику Docker контейнеров")
async def get_docker_stats():
    """
    Получить детальную статистику всех Docker контейнеров.
    """
    try:
        stats = docker_monitor.get_detailed_stats()
        return stats
    except Exception as e:
        logger.error(f"Ошибка получения Docker статистики: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/docker/system", summary="Получить информацию о Docker системе")
async def get_docker_system_info():
    """
    Получить общую информацию о Docker системе.
    """
    try:
        info = docker_monitor.get_system_info()
        return info
    except Exception as e:
        logger.error(f"Ошибка получения информации о Docker: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/docker/{container_name}/restart", summary="Перезапустить контейнер")
async def restart_container(container_name: str):
    """
    Перезапустить Docker контейнер.
    """
    try:
        success = docker_monitor.restart_container(container_name)
        if success:
            return {"status": "success", "message": f"Контейнер {container_name} перезапущен"}
        else:
            raise HTTPException(status_code=500, detail="Ошибка перезапуска контейнера")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Ошибка перезапуска контейнера: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/docker/{container_name}/stop", summary="Остановить контейнер")
async def stop_container(container_name: str):
    """Остановить Docker контейнер"""
    try:
        success = docker_monitor.stop_container(container_name)
        if success:
            return {"status": "success", "message": f"Контейнер {container_name} остановлен"}
        else:
            raise HTTPException(status_code=500, detail="Ошибка остановки контейнера")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Ошибка остановки контейнера: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/docker/{container_name}/start", summary="Запустить контейнер")
async def start_container(container_name: str):
    """Запустить Docker контейнер"""
    try:
        success = docker_monitor.start_container(container_name)
        if success:
            return {"status": "success", "message": f"Контейнер {container_name} запущен"}
        else:
            raise HTTPException(status_code=500, detail="Ошибка запуска контейнера")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Ошибка запуска контейнера: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/docker/{container_name}/logs", summary="Получить логи контейнера")
async def get_container_logs(container_name: str, lines: int = 100):
    """Получить логи Docker контейнера"""
    try:
        logs = docker_monitor.get_container_logs(container_name, lines)
        return {"logs": logs}
    except Exception as e:
        logger.error(f"Ошибка получения логов: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ===========================================
# System мониторинг (хостовая машина)
# ===========================================

@router.get("/system/info", summary="Получить информацию о системе хоста")
async def get_system_info():
    """Получить полную информацию о системе"""
    try:
        return system_monitor.get_system_info()
    except Exception as e:
        logger.error(f"Ошибка получения system info: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/system/cpu", summary="Получить информацию о CPU")
async def get_cpu_info():
    """Информация о CPU"""
    try:
        return system_monitor.get_cpu_info()
    except Exception as e:
        logger.error(f"Ошибка получения CPU info: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/system/memory", summary="Получить информацию о памяти")
async def get_memory_info():
    """Информация о памяти"""
    try:
        return system_monitor.get_memory_info()
    except Exception as e:
        logger.error(f"Ошибка получения memory info: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/system/disk", summary="Получить информацию о дисках")
async def get_disk_info():
    """Информация о дисках"""
    try:
        return system_monitor.get_disk_info()
    except Exception as e:
        logger.error(f"Ошибка получения disk info: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/system/network", summary="Получить информацию о сети")
async def get_network_info():
    """Информация о сети"""
    try:
        return system_monitor.get_network_info()
    except Exception as e:
        logger.error(f"Ошибка получения network info: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ===========================================
# Qdrant мониторинг (векторная БД)
# ===========================================

from src.api.services.qdrant_monitor import qdrant_monitor


@router.get("/qdrant/info", summary="Получить информацию о Qdrant")
async def get_qdrant_info(collection_name: str = "kag_documents"):
    """
    Получить полную информацию о Qdrant базе данных.

    Включает:
    - Список коллекций
    - Количество документов/векторов
    - Метаданные (payload schema)
    - Примеры документов
    """
    try:
        return qdrant_monitor.get_full_info(collection_name)
    except Exception as e:
        logger.error(f"Ошибка получения Qdrant info: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/qdrant/collections", summary="Получить список коллекций Qdrant")
async def get_qdrant_collections():
    """Получить список всех коллекций"""
    try:
        return qdrant_monitor.get_collections_list()
    except Exception as e:
        logger.error(f"Ошибка получения коллекций: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/qdrant/collections/{collection_name}", summary="Получить информацию о коллекции")
async def get_collection_info(collection_name: str):
    """Детальная информация о коллекции"""
    try:
        return qdrant_monitor.get_collection_info(collection_name)
    except Exception as e:
        logger.error(f"Ошибка получения информации о коллекции: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/qdrant/collections/{collection_name}/points", summary="Получить пример точек")
async def get_collection_points(collection_name: str, limit: int = 20):
    """Получить пример точек (документов) из коллекции"""
    try:
        return {"points": qdrant_monitor.get_points_sample(collection_name, limit)}
    except Exception as e:
        logger.error(f"Ошибка получения точек: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/qdrant/collections/{collection_name}/payload-stats", summary="Получить статистику метаданных")
async def get_payload_stats(collection_name: str):
    """Статистика по метаданным (payload)"""
    try:
        return qdrant_monitor.get_payload_stats(collection_name)
    except Exception as e:
        logger.error(f"Ошибка получения статистики payload: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/qdrant/collections/{collection_name}/chunks", summary="Получить чанки")
async def get_collection_chunks(
    collection_name: str,
    limit: int = 100,
    offset: int = 0,
    document_id: Optional[str] = None
):
    """Получить чанки из коллекции с пагинацией"""
    try:
        return qdrant_monitor.get_chunks(collection_name, limit, offset, document_id)
    except Exception as e:
        logger.error(f"Ошибка получения чанков: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ===========================================
# Настройки чанкинга
# ===========================================

class ChunkingConfigRequest(BaseModel):
    """Запрос на сохранение настроек чанкинга"""
    chunk_size: int = Field(default=1000, ge=100, le=5000)
    chunk_overlap: int = Field(default=200, ge=0, le=1000)


@router.get("/chunking-config", summary="Получить настройки чанкинга")
async def get_chunking_config():
    """Получить текущие настройки чанкинга из Redis"""
    from src.api.services.config_store import config_store
    
    config = config_store.get("chunking", "default", {
        "chunk_size": 1000,
        "chunk_overlap": 200
    })
    
    return config


@router.post("/chunking-config", summary="Сохранить настройки чанкинга")
async def save_chunking_config(request: ChunkingConfigRequest):
    """Сохранить настройки чанкинга в Redis"""
    from src.api.services.config_store import config_store
    
    config = {
        "chunk_size": request.chunk_size,
        "chunk_overlap": request.chunk_overlap
    }
    
    success = config_store.set("chunking", "default", config)
    
    if success:
        return {
            "status": "success",
            "message": "Настройки чанкинга сохранены в PostgreSQL",
            "config": config
        }
    else:
        raise HTTPException(status_code=500, detail="Ошибка сохранения")


# ===========================================
# Модели запросов/ответов
# ===========================================

class SwitchModelRequest(BaseModel):
    """Запрос на переключение модели"""
    backend_type: LLMBackendType = Field(..., description="Тип бэкенда")
    model_name: str = Field(..., description="Название модели")
    url: Optional[str] = Field(default=None, description="URL API")
    api_key: Optional[str] = Field(default=None, description="API ключ")
    provider: Optional[str] = Field(default=None, description="Провайдер")


class SwitchEmbeddingRequest(BaseModel):
    """Запрос на переключение embedding модели"""
    model_name: str = Field(..., description="Название embedding модели")


class PullModelRequest(BaseModel):
    """Запрос на загрузку модели"""
    model_name: str = Field(..., description="Название модели для загрузки")





# ===========================================
# API endpoints
# ===========================================


@router.post("/restart-ollama", summary="Перезапустить Ollama сервер")
async def restart_ollama(connection_id: str = "default"):
    """
    Перезапустить Ollama сервер через SSH с сохранёнными настройками.
    """
    import asyncio
    import subprocess
    import httpx
    
    try:
        config = ssh_manager.get_config(connection_id)
        logger.info(f"Перезапуск Ollama на {config.host}...")
        
        # Формируем команду
        if config.password:
            ssh_cmd = f"sshpass -p '{config.password}' ssh -o StrictHostKeyChecking=no -p {config.port} {config.username}@{config.host}"
        else:
            ssh_cmd = f"ssh -o StrictHostKeyChecking=no -p {config.port} {config.username}@{config.host}"
        
        # Выполняем перезапуск
        sudo_pass_part = f"echo '{config.sudo_password}' | " if config.sudo_password else ""
        restart_cmd = f"{ssh_cmd} '{sudo_pass_part}sudo -S systemctl restart {config.ollama_service_name}'"
        logger.debug(f"Выполняю: {restart_cmd}")
        
        result = await asyncio.to_thread(
            subprocess.run,
            restart_cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30
        )
        
        logger.info(f"Результат перезапуска: returncode={result.returncode}")
        
        # Ждём запуска
        await asyncio.sleep(8)
        
        # Проверяем статус
        status_cmd = f"{ssh_cmd} 'sudo systemctl is-active {config.ollama_service_name}'"
        status_result = await asyncio.to_thread(
            subprocess.run,
            status_cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=10
        )
        
        is_active = status_result.stdout.strip() == "active"
        
        # Проверяем что Ollama отвечает
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(f"http://{config.host}:{config.ollama_port}/")
                ollama_responding = response.status_code == 200
        except:
            ollama_responding = False
        
        return {
            "status": "success" if (is_active or ollama_responding) else "warning",
            "message": f"Ollama {'перезапущен успешно' if (is_active or ollama_responding) else 'перезапущен, но статус неизвестен'}",
            "service_active": is_active or ollama_responding,
            "systemctl_active": is_active,
            "http_responding": ollama_responding
        }
        
    except subprocess.TimeoutExpired:
        return {
            "status": "error",
            "message": "Таймаут при перезапуске Ollama"
        }
    except Exception as e:
        logger.error(f"Ошибка перезапуска Ollama: {e}")
        import traceback
        return {
            "status": "error",
            "message": f"Ошибка: {str(e)}"
        }

@router.get("/status", summary="Получить статус системы моделей")
async def get_models_status():
    """Получить полный статус всех моделей и бэкендов"""
    try:
        status = await model_manager.get_status()
        return status
    except Exception as e:
        logger.error(f"Ошибка получения статуса: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/llm", summary="Список доступных LLM моделей")
async def list_llm_models():
    """Получить список всех доступных LLM моделей"""
    try:
        models = await model_manager.list_llm_models()
        return [m.model_dump() for m in models]
    except Exception as e:
        logger.error(f"Ошибка получения LLM моделей: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/embeddings", summary="Список доступных embedding моделей")
async def list_embedding_models():
    """Получить список доступных embedding моделей"""
    try:
        models = await model_manager.list_embedding_models()
        return [m.model_dump() for m in models]
    except Exception as e:
        logger.error(f"Ошибка получения embedding моделей: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/ollama-models", summary="Все модели Ollama с деталями")
async def list_ollama_models():
    """Получить детальную информацию о всех моделях Ollama"""
    try:
        models = await model_manager.get_ollama_models_detailed()
        return models
    except Exception as e:
        logger.error(f"Ошибка получения моделей Ollama: {e}")
        raise HTTPException(status_code=500, detail=str(e))





@router.get("/llm-config", summary="Получить сохранённые настройки активной LLM")
async def get_llm_config():
    """Вернуть сохранённые настройки активной LLM из PostgreSQL."""
    cfg = config_store.get("llm_config", "active") or {}
    return {
        "backend_type": cfg.get("backend_type", "ollama"),
        "model_name": cfg.get("model_name", "phi4-mini:latest"),
        "url": cfg.get("url", "http://192.168.50.41:11434"),
        "api_key": cfg.get("api_key", ""),
        "provider": cfg.get("provider", "ollama"),
    }

@router.post("/switch-llm", summary="Переключить активную LLM модель")
async def switch_llm_model(request: SwitchModelRequest):
    """
    Переключить активную LLM модель и сохранить настройки в PostgreSQL.

    - **backend_type**: Тип бэкенда (ollama, vllm, openai, deepseek, openrouter)
    - **model_name**: Название модели
    - **url**: URL API (опционально)
    - **api_key**: API ключ (опционально)
    - **provider**: Провайдер (опционально)
    """
    try:
        success = await model_manager.switch_llm_model(
            request.backend_type,
            request.model_name
        )
        
        # Сохраняем настройки в PostgreSQL
        cfg = {
            "backend_type": request.backend_type.value if hasattr(request.backend_type, 'value') else str(request.backend_type),
            "model_name": request.model_name,
            "url": request.url or "",
            "api_key": request.api_key or "",
            "provider": request.provider or "",
        }
        config_store.set("llm_config", "active", cfg)
        
        if success:
            return {
                "status": "success",
                "message": f"Модель переключена на {request.model_name}",
                "config": cfg
            }
        else:
            raise HTTPException(status_code=400, detail="Не удалось переключить модель")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Ошибка переключения модели: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/switch-embedding", summary="Переключить embedding модель")
async def switch_embedding_model(request: SwitchEmbeddingRequest):
    """
    Переключить активную embedding модель.

    - **model_name**: Название embedding модели
    """
    try:
        success = await model_manager.switch_embedding_model(request.model_name)
        
        # Сохраняем в config_store
        try:
            from src.api.services.config_store import config_store
            config = config_store.get("embedding", "default") or {}
            config["model"] = request.model_name
            config_store.set("embedding", "default", config)
        except Exception as e:
            logger.warning(f"Не удалось сохранить embedding модель в config_store: {e}")
        
        if success:
            return {
                "status": "success",
                "message": f"Embedding модель переключена на {request.model_name}"
            }
        else:
            raise HTTPException(status_code=400, detail="Не удалось переключить embedding модель")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Ошибка переключения embedding модели: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/pull", summary="Загрузить модель из Ollama registry")
async def pull_model(request: PullModelRequest):
    """
    Загрузить новую модель из Ollama registry.

    - **model_name**: Название модели (например, llama2:7b)
    """
    try:
        result = await model_manager.pull_model(request.model_name)
        return result
    except Exception as e:
        logger.error(f"Ошибка загрузки модели: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/delete/{model_name}", summary="Удалить модель")
async def delete_model(model_name: str):
    """
    Удалить модель из Ollama.

    - **model_name**: Название модели для удаления
    """
    try:
        success = await model_manager.delete_model(model_name)
        
        if success:
            return {
                "status": "success",
                "message": f"Модель {model_name} удалена"
            }
        else:
            raise HTTPException(status_code=400, detail="Не удалось удалить модель")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Ошибка удаления модели: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# === Внешние LLM для анализа документов ===

from pydantic import BaseModel
from typing import Optional as Opt

class ExtLLMConfig(BaseModel):
    url: str = "http://192.168.50.41:11434"
    model: str = "phi4-mini"
    api_key: Opt[str] = None
    provider: str = "ollama"

_ext_llm_config: ExtLLMConfig = ExtLLMConfig()

# Инициализация из БД при старте модуля
try:
    from src.api.services.config_store import config_store
    saved = config_store.get("ext_llm", "default")
    if saved and (saved.get("model") or saved.get("api_key")):
        _ext_llm_config = ExtLLMConfig(
            url=saved.get("url", ""),
            model=saved.get("model", ""),
            provider=saved.get("provider", "ollama"),
            api_key=saved.get("api_key", "")
        )
except Exception:
    pass


@router.post("/ext-llm", summary="Сохранить настройки внешнего LLM")
async def save_ext_llm(config: ExtLLMConfig):
    """Сохранить настройки внешнего LLM в config_store (PostgreSQL)."""
    global _ext_llm_config
    _ext_llm_config = config
    # Персистентное сохранение в БД
    try:
        from src.api.services.config_store import config_store
        config_store.set("ext_llm", "default", {
            "url": config.url,
            "model": config.model,
            "provider": config.provider,
            "api_key": config.api_key
        })
    except Exception as e:
        logger.warning(f"Не удалось сохранить ext_llm в config_store: {e}")
    logger.info(f"Внешний LLM настроен: {config.provider}/{config.model} @ {config.url}")
    return {"status": "ok", "message": "Настройки сохранены"}


@router.get("/ext-llm", summary="Получить настройки внешнего LLM")
async def get_ext_llm():
    """Получить текущие настройки внешнего LLM.
    
    ВСЕГДА загружает из config_store (PostgreSQL) приоритетно.
    Глобальная переменная — только fallback если БД недоступна.
    """
    global _ext_llm_config
    # Всегда пробуем загрузить из БД
    try:
        from src.api.services.config_store import config_store
        saved = config_store.get("ext_llm", "default")
        if saved and (saved.get("model") or saved.get("api_key")):
            _ext_llm_config = ExtLLMConfig(
                url=saved.get("url", ""),
                model=saved.get("model", ""),
                provider=saved.get("provider", "ollama"),
                api_key=saved.get("api_key", "")
            )
    except Exception:
        pass
    return {
        "url": _ext_llm_config.url,
        "model": _ext_llm_config.model,
        "provider": _ext_llm_config.provider,
        "api_key": _ext_llm_config.api_key
    }


@router.post("/ext-llm/test", summary="Тест подключения к внешнему LLM")
async def test_ext_llm():
    """Проверить подключение к внешнему LLM."""
    import aiohttp
    
    try:
        if _ext_llm_config.provider == "ollama":
            url = f"{_ext_llm_config.url}/api/generate"
            payload = {
                "model": _ext_llm_config.model,
                "prompt": "Ответь одним словом: ОК",
                "stream": False,
                "options": {"max_tokens": 5}
            }
            headers = {}
        elif _ext_llm_config.provider in ("openai", "deepseek", "openrouter"):
            # OpenAI-совместимый API
            url = f"{_ext_llm_config.url}/v1/chat/completions"
            payload = {
                "model": _ext_llm_config.model,
                "messages": [{"role": "user", "content": "Say OK"}],
                "max_tokens": 5
            }
            headers = {"Authorization": f"Bearer {_ext_llm_config.api_key}"} if _ext_llm_config.api_key else {}
        else:
            return {"ok": False, "error": f"Провайдер {_ext_llm_config.provider} пока не поддерживается для теста"}
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    response_text = data.get("response") or data.get("choices", [{}])[0].get("message", {}).get("content", "")
                    return {"ok": True, "response": response_text[:100]}
                else:
                    body = await resp.text()
                    return {"ok": False, "error": f"HTTP {resp.status}: {body[:100]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/ext-llm/models", summary="Список моделей внешнего провайдера")
async def list_ext_llm_models(provider: str = "ollama"):
    """Получить список доступных моделей для указанного провайдера.
    
    Для Ollama — возвращает локально загруженные модели.
    Для OpenAI/DeepSeek/OpenRouter — обращается к API провайдера с сохранённым ключом.
    """
    import aiohttp
    try:
        if provider == "ollama":
            # Локальные модели
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{_ext_llm_config.url}/api/tags",
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        models = [{"id": m.get("name", m.get("model", "")), "name": m.get("name", "")}
                                  for m in data.get("models", [])]
                        return {"models": models, "provider": "ollama"}
                    return {"models": [], "error": f"Ollama: HTTP {resp.status}"}
        
        # Внешние провайдеры — OpenAI-совместимый API
        api_key = _ext_llm_config.api_key
        if not api_key:
            return {"models": [], "error": "API ключ не указан. Сохраните ключ в настройках."}
        
        headers = {"Authorization": f"Bearer {api_key}"}
        # OpenRouter и OpenAI используют /v1/models
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{_ext_llm_config.url}/v1/models",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    # OpenAI формат: {"data": [{"id": "gpt-4", ...}, ...]}
                    # OpenRouter: {"data": [{"id": "openai/gpt-4o", "name": "GPT-4o", ...}, ...]}
                    models = []
                    for m in data.get("data", []):
                        models.append({
                            "id": m.get("id", ""),
                            "name": m.get("name", m.get("id", ""))
                        })
                    return {"models": models, "provider": provider}
                else:
                    text = await resp.text()
                    return {"models": [], "error": f"HTTP {resp.status}: {text[:200]}"}
    except Exception as e:
        return {"models": [], "error": str(e)}


@router.get("/ext-llm/balance", summary="Проверить баланс провайдера")
async def check_ext_llm_balance():
    """Проверить состояние баланса/кредитов внешнего провайдера.
    
    Поддерживает:
    - OpenAI: GET /v1/dashboard/billing/subscription (остаток кредитов)
    - DeepSeek: GET /v1/user/balance (баланс в токенах)
    - OpenRouter: GET /api/v1/credits (оставшиеся кредиты)
    - Ollama: всегда возвращает ok (локальный — безлимитный)
    """
    import aiohttp
    prov = _ext_llm_config.provider
    api_key = _ext_llm_config.api_key
    
    try:
        if prov == "ollama":
            return {"provider": "ollama", "balance_ok": True, "message": "Локальный сервер — без ограничений"}
        
        if not api_key:
            return {"provider": prov, "balance_ok": False, "message": "API ключ не указан", "balance": 0}
        
        headers = {"Authorization": f"Bearer {api_key}"}
        
        if prov == "openrouter":
            # OpenRouter: GET /api/v1/credits
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{_ext_llm_config.url}/api/v1/credits",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        credits = data.get("data", {}).get("total_credits", 0)
                        used = data.get("data", {}).get("total_usage", 0)
                        remaining = credits - used
                        return {
                            "provider": prov,
                            "balance_ok": remaining > 0,
                            "balance": round(remaining, 4),
                            "total_credits": credits,
                            "total_usage": round(used, 4),
                            "message": f"Остаток: ${remaining:.4f} из ${credits:.2f}"
                        }
                    return {"provider": prov, "balance_ok": False, "message": f"HTTP {resp.status}"}
        
        elif prov == "openai":
            # OpenAI: пробуем usage endpoint
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://api.openai.com/v1/usage?date=" + __import__('datetime').datetime.utcnow().strftime('%Y-%m-%d'),
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        return {"provider": prov, "balance_ok": True, "message": "API доступен"}
                    # Fallback: проверяем просто доступность
                    if resp.status in (401, 403):
                        return {"provider": prov, "balance_ok": False, "message": "API ключ недействителен", "balance": 0}
                # Простой тест — список моделей
                async with session.get(
                    f"{_ext_llm_config.url}/v1/models",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        return {"provider": prov, "balance_ok": True, "message": "API доступен (проверьте баланс в панели OpenAI)"}
                    return {"provider": prov, "balance_ok": False, "message": f"HTTP {resp.status}"}
        
        elif prov == "deepseek":
            # DeepSeek: GET /v1/user/balance
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{_ext_llm_config.url}/v1/user/balance",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        balance = data.get("balance", data.get("data", {}).get("balance", 0))
                        return {
                            "provider": prov,
                            "balance_ok": float(balance) > 0 if balance else True,
                            "balance": balance,
                            "message": f"Баланс: {balance} токенов"
                        }
                    return {"provider": prov, "balance_ok": False, "message": f"HTTP {resp.status}"}
        
        return {"provider": prov, "balance_ok": None, "message": f"Провайдер {prov} — проверка баланса не реализована"}
    
    except Exception as e:
        return {"provider": prov, "balance_ok": False, "message": str(e), "balance": 0}





@router.get("/graph/balance", summary="Проверить баланс провайдера граф-модели")
async def check_graph_balance():
    """Проверить баланс провайдера граф-модели (OpenAI/DeepSeek/OpenRouter).
    Возвращает баланс в долларах или юанях."""
    import aiohttp
    prov = _graph_model_config.get("provider", "ollama")
    api_key = _graph_model_config.get("api_key", "")
    
    if prov == "ollama":
        return {"provider": "ollama", "balance_ok": True, "message": "Локальный — безлимитный", "display": "∞"}
    
    if not api_key:
        return {"provider": prov, "balance_ok": False, "message": "API ключ не указан"}
    
    try:
        if prov == "openrouter":
            url = "https://openrouter.ai/api/v1/credits"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers={"Authorization": f"Bearer {api_key}"}, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        credits = data.get("data", {}).get("total_credits", 0)
                        used = data.get("data", {}).get("total_usage", 0)
                        remaining = credits - used
                        return {"provider": "openrouter", "balance_ok": True, "balance_usd": remaining, "display": f"${remaining:.2f} (из ${credits:.2f})"}
        
        elif prov == "deepseek":
            url = f"{_graph_model_config.get('url', 'https://api.deepseek.com')}/v1/user/balance"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers={"Authorization": f"Bearer {api_key}"}, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        bal = data.get("balance_infos", data.get("data", []))
                        if isinstance(bal, list) and bal:
                            b = bal[0]
                            currency = b.get("currency", "USD")
                            amount = float(b.get("total_balance", b.get("balance", 0)))
                            return {"provider": "deepseek", "balance_ok": True, "balance_usd": amount, "display": f"{currency} {amount:.2f}"}
        
        elif prov == "openai":
            # OpenAI не отдаёт баланс напрямую — просто проверяем доступность ключа
            url = f"{_graph_model_config.get('url', 'https://api.openai.com')}/v1/models"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers={"Authorization": f"Bearer {api_key}"}, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        return {"provider": "openai", "balance_ok": True, "display": "✅ API ключ активен", "message": "OpenAI не предоставляет баланс через API"}
                    return {"provider": "openai", "balance_ok": False, "message": f"HTTP {resp.status}"}
        
        return {"provider": prov, "balance_ok": None, "message": f"Провайдер {prov} — проверка не реализована"}
    except Exception as e:
        return {"provider": prov, "balance_ok": False, "message": str(e)}

_graph_model_config = {"model": "phi4-mini:latest", "provider": "ollama"}

# Инициализация из БД при старте
try:
    from src.api.services.config_store import config_store
    saved = config_store.get("graph_model", "default")
    if saved and saved.get("model"):
        _graph_model_config = saved
except Exception:
    pass

@router.get("/graph", summary="Получить модель для графа")
async def get_graph_model():
    # Пробуем загрузить из config_store
    try:
        from src.api.services.config_store import config_store
        saved = config_store.get("graph_model", "default")
        if saved and saved.get("model"):
            return saved
    except Exception:
        pass
    return _graph_model_config

@router.post("/graph", summary="Сохранить модель для графа")
async def save_graph_model(config: dict):
    global _graph_model_config
    _graph_model_config = config
    # Сохраняем в config_store
    try:
        from src.api.services.config_store import config_store
        config_store.set("graph_model", "default", config)
    except Exception as e:
        logger.warning(f"Не удалось сохранить в config_store: {e}")
    logger.info(f"Graph model set: model={config.get('model')} provider={config.get('provider')}")
    return {"status": "ok", "message": "Модель для графа сохранена"}


# ===========================================
# Деплой (обновление из Git)
# ===========================================

class DeployRequest(BaseModel):
    file_content: Optional[str] = Field(default=None, description="Содержимое файла для записи (base64 или текст)")
    file_path: Optional[str] = Field(default=None, description="Путь к файлу относительно /app/src/")
    action: str = Field(default="write_file", description="Действие: write_file | git_pull | restart")

@router.post("/deploy", summary="Деплой: запись файла, git pull или перезапуск")
async def deploy_action(req: DeployRequest):
    """
    Универсальный endpoint для деплоя:
    - write_file: записать содержимое в файл на диске
    - git_pull: выполнить git pull в /home/yartsevn/kag-system
    - restart: перезапустить Docker-контейнер api
    """
    import subprocess
    import os
    import base64

    if req.action == "write_file":
        if not req.file_content or not req.file_path:
            return {"status": "error", "message": "file_content и file_path обязательны для write_file"}

        full_path = os.path.join("/app/src", req.file_path)
        # Безопасность: только внутри /app/src
        if not os.path.realpath(full_path).startswith("/app/src"):
            return {"status": "error", "message": "Недопустимый путь"}

        try:
            content = req.file_content
            # Пробуем декодировать base64
            try:
                content = base64.b64decode(req.file_content).decode("utf-8")
            except Exception:
                pass  # Не base64 — используем как есть

            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(content)

            logger.info(f"Deploy: записан файл {full_path} ({len(content)} байт)")
            return {"status": "ok", "message": f"Файл {req.file_path} записан ({len(content)} байт)"}
        except Exception as e:
            logger.error(f"Deploy: ошибка записи {req.file_path}: {e}")
            return {"status": "error", "message": str(e)}

    elif req.action == "git_pull":
        try:
            result = subprocess.run(
                ["git", "pull"],
                cwd="/home/yartsevn/kag-system",
                capture_output=True,
                text=True,
                timeout=60
            )
            logger.info(f"Git pull: {result.stdout}")
            return {
                "status": "ok" if result.returncode == 0 else "error",
                "stdout": result.stdout,
                "stderr": result.stderr
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    elif req.action == "restart":
        try:
            result = subprocess.run(
                ["docker", "compose", "up", "-d", "--no-deps", "--force-recreate", "api"],
                cwd="/home/yartsevn/kag-system",
                capture_output=True,
                text=True,
                timeout=120
            )
            logger.info(f"Docker restart: {result.stdout}")
            return {
                "status": "ok" if result.returncode == 0 else "error",
                "stdout": result.stdout,
                "stderr": result.stderr
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    return {"status": "error", "message": f"Неизвестное действие: {req.action}"}


# ===========================================
# Конфигурация разрешённых форматов загрузки
# ===========================================

# Все поддерживаемые форматы
ALL_SUPPORTED_EXTENSIONS = {
    ".pdf":  "PDF",
    ".docx": "DOCX",
    ".doc":  "DOC",
    ".txt":  "TXT",
    ".md":   "MD",
    ".csv":  "CSV",
    ".odt":  "ODT",
    ".rtf":  "RTF",
    ".png":  "PNG",
    ".jpg":  "JPG",
    ".jpeg": "JPEG",
    ".gif":  "GIF",
}

def _get_allowed_extensions() -> dict:
    """Загрузить разрешённые расширения из config_store."""
    try:
        from src.api.services.config_store import config_store
        saved = config_store.get("upload_config", "allowed_extensions")
        if saved and isinstance(saved, dict):
            return saved
    except Exception:
        pass
    # Default: all except image formats
    return {ext: True for ext in ALL_SUPPORTED_EXTENSIONS if ext not in ('.png', '.jpg', '.jpeg', '.gif')}


@router.get("/upload-config", summary="Разрешённые форматы загрузки")
async def get_upload_config():
    """Получить список разрешённых форматов."""
    allowed = _get_allowed_extensions()
    return {
        "all_formats": ALL_SUPPORTED_EXTENSIONS,
        "allowed": allowed
    }


class UploadConfigRequest(BaseModel):
    allowed: dict = Field(default={}, description="Словарь {'.ext': True/False}")

@router.post("/upload-config", summary="Сохранить разрешённые форматы")
async def save_upload_config(req: UploadConfigRequest):
    """Сохранить список разрешённых форматов."""
    try:
        from src.api.services.config_store import config_store
        config_store.set("upload_config", "allowed_extensions", req.allowed)
        return {"status": "ok", "message": "Настройки форматов сохранены"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ===========================================
# Мониторинг системы и Docker
# ===========================================

@router.get("/system/info", summary="Информация о системе")
async def get_system_info():
    """CPU, память, диск, ОС."""
    try:
        from src.api.services.system_monitor import system_monitor
        info = system_monitor.get_system_info()
        
        # Добавляем моментальные метрики
        try:
            import psutil
            info["cpu_pct"] = psutil.cpu_percent(interval=0.1)
            info["mem"] = {
                "used": psutil.virtual_memory().used,
                "total": psutil.virtual_memory().total,
                "percent": psutil.virtual_memory().percent,
                "available": psutil.virtual_memory().available,
            }
            info["dsk"] = {
                "used": psutil.disk_usage('/').used,
                "total": psutil.disk_usage('/').total,
                "free": psutil.disk_usage('/').free,
                "percent": psutil.disk_usage('/').percent,
            }
        except Exception:
            # Fallback to system_monitor data
            cpu_data = info.get("cpu", {})
            info["cpu_pct"] = cpu_data.get("usage_percent", 0)
            mem_data = info.get("memory", {})
            info["mem"] = {
                "used": mem_data.get("used", 0),
                "total": mem_data.get("total", 0),
                "percent": mem_data.get("percent", 0),
                "available": mem_data.get("available", 0),
            }
            dsk_list = info.get("disk", [])
            dsk = dsk_list[0] if dsk_list else {}
            info["dsk"] = {
                "used": dsk.get("used", 0),
                "total": dsk.get("total", 0),
                "free": dsk.get("free", 0),
                "percent": dsk.get("percent", 0),
            }
        return info
    except Exception as e:
        return {"error": str(e), "hostname": "unknown", "cpu_pct": 0, "mem": {}, "dsk": {}}


@router.get("/docker/stats", summary="Статистика Docker")
async def get_docker_stats():
    """Список контейнеров через docker CLI или psutil."""
    import subprocess, os
    result = {"containers": [], "system": {}}
    
    # Try docker CLI
    try:
        ps_out = subprocess.check_output(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}\t{{.ID}}", "--no-trunc"],
            timeout=5, stderr=subprocess.DEVNULL
        ).decode().strip()
        for line in ps_out.split('\n'):
            if line.strip():
                parts = line.split('\t')
                if len(parts) >= 3:
                    state = "running" if "Up" in parts[1] else "stopped"
                    result["containers"].append({
                        "name": parts[0], "state": state, "status": parts[1],
                        "image": parts[2], "id": parts[3][:12] if len(parts) > 3 else ""
                    })
        # Docker system info
        info_out = subprocess.check_output(["docker", "info", "--format", "{{.ContainersRunning}}/{{.Containers}}"], timeout=5, stderr=subprocess.DEVNULL).decode().strip()
        parts = info_out.split('/')
        result["system"] = {"containers_running": int(parts[0]) if parts[0].isdigit() else 0, "containers_total": int(parts[1]) if len(parts)>1 and parts[1].isdigit() else 0}
    except Exception:
        # Fallback: return self + host system info
        import psutil
        result["containers"].append({
            "name": "kag-api", "state": "running",
            "image": "kag-system_api:latest",
            "stats": {
                "cpu_percent": round(psutil.cpu_percent(interval=0.1), 1),
                "mem_used": psutil.virtual_memory().used,
                "mem_limit": psutil.virtual_memory().total,
                "pids": len(psutil.pids())
            }
        })
        result["system"] = {"containers_running": 1, "containers_total": 1}
        result["note"] = "Docker socket not available — showing kag-api only"
    return result


@router.get("/docker/{container_name}/logs", summary="Логи контейнера")
async def get_container_logs(container_name: str, lines: int = 30):
    """Последние N строк логов контейнера. Только безопасные имена."""
    import re, subprocess
    # Sanitize: only allow alphanumeric, hyphens, underscores
    if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]*$', container_name):
        return {"container": container_name, "logs": "", "error": "Invalid container name"}
    try:
        out = subprocess.check_output(
            ["docker", "logs", "--tail", str(max(1, min(lines, 200))), container_name],
            timeout=10, stderr=subprocess.STDOUT
        ).decode(errors='replace')
        return {"container": container_name, "logs": out[-10000:]}
    except Exception as e:
        return {"container": container_name, "logs": "", "error": str(e)}


# ============================================================
# Системный промпт для чата
# ============================================================

@router.get("/chat-prompt", summary="Получить системный промпт чата")
async def get_chat_prompt():
    """Получить текущий системный промпт для чата."""
    try:
        from src.api.services.config_store import config_store
        saved = config_store.get("llm", "default") or {}
        prompt = saved.get("system_prompt", "")
        return {"prompt": prompt}
    except Exception as e:
        return {"prompt": "", "error": str(e)}


@router.post("/chat-prompt", summary="Сохранить системный промпт чата")
async def save_chat_prompt(data: dict):
    """Сохранить системный промпт для чата в config_store."""
    try:
        from src.api.services.config_store import config_store
        prompt = data.get("prompt", "")
        existing = config_store.get("llm", "default") or {}
        existing["system_prompt"] = prompt
        config_store.set("llm", "default", existing)
        logger.info(f"Системный промпт чата сохранён ({len(prompt)} символов)")
        return {"status": "ok", "message": "Промпт сохранён"}
    except Exception as e:
        return {"status": "error", "message": str(e)}



# ============================================================
# Единая система управления провайдерами LLM
# ============================================================
# Новая архитектура: каждый провайдер (Ollama, OpenAI, DeepSeek...)
# хранится отдельно в config_store("providers", id).
# Каждая функция (chat, embedding, graph, doc_analysis) привязана
# к провайдеру + модели через config_store("function_map", function).
#
# Провайдер — это источник LLM (credentials, URL).
# Функция — это роль (чат, эмбеддинг, граф, анализ).
# ============================================================

from src.api.services.provider_service import (
    provider_service, ProviderConfig, FunctionMap,
    PROVIDER_TYPES, FUNCTION_DEFINITIONS,
)


@router.get("/provider-types", summary="Типы провайдеров")
async def get_provider_types():
    """Вернуть список поддерживаемых типов провайдеров с метаданными."""
    return PROVIDER_TYPES


@router.get("/function-definitions", summary="Определения функций")
async def get_function_definitions():
    """Вернуть список функций, которые могут использовать LLM."""
    return FUNCTION_DEFINITIONS


@router.get("/providers", summary="Список всех провайдеров")
async def list_providers():
    """Вернуть список всех провайдеров (без API-ключей)."""
    return provider_service.list_providers()


@router.get("/providers/{provider_id}", summary="Получить провайдера")
async def get_provider(provider_id: str):
    """Вернуть провайдера по ID (без API-ключа)."""
    p = provider_service.get_provider(provider_id)
    if not p:
        raise HTTPException(status_code=404, detail="Провайдер не найден")
    return p


class ProviderSaveRequest(BaseModel):
    """Запрос на сохранение провайдера"""
    id: str = Field(default="", description="ID провайдера (пусто = создать новый)")
    name: str = Field(default="", description="Название")
    type: str = Field(default="ollama", description="Тип: ollama, openai, deepseek, openrouter, custom")
    url: str = Field(default="", description="URL API")
    api_key: str = Field(default="", description="API ключ (опционально)")
    enabled: bool = Field(default=True, description="Включён")


@router.post("/providers", summary="Сохранить провайдера")
async def save_provider(req: ProviderSaveRequest):
    """Создать или обновить провайдера."""
    import uuid

    config = ProviderConfig(
        id=req.id or f"provider-{uuid.uuid4().hex[:8]}",
        name=req.name,
        type=req.type,
        url=req.url,
        api_key=req.api_key,
        enabled=req.enabled,
    )

    success = provider_service.save_provider(config)
    if not success:
        raise HTTPException(status_code=500, detail="Ошибка сохранения провайдера")

    return {
        "status": "success",
        "message": f"Провайдер {config.name} сохранён",
        "provider": config.to_dict(include_secret=False),
    }


@router.delete("/providers/{provider_id}", summary="Удалить провайдера")
async def delete_provider(provider_id: str):
    """Удалить провайдера и все его привязки."""
    success = provider_service.delete_provider(provider_id)
    if not success:
        raise HTTPException(status_code=404, detail="Провайдер не найден")
    return {"status": "success", "message": f"Провайдер {provider_id} удалён"}


@router.post("/providers/{provider_id}/fetch-models", summary="Запросить модели провайдера")
async def fetch_provider_models(provider_id: str):
    """Получить список моделей провайдера через его API и обновить кэш."""
    models = await provider_service.fetch_provider_models(provider_id)
    return {
        "provider_id": provider_id,
        "models": models,
        "count": len(models),
    }


@router.post("/providers/{provider_id}/test", summary="Проверить подключение к провайдеру")
async def test_provider_connection(provider_id: str):
    """Проверить, что провайдер отвечает."""
    provider = provider_service.get_provider_with_key(provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Провайдер не найден")

    import httpx
    try:
        if provider.type == "ollama":
            url = f"{provider.url}/api/tags"
        else:
            url = f"{provider.url.rstrip('/')}/v1/models"

        headers = {}
        if provider.api_key:
            headers["Authorization"] = f"Bearer {provider.api_key}"

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                model_count = 0
                if provider.type == "ollama":
                    model_count = len(data.get("models", []))
                else:
                    model_count = len(data.get("data", []))

                return {
                    "ok": True,
                    "message": f"✅ Подключение успешно, {model_count} моделей",
                    "model_count": model_count,
                    "response_time_ms": resp.elapsed.total_seconds() * 1000,
                }
            else:
                body = await resp.text()
                return {
                    "ok": False,
                    "message": f"❌ HTTP {resp.status_code}: {body[:200]}",
                }
    except Exception as e:
        return {"ok": False, "message": f"❌ {str(e)}"}


# ===========================================
# Привязка функций к провайдерам
# ===========================================

@router.get("/functions", summary="Список привязок функций")
async def list_function_maps():
    """Вернуть все привязки функций к провайдерам."""
    return provider_service.list_function_maps()


@router.get("/functions/{function_name}", summary="Получить привязку функции")
async def get_function_map(function_name: str):
    """Вернуть привязку функции к провайдеру."""
    fm = provider_service.get_function_map(function_name)
    if not fm:
        # Возвращаем пустой шаблон для функции
        func_def = FUNCTION_DEFINITIONS.get(function_name, {})
        return {
            "function": function_name,
            "provider_id": provider_service.get_default_provider_id() or "",
            "model": "",
            "system_prompt": "",
            "parameters": {"temperature": 0.7, "max_tokens": 4096},
            "is_default": True,
        }
    return fm


class FunctionMapSaveRequest(BaseModel):
    """Запрос на сохранение привязки функции"""
    function: str = Field(default="", description="Название функции")
    provider_id: str = Field(default="", description="ID провайдера")
    model: str = Field(default="", description="Модель")
    system_prompt: str = Field(default="", description="Системный промпт")
    parameters: dict = Field(default_factory=lambda: {"temperature": 0.7, "max_tokens": 4096})


@router.post("/functions", summary="Сохранить привязку функции")
async def save_function_map(req: FunctionMapSaveRequest):
    """Сохранить привязку функции к провайдеру."""
    # Валидация
    if req.function not in FUNCTION_DEFINITIONS:
        raise HTTPException(status_code=400, detail=f"Неизвестная функция: {req.function}")

    fm = FunctionMap(
        function=req.function,
        provider_id=req.provider_id,
        model=req.model,
        system_prompt=req.system_prompt,
        parameters=req.parameters or {"temperature": 0.7, "max_tokens": 4096},
    )

    success = provider_service.save_function_map(fm)
    if not success:
        raise HTTPException(status_code=500, detail="Ошибка сохранения привязки")

    return {
        "status": "success",
        "message": f"Привязка функции {req.function} сохранена",
        "mapping": fm.to_dict(),
    }


@router.post("/ensure-default-provider", summary="Создать провайдера по умолчанию")
async def ensure_default_provider():
    """Создать провайдера по умолчанию (Ollama) и дефолтные привязки, если пусто."""
    success = provider_service.ensure_defaults()
    return {
        "status": "success" if success else "error",
        "providers": provider_service.list_providers(),
        "functions": provider_service.list_function_maps(),
    }


@router.post("/migrate-old-config", summary="Мигрировать старые настройки в новую систему")
async def migrate_old_config():
    """Прочитать старые конфиги (llm_config, ext_llm, graph_model, embedding)
    и импортировать их в новую систему провайдеров."""
    from src.api.services.config_store import config_store as cs
    import uuid

    results = {"migrated": [], "errors": []}
    providers_map = {}  # old key -> new provider_id

    # 1. LLM config (чат)
    try:
        llm_cfg = cs.get("llm_config", "active") or {}
        if llm_cfg and llm_cfg.get("model_name"):
            pid = f"migrated-{uuid.uuid4().hex[:6]}"
            ptype = llm_cfg.get("backend_type", llm_cfg.get("provider", "ollama"))
            provider_config = ProviderConfig(
                id=pid,
                name=f"Мигрированный: {ptype} (чат)",
                type=ptype,
                url=llm_cfg.get("url", ""),
                api_key=llm_cfg.get("api_key", ""),
                enabled=True,
            )
            if provider_service.save_provider(provider_config):
                providers_map["llm_config/active"] = pid
                fm = FunctionMap(
                    function="chat",
                    provider_id=pid,
                    model=llm_cfg.get("model_name", ""),
                )
                provider_service.save_function_map(fm)
                results["migrated"].append(f"llm_config → провайдер {pid} (чат)")
    except Exception as e:
        results["errors"].append(f"llm_config: {e}")

    # 2. Embedding config
    try:
        emb_cfg = cs.get("embedding", "default") or {}
        emb_model = emb_cfg.get("model", "")
        if emb_model:
            # Используем тот же провайдер, если он уже есть
            chat_pid = providers_map.get("llm_config/active")
            if chat_pid:
                fm = FunctionMap(
                    function="embedding",
                    provider_id=chat_pid,
                    model=emb_model,
                )
                provider_service.save_function_map(fm)
                results["migrated"].append(f"embedding → привязан к {chat_pid}")
    except Exception as e:
        results["errors"].append(f"embedding: {e}")

    # 3. Graph model
    try:
        graph_cfg = cs.get("graph_model", "default") or {}
        if graph_cfg and graph_cfg.get("model"):
            pid = f"migrated-{uuid.uuid4().hex[:6]}"
            ptype = graph_cfg.get("provider", "ollama")
            provider_config = ProviderConfig(
                id=pid,
                name=f"Мигрированный: {ptype} (граф)",
                type=ptype,
                url=graph_cfg.get("url", ""),
                api_key=graph_cfg.get("api_key", ""),
                enabled=True,
            )
            if provider_service.save_provider(provider_config):
                providers_map["graph_model/default"] = pid
                fm = FunctionMap(
                    function="graph",
                    provider_id=pid,
                    model=graph_cfg.get("model", ""),
                    system_prompt=graph_cfg.get("system_prompt", ""),
                )
                provider_service.save_function_map(fm)
                results["migrated"].append(f"graph_model → провайдер {pid}")
    except Exception as e:
        results["errors"].append(f"graph_model: {e}")

    # 4. Ext LLM (анализ документов)
    try:
        ext_cfg = cs.get("ext_llm", "default") or {}
        if ext_cfg and ext_cfg.get("model"):
            pid = f"migrated-{uuid.uuid4().hex[:6]}"
            ptype = ext_cfg.get("provider", "ollama")
            provider_config = ProviderConfig(
                id=pid,
                name=f"Мигрированный: {ptype} (анализ доков)",
                type=ptype,
                url=ext_cfg.get("url", ""),
                api_key=ext_cfg.get("api_key", ""),
                enabled=True,
            )
            if provider_service.save_provider(provider_config):
                fm = FunctionMap(
                    function="doc_analysis",
                    provider_id=pid,
                    model=ext_cfg.get("model", ""),
                )
                provider_service.save_function_map(fm)
                results["migrated"].append(f"ext_llm → провайдер {pid}")
    except Exception as e:
        results["errors"].append(f"ext_llm: {e}")

    return {
        "status": "ok",
        "results": results,
        "providers": provider_service.list_providers(),
        "functions": provider_service.list_function_maps(),
    }


# ============================================================
# Типы документов (авто-пополняемый список)
# ============================================================

@router.get("/doc-types", summary="Получить список типов документов")
async def get_doc_types():
    try:
        from src.api.services.config_store import config_store
        type_list = config_store.get("kg_config", "doc_types") or {}
        types = type_list.get("types", []) if isinstance(type_list, dict) else []
        return {"types": types}
    except Exception as e:
        return {"types": [], "error": str(e)}


@router.post("/doc-types", summary="Изменить список типов")
async def update_doc_types(data: dict):
    try:
        from src.api.services.config_store import config_store
        action = data.get("action", "add")
        name = data.get("name", "").strip().lower()
        if not name:
            return {"status": "error", "message": "Имя типа не указано"}
        
        type_list = config_store.get("kg_config", "doc_types") or {}
        types = type_list.get("types", []) if isinstance(type_list, dict) else []
        
        if action == "add" and name not in types:
            types.append(name)
        elif action == "remove" and name in types:
            types.remove(name)
        else:
            return {"status": "ok", "message": "Без изменений"}
        
        config_store.set("kg_config", "doc_types", {"types": types})
        return {"status": "ok", "message": f"Тип '{name}' {'добавлен' if action == 'add' else 'удалён'}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}
