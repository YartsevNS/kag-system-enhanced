"""
Маршруты для управления моделями LLM и Embeddings

Админ-панель для:
- Просмотра доступных моделей
- Переключения между моделями
- Загрузки новых моделей
- Мониторинга статуса бэкендов
- Выбора embedding модели
"""

import os
import asyncio
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Query

from loguru import logger
from pydantic import BaseModel, Field

from src.api.services.model_manager import model_manager
from src.api.services.ssh_manager import ssh_manager, SSHConnectionConfig
from src.api.services.docker_monitor import docker_monitor
from src.api.services.system_monitor import system_monitor
from src.config import get_settings
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

    ВАЖНО: docker_monitor.get_detailed_stats() — СИНХРОННЫЙ docker SDK
    (собирает stats по всем контейнерам). Прямой вызов блокировал event
    loop — api переставал отвечать (healthcheck «exceeded timeout»).
    Через asyncio.to_thread.
    """
    try:
        stats = await asyncio.to_thread(docker_monitor.get_detailed_stats)
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
        info = await asyncio.to_thread(docker_monitor.get_system_info)
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
    """Получить логи Docker контейнера.

    ВАЖНО: docker_monitor.get_container_logs() — СИНХРОННЫЙ вызов docker SDK
    (.logs() читает stdout контейнера через сокет). Прямой вызов в async-
    функции блокировал event loop api на всё время чтения — страница /logs
    («все источники») дёргала 8 таких запросов и api «зависал» (даже health
    переставал отвечать). Поэтому — через asyncio.to_thread.
    """
    try:
        logs = await asyncio.to_thread(docker_monitor.get_container_logs, container_name, lines)
        return {"logs": logs}
    except Exception as e:
        logger.error(f"Ошибка получения логов: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ===========================================
# System мониторинг (хостовая машина)
# ===========================================

@router.get("/system/info", summary="Получить информацию о системе хоста")
async def get_system_info():
    """Получить полную информацию о системе хоста.

    system_monitor.get_system_info() — синхронный (subprocess/psutil);
    через to_thread, чтобы не блокировать event loop.
    """
    try:
        return await asyncio.to_thread(system_monitor.get_system_info)
    except Exception as e:
        logger.error(f"Ошибка получения информации о системе: {e}")
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
    chunk_size: int = Field(default=1500, ge=100, le=5000)
    chunk_overlap: int = Field(default=225, ge=0, le=1000)


@router.get("/chunking-config", summary="Получить настройки чанкинга")
async def get_chunking_config():
    """Получить текущие настройки чанкинга из Redis"""
    from src.api.services.config_store import config_store
    from src.config import get_settings
    _cfg = get_settings()

    config = config_store.get("chunking", "default", {
        "chunk_size": _cfg.CHUNK_SIZE,
        "chunk_overlap": _cfg.CHUNK_OVERLAP
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
        "url": cfg.get("url", get_settings().OLLAMA_BASE_URL),
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
    url: str = ""
    model: str = "phi4-mini"
    api_key: Opt[str] = None
    provider: str = "ollama"

_ext_llm_config: ExtLLMConfig = ExtLLMConfig(url=get_settings().OLLAMA_BASE_URL)

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

    # Автоматически подтягиваем модели провайдера (для всех типов, не только Ollama).
    # Раньше было только ollama — остальные (deepseek/openai/custom) оставались
    # с пустым списком моделей до ручного «Обновить модели».
    fetched_models = []
    if config.enabled:
        try:
            fetched_models = await provider_service.fetch_provider_models(config.id)
            logger.info(f"Модели провайдера {config.name}: {len(fetched_models)} шт")
        except Exception as e:
            logger.warning(f"Не удалось загрузить модели {config.name}: {e}")

    return {
        "status": "success",
        "message": f"Провайдер {config.name} сохранён",
        "provider": config.to_dict(include_secret=False),
        "models": fetched_models,
        "models_count": len(fetched_models),
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


@router.post("/providers/{provider_id}/balance", summary="Проверить баланс провайдера")
async def check_provider_balance(provider_id: str):
    """Проверить баланс API провайдера (OpenAI, DeepSeek, OpenRouter)."""
    provider = provider_service.get_provider_with_key(provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Провайдер не найден")

    if provider.type == "ollama":
        return {"ok": True, "message": "Локальный сервер — без ограничений", "balance_ok": True, "display": "∞"}

    if not provider.api_key:
        return {"ok": False, "message": "API ключ не указан", "balance_ok": False}

    import httpx
    try:
        base_url = provider.url.rstrip("/")
        headers = {"Authorization": f"Bearer {provider.api_key}"}

        if provider.type == "openrouter":
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{base_url}/api/v1/auth/key", headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    credits = float(data.get("data", {}).get("credits", 0))
                    return {"ok": True, "balance_ok": True, "balance_usd": credits, "display": f"${credits:.2f}", "message": f"Баланс OpenRouter: ${credits:.2f}"}
                return {"ok": False, "balance_ok": False, "message": f"HTTP {resp.status_code}"}

        elif provider.type == "deepseek":
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{base_url}/v1/user/balance", headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    bal = float(data.get("balance", data.get("data", {}).get("balance", 0)))
                    return {"ok": True, "balance_ok": bal > 0, "balance": bal, "display": f"{bal} токенов", "message": f"Баланс DeepSeek: {bal} токенов"}
                return {"ok": False, "balance_ok": False, "message": f"HTTP {resp.status_code}"}

        elif provider.type == "openai":
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{base_url}/v1/models", headers=headers)
                if resp.status_code == 200:
                    return {"ok": True, "balance_ok": True, "display": "✅ Ключ активен", "message": "API ключ работает"}
                return {"ok": False, "balance_ok": False, "message": f"HTTP {resp.status_code}"}

        return {"ok": True, "balance_ok": None, "message": f"Тип {provider.type} — проверка не реализована"}
    except Exception as e:
        return {"ok": False, "balance_ok": False, "message": str(e)}


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
        # Возвращаем пустой шаблон функции, но с дефолтным промптом из prompts/*.txt
        return {
            "function": function_name,
            "provider_id": provider_service.get_default_provider_id() or "",
            "model": "",
            "system_prompt": provider_service._load_default_prompt(function_name),
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

    # Обновляем .env если это embedding модель (чтобы не сбрасывалась при пересоздании контейнеров)
    if req.function == "embedding":
        try:
            env_path = "/app/kag.env"
            if os.path.exists(env_path):
                with open(env_path, "r") as f:
                    env_lines = f.readlines()
                new_lines = []
                for line in env_lines:
                    if line.startswith("EMBEDDING_MODEL="):
                        new_lines.append(f"EMBEDDING_MODEL={req.model}\n")
                    else:
                        new_lines.append(line)
                with open(env_path, "w") as f:
                    f.writelines(new_lines)
                logger.info(f".env EMBEDDING_MODEL обновлён: {req.model}")
        except Exception as e:
            logger.warning(f"Не удалось обновить .env для embedding: {e}")

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
# ═══════════════════════════════════════
# Брендинг (единая настройка для всех страниц)
# ═══════════════════════════════════════

@router.get("/branding-config", summary="Настройки брендинга")
async def get_branding_config():
    try:
        from src.api.services.config_store import config_store
        cfg = config_store.get("system", "branding") or {}
        if not isinstance(cfg, dict):
            cfg = {}
        return {
            "name": cfg.get("name", "KAG"),
            "version": cfg.get("version", ""),
            "footer": cfg.get("footer", ""),
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.post("/branding-config", summary="Сохранить брендинг")
async def save_branding_config(data: dict):
    try:
        from src.api.services.config_store import config_store
        cfg = config_store.get("system", "branding") or {}
        if not isinstance(cfg, dict):
            cfg = {}
        if "name" in data:
            cfg["name"] = str(data["name"]).strip()[:40] or "KAG"
        if "version" in data:
            cfg["version"] = str(data["version"]).strip()[:20]
        if "footer" in data:
            cfg["footer"] = str(data["footer"]).strip()[:120]
        config_store.set("system", "branding", cfg)
        return {"status": "ok", **cfg}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ═══════════════════════════════════════
# Обработка документов (блокировка запуска)
# ═══════════════════════════════════════

@router.get("/processing-config", summary="Статус обработки документов (блокировка)")
async def get_processing_config():
    """Вернуть {blocked: bool, message: str} — блокирует ли админ запуск обработки."""
    try:
        from src.api.services.config_store import config_store
        cfg = config_store.get("system", "processing") or {}
        if not isinstance(cfg, dict):
            cfg = {}
        return {
            "blocked": bool(cfg.get("blocked", False)),
            "message": str(cfg.get("message", "")),
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.post("/processing-config", summary="Сохранить блокировку обработки документов")
async def save_processing_config(data: dict):
    """Админ блокирует/разблокирует запуск обработки (кнопка «Обработать» на странице Документы)."""
    try:
        from src.api.services.config_store import config_store
        cfg = config_store.get("system", "processing") or {}
        if not isinstance(cfg, dict):
            cfg = {}
        if "blocked" in data:
            cfg["blocked"] = bool(data["blocked"])
        if "message" in data:
            cfg["message"] = str(data["message"]).strip()[:200]
        config_store.set("system", "processing", cfg)
        return {"status": "ok", "blocked": bool(cfg.get("blocked", False)),
                "message": str(cfg.get("message", ""))}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ═══════════════════════════════════════
# Настройки поиска (Hybrid Search)
# ═══════════════════════════════════════

@router.get("/search-config", summary="Настройки поиска (Hybrid Search)")
async def get_search_config():
    """Вернуть настройки поиска: sparse_enabled (BM25)."""
    try:
        from src.api.services.config_store import config_store
        cfg = config_store.get("search", "config") or {}
        return {"sparse_enabled": bool(cfg.get("sparse_enabled"))}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.post("/search-config", summary="Сохранить настройки поиска")
async def save_search_config(data: dict):
    """Включить/выключить Hybrid Search (sparse BM25)."""
    try:
        from src.api.services.config_store import config_store
        cfg = config_store.get("search", "config") or {}
        if not isinstance(cfg, dict):
            cfg = {}
        cfg["sparse_enabled"] = bool(data.get("sparse_enabled", False))
        config_store.set("search", "config", cfg)
        return {"status": "ok", "sparse_enabled": cfg["sparse_enabled"]}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ═══════════════════════════════════════
# Настройки Neo4j (граф знаний)
# ═══════════════════════════════════════

@router.get("/neo4j-config", summary="Настройки Neo4j (граф)")
async def get_neo4j_config():
    """Вернуть настройки графа: батч-запись, размер батча, таймаут."""
    try:
        from src.api.services.config_store import config_store
        cfg = config_store.get("neo4j", "config") or {}
        return {
            "batch_enabled": bool(cfg.get("batch_enabled", True)),
            "batch_size": int(cfg.get("batch_size", 100) or 100),
            "timeout": int(cfg.get("timeout", 20) or 20),
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.post("/neo4j-config", summary="Сохранить настройки Neo4j")
async def save_neo4j_config(data: dict):
    try:
        from src.api.services.config_store import config_store
        cfg = config_store.get("neo4j", "config") or {}
        if not isinstance(cfg, dict):
            cfg = {}
        if "batch_enabled" in data:
            cfg["batch_enabled"] = bool(data["batch_enabled"])
        if "batch_size" in data:
            bs = int(data["batch_size"])
            cfg["batch_size"] = max(1, min(500, bs))
        if "timeout" in data:
            t = int(data["timeout"])
            cfg["timeout"] = max(5, min(120, t))
        config_store.set("neo4j", "config", cfg)
        return {"status": "ok", **cfg}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ═══════════════════════════════════════
# Типы документов (авто-пополняемый список)
# ═══════════════════════════════════════

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

        def _keys(ts):
            """Ключи типов: поддерживаем строки и объекты {key, label}."""
            keys = []
            for t in ts:
                if isinstance(t, dict):
                    keys.append((t.get("key") or "").lower())
                else:
                    keys.append(str(t).lower())
            return keys

        if action == "add":
            if name not in _keys(types):
                types.append({"key": name, "label": name})
            else:
                return {"status": "ok", "message": "Без изменений"}
        elif action == "remove":
            before = len(types)
            types = [
                t for t in types
                if not (isinstance(t, dict) and (t.get("key") or "").lower() == name)
                and str(t).lower() != name
            ]
            if len(types) == before:
                return {"status": "ok", "message": "Без изменений"}
        else:
            return {"status": "ok", "message": "Без изменений"}

        config_store.set("kg_config", "doc_types", {"types": types})
        return {"status": "ok", "message": f"Тип '{name}' {'добавлен' if action == 'add' else 'удалён'}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# Theme API
from pydantic import BaseModel

class ThemeRequest(BaseModel):
    theme: str  # "light" or "dark"

@router.get("/theme", summary="Получить тему пользователя")
async def get_theme():
    """Возвращает сохранённую тему (light/dark). По умолчанию light."""
    try:
        from src.api.services.config_store import config_store
        theme = config_store.get("ui", "theme") or {"value": "light"}
        return theme
    except Exception:
        return {"value": "light"}

@router.post("/theme", summary="Сохранить тему пользователя")
async def save_theme(body: ThemeRequest):
    """Сохраняет выбранную тему в PostgreSQL."""
    from src.api.services.config_store import config_store
    config_store.set("ui", "theme", {"value": body.theme})
    return {"status": "ok", "theme": body.theme}

# OCR Settings API
class OcrSettingsRequest(BaseModel):
    force_ocr: bool = False
    dpi: int = 200
    enable_summarization: bool = False
    # Модель распознавания таблиц: pymupdf (сейчас) | docling | granite (будущее)
    # pymupdf — быстрый, встроенный find_tables; docling — TableFormer (нужен
    # рабочий layout); granite — Granite Vision на кластере (таблицы → HTML/JSON)
    table_model: str = "pymupdf"

@router.get("/ocr-settings", summary="Получить настройки OCR")
async def get_ocr_settings():
    from src.api.services.config_store import config_store
    cfg = config_store.get("ocr", "settings") or {"force_ocr": False, "dpi": 200}
    cfg.setdefault("table_model", "pymupdf")
    return cfg

@router.post("/ocr-settings", summary="Сохранить настройки OCR")
async def save_ocr_settings(body: OcrSettingsRequest):
    from src.api.services.config_store import config_store
    config_store.set("ocr", "settings", {
        "force_ocr": body.force_ocr, "dpi": body.dpi,
        "enable_summarization": body.enable_summarization,
        "table_model": body.table_model or "pymupdf",
    })
    return {"status": "ok"}
# Backup endpoint
from fastapi.responses import JSONResponse
from datetime import datetime

BACKUP_NAMESPACES = [
    "web_monitor", "ocr", "chunking", "embedding", "function_map",
    "llm_config", "ext_llm", "kg_config", "ui", "documents",
    "process_logs", "llm", "upload_formats", "hot_folder",
    "ssh_config", "model_manager",
]

@router.get("/backup", summary="Backup всех настроек системы")
async def get_backup():
    from src.api.services.config_store import config_store
    backup = {"created_at": datetime.utcnow().isoformat(), "version": "1.0", "data": {}}
    for ns in BACKUP_NAMESPACES:
        try:
            data = config_store.get_all(ns)
            if data:
                backup["data"][ns] = data
        except Exception as e:
            backup["data"][ns] = {"error": str(e)}
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    headers = {"Content-Disposition": f"attachment; filename=kag-backup-{ts}.json"}
    return JSONResponse(content=backup, headers=headers)
import json, traceback
from fastapi import UploadFile, File
from src.api.services.config_store import config_store

@router.post("/backup-restore", summary="Восстановить настройки из backup JSON")
async def restore_backup(file: UploadFile = File(...)):
    """Загружает backup JSON и восстанавливает все namespace-ы в config_store."""
    try:
        content = await file.read()
        data = json.loads(content)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")
    
    if "data" not in data:
        raise HTTPException(status_code=400, detail="Not a valid backup file (missing 'data' key)")
    
    restored = 0
    errors = []
    for ns, ns_data in data["data"].items():
        if not isinstance(ns_data, dict):
            continue
        try:
            for key, value in ns_data.items():
                config_store.set(ns, key, value)
            restored += 1
        except Exception as e:
            errors.append(f"{ns}: {e}")
    
    return {"status": "ok", "restored": restored, "errors": errors}


# ═══════════════════════════════════════
# Внешний адрес системы — настройка Keycloak после развёртывания
# ═══════════════════════════════════════
# Проект разворачивают в разных сетях за reverse proxy — внешний адрес
# неизвестен при деплое. Админ вводит его после развёртывания, система
# переключает Keycloak в production mode (KC_HOSTNAME + start).

@router.get("/system-config", summary="Внешний адрес и статус Keycloak")
async def get_system_config():
    """Вернуть внешний адрес (config_store) + статус Keycloak + SSO."""
    from src.api.services.config_store import config_store
    settings = get_settings()
    cfg = config_store.get("system", "config", {})
    if not isinstance(cfg, dict):
        cfg = {}

    base_url = cfg.get("base_url", "")
    # SSO: config_store (переключается кнопками в админке) или env AUTH_ENABLED
    sso_enabled = cfg.get("sso_enabled", settings.AUTH_ENABLED)

    kc_status = {"mode": "unknown", "hostname": None, "container_running": False}
    try:
        import docker
        client = docker.from_env()
        kc = client.containers.get("kag-keycloak")
        kc_status["container_running"] = kc.status == "running"
        env = kc.attrs.get("Config", {}).get("Env", [])
        for e in env:
            if e.startswith("KC_HOSTNAME="):
                kc_status["hostname"] = e.split("=", 1)[1]
        cmd = kc.attrs.get("Config", {}).get("Cmd", [])
        kc_status["mode"] = "dev" if any("start-dev" in str(c) for c in cmd) else "production"
    except Exception:
        pass

    return {"base_url": base_url, "sso_enabled": bool(sso_enabled), "keycloak": kc_status}


class SystemConfigRequest(BaseModel):
    base_url: Optional[str] = None
    sso_enabled: Optional[bool] = None


@router.put("/system-config", summary="Сохранить внешний адрес и флаг SSO")
async def save_system_config(req: SystemConfigRequest):
    """Сохраняет внешний адрес и флаг SSO в config_store.

    ПРИМЕНЕНИЕ Keycloak в prod mode (KC_HOSTNAME, command start) — только
    через деплой (.env / docker-compose.yml), НЕ через API. На живом
    сервере compose-файл и контейнер keycloak не трогаются.
    """
    from src.api.services.config_store import config_store
    cfg = config_store.get("system", "config", {})
    if not isinstance(cfg, dict):
        cfg = {}

    base_url = cfg.get("base_url", "")
    if req.base_url is not None:
        base_url = (req.base_url or "").strip().rstrip("/")
        cfg["base_url"] = base_url

    sso_changed = False
    if req.sso_enabled is not None:
        cfg["sso_enabled"] = bool(req.sso_enabled)
        sso_changed = True

    config_store.set("system", "config", cfg)

    if sso_changed and req.base_url is None:
        return {"status": "ok", "sso_enabled": bool(cfg["sso_enabled"]),
                "message": "SSO " + ("включён" if cfg["sso_enabled"] else "выключен")}

    if not base_url:
        return {"status": "ok", "base_url": "", "sso_enabled": bool(cfg.get("sso_enabled", False)),
                "message": "Внешний адрес сброшен"}

    # Адрес только сохраняем. Prod mode Keycloak включается при деплое:
    # KC_HOSTNAME=<base_url> и command: start --import-realm в compose/.env.
    return {
        "status": "ok", "base_url": base_url,
        "sso_enabled": bool(cfg.get("sso_enabled", False)),
        "message": "Внешний адрес сохранён. Keycloak переключится в prod mode "
                   "при деплое (KC_HOSTNAME=" + base_url + ", command: start --import-realm).",
    }


# ═══════════════════════════════════════
# Масштабирование — настройки для роста (500+ пользователей)
# ═══════════════════════════════════════
# Зачем: храним целевые параметры масштабирования в config_store.
# Они НЕ применяются на лету — это ЗАГОТОВКА для деплоя: админ задаёт
# значения, devops применяет при следующем развёртывании
# (docker-compose scale worker=N, память Neo4j и т.д.).

SCALING_DEFAULTS = {
    "worker_replicas": 1,      # сколько экземпляров worker
    "worker_cpus": "4.0",      # CPU на worker
    "worker_memory": "12G",    # память на worker
    "neo4j_heap": "512M",      # Neo4j JVM heap
    "neo4j_pagecache": "512M", # Neo4j pagecache
    "api_workers": 1,          # uvicorn workers для api
    "notes": "",
}


@router.get("/scaling", summary="Текущее состояние и настройки масштабирования")
async def get_scaling():
    """Вернуть: текущее состояние (запущенные worker'ы, память Neo4j)
    + сохранённые целевые настройки масштабирования из config_store."""
    from src.api.services.config_store import config_store
    cfg = config_store.get("scaling", "config", {})
    if not isinstance(cfg, dict):
        cfg = {}
    cfg = {**SCALING_DEFAULTS, **cfg}

    current = {"worker_count": 0, "neo4j_heap": None, "neo4j_pagecache": None}
    try:
        import docker
        client = docker.from_env()
        workers = [c for c in client.containers.list() if "worker" in c.name]
        current["worker_count"] = len(workers)
        # Память Neo4j из env контейнера
        try:
            neo = client.containers.get("kag-neo4j")
            env = neo.attrs.get("Config", {}).get("Env", [])
            for e in env:
                if e.startswith("NEO4J_dbms_memory_heap_max"):
                    current["neo4j_heap"] = e.split("=", 1)[1]
                if e.startswith("NEO4J_dbms_memory_pagecache"):
                    current["neo4j_pagecache"] = e.split("=", 1)[1]
        except Exception:
            pass
    except Exception:
        pass

    return {"current": current, "config": cfg}


class ScalingConfigRequest(BaseModel):
    worker_replicas: Optional[int] = None
    worker_cpus: Optional[str] = None
    worker_memory: Optional[str] = None
    neo4j_heap: Optional[str] = None
    neo4j_pagecache: Optional[str] = None
    api_workers: Optional[int] = None
    notes: Optional[str] = None


@router.put("/scaling", summary="Сохранить целевые настройки масштабирования")
async def save_scaling(req: ScalingConfigRequest):
    """Сохранить настройки масштабирования в config_store.

    ВАЖНО: только сохраняет ЦЕЛЕВЫЕ значения — НЕ применяет. Применяются
    при следующем деплое (см. комментарии в docker-compose.yml).
    """
    from src.api.services.config_store import config_store
    cfg = config_store.get("scaling", "config", {})
    if not isinstance(cfg, dict):
        cfg = {}
    cfg = {**SCALING_DEFAULTS, **cfg}

    data = req.model_dump(exclude_none=True)
    cfg.update(data)

    # Валидация
    if cfg.get("worker_replicas") is not None:
        cfg["worker_replicas"] = max(1, min(int(cfg["worker_replicas"]), 16))
    if cfg.get("api_workers") is not None:
        cfg["api_workers"] = max(1, min(int(cfg["api_workers"]), 16))

    config_store.set("scaling", "config", cfg)
    return {"status": "ok", "config": cfg,
            "message": "Настройки сохранены (применятся при следующем деплое)"}


# ═══════════════════════════════════════
# Worker Resources — настройка CPU/памяти
# ═══════════════════════════════════════

def _find_worker_container(client):
    """Найти контейнер worker.

    После `docker compose up -d --scale worker=N` контейнер называется
    kag-system_worker_1/2 (container_name убран), а не kag-worker — ищем по
    compose-метке, с fallback на старое имя.
    """
    try:
        workers = client.containers.list(
            all=True, filters={"label": "com.docker.compose.service=worker"}
        )
        if workers:
            return workers[0]
    except Exception:
        pass
    return client.containers.get("kag-worker")


@router.get("/worker-resources", summary="Текущие ресурсы worker")
async def get_worker_resources():
    """Читает текущие cpus/memory из docker-compose.yml для worker."""
    import docker
    try:
        client = docker.from_env()
        w = _find_worker_container(client)
        host_cfg = w.attrs["HostConfig"]
        return {
            "cpus": str(host_cfg.get("NanoCpus", 0) / 1e9) if host_cfg.get("NanoCpus") else "2.0",
            "memory": str(host_cfg.get("Memory", 0) / (1024**3)) + "G" if host_cfg.get("Memory") else "4G",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/worker-resources", summary="Задать целевые ресурсы worker")
async def update_worker_resources(req: dict):
    """Сохраняет целевые cpus/memory worker в config_store.

    ПРИМЕНЕНИЕ — только через деплой (docker-compose.yml, env
    WORKER_CPUS/WORKER_MEMORY). На живом сервере docker-compose.yml
    и контейнеры не трогаются — это правило проекта (правка compose
    через API ранее убивала сервер).
    """
    cpus = str(req.get("cpus", "4.0")).strip()
    memory = str(req.get("memory", "8G")).strip().upper()

    # ── Валидация ──
    try:
        cpus_f = float(cpus)
        if not (0.5 <= cpus_f <= 32):
            return {"status": "error", "message": "CPU должен быть в диапазоне 0.5–32"}
    except ValueError:
        return {"status": "error", "message": "CPU — число (напр. 4.0)"}
    import re as _re
    mem_match = _re.fullmatch(r"(\d+(?:\.\d+)?)([MG])", memory)
    if not mem_match:
        return {"status": "error", "message": "Память — число с суффиксом M или G (напр. 12G)"}
    mem_val = float(mem_match.group(1))
    if mem_val < 1:
        return {"status": "error", "message": "Память не может быть меньше 1 (G)"}

    # Сохраняем целевые значения (применятся при следующем deploy).
    try:
        from src.api.services.config_store import config_store
        config_store.set("worker", "resources", {"cpus": cpus, "memory": memory})
    except Exception as e:
        return {"status": "error", "message": f"Не удалось сохранить: {e}"}

    return {
        "status": "ok", "cpus": cpus, "memory": memory,
        "message": f"Сохранено (worker: {cpus} CPU / {memory}). "
                   f"Применится при deploy: docker-compose up -d --no-deps --force-recreate worker "
                   f"(WORKER_CPUS={cpus} WORKER_MEMORY={memory}).",
    }


# ===========================================
# Словарь алиасов сущностей (entity resolution)
# ===========================================

class AliasPairRequest(BaseModel):
    """Запрос на добавление/обновление пары алиасов."""
    canonical_name: str = Field(..., min_length=1, max_length=500)
    alias: str = Field(..., min_length=1, max_length=500)
    entity_type: str = Field(default="organization", max_length=50)
    domain: str = Field(default="universal", max_length=50)
    comment: str = Field(default="")
    source: str = Field(default="manual", max_length=50)


@router.get("/aliases", summary="Список пар алиасов")
async def list_aliases(include_pending: bool = False, verdict: str = ""):
    """Список пар из словаря entity_aliases.

    include_pending=true — показать и непросмотренные (сомнительные) пары.
    verdict=approved/rejected — фильтр по вердикту админа.
    """
    try:
        from src.indexing.knowledge_graph import kg_service
        pairs = kg_service.list_alias_pairs(include_pending=include_pending, verdict=verdict)
        # Если просим pending — отдельно показываем счётчик непросмотренных
        pending_count = len([
            p for p in kg_service.list_alias_pairs(include_pending=True)
            if not p.get("reviewed")
        ])
        return {
            "status": "ok",
            "pairs": pairs,
            "total": len(pairs),
            "pending_count": pending_count,
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.post("/aliases", summary="Добавить пару алиасов")
async def add_alias(request: AliasPairRequest):
    """Добавить пару (alias → canonical) в словарь."""
    try:
        from src.indexing.knowledge_graph import kg_service
        ok = kg_service.save_alias_pair(
            canonical=request.canonical_name.strip(),
            alias=request.alias.strip(),
            entity_type=request.entity_type,
            domain=request.domain or "universal",
            source=request.source or "manual",
            comment=request.comment,
            reviewed=True,      # добавлено админом — сразу применяемое
            verdict="approved",
        )
        if ok:
            return {"status": "ok", "message": "Пара добавлена"}
        return {"status": "error", "message": "Не удалось добавить (возможно, уже есть)"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.put("/aliases/{pair_id}", summary="Обновить пару алиасов")
async def update_alias(pair_id: str, request: AliasPairRequest):
    """Обновить содержимое пары (alias/canonical/type/domain/comment).

    reviewed/verdict не трогаем — пара остаётся в модерации до подтверждения.
    """
    try:
        from src.indexing.knowledge_graph import kg_service
        ok = kg_service.update_alias_pair(
            pair_id,
            alias=request.alias,
            canonical=request.canonical_name,
            entity_type=request.entity_type,
            domain=request.domain or "universal",
            comment=request.comment,
        )
        if ok:
            return {"status": "ok", "message": "Пара обновлена"}
        return {"status": "error", "message": "Пара не найдена"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.post("/aliases/apply", summary="Применить словарь к графу")
async def apply_aliases():
    """Применить все approved-пары к графу Neo4j (слить алиасы в канонические узлы)."""
    try:
        from src.indexing.knowledge_graph import kg_service
        res = kg_service.apply_alias_pairs()
        return {"status": "ok", **res, "message": f"Применено пар: {res.get('applied', 0)}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.post("/aliases/{pair_id}/review", summary="Решение админа по сомнительной паре")
async def review_alias(pair_id: str, verdict: str = Query("approved", pattern="^(approved|rejected)$")):
    """Подтвердить или отклонить сомнительную пару.

    approved — пара попадёт в применяемый словарь;
    rejected — отклонена, не будет применяться.
    """
    try:
        from src.indexing.knowledge_graph import kg_service
        ok = kg_service.review_alias_pair(pair_id, verdict)
        if not ok:
            return {"status": "error", "message": "Пара не найдена"}
        # Если approved — сразу применяем к графу
        if verdict == "approved":
            kg_service.apply_alias_pairs()
        return {"status": "ok", "message": f"Пара {'подтверждена' if verdict == 'approved' else 'отклонена'}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.delete("/aliases/{pair_id}", summary="Удалить пару алиасов")
async def delete_alias(pair_id: str):
    """Удалить пару из словаря."""
    try:
        from src.indexing.knowledge_graph import kg_service
        ok = kg_service.delete_alias_pair(pair_id)
        if not ok:
            return {"status": "error", "message": "Пара не найдена"}
        return {"status": "ok", "message": "Пара удалена"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ═══════════════════════════════════════
# Бэкап документов (ZIP: файлы + метаданные)
# ═══════════════════════════════════════

@router.get("/backup-documents", summary="Скачать все документы (ZIP: файлы + метаданные)")
async def backup_documents():
    """Собрать все документы (файлы из uploads + documents_meta.json) в ZIP.

    Файлы кладутся в подпапку documents/, метаданные — documents_meta.json
    (id → все колонки из DocumentRepository). ZIP формируется во временном
    файле (не в памяти) и удаляется после отправки.
    """
    import json, os, tempfile, zipfile
    from pathlib import Path
    from starlette.responses import FileResponse
    from starlette.background import BackgroundTask

    from src.api.services.document_repository import get_doc_repo
    from src.api.services.document_service import document_service

    docs = get_doc_repo().get_all() or {}
    upload_dir = Path(document_service._upload_dir)

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    tmp_path = tmp.name
    tmp.close()

    files_added = 0
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for doc_id, meta in docs.items():
                filename = meta.get("filename") or doc_id
                fname = f"{doc_id}_{filename}"
                path = upload_dir / fname
                if path.exists():
                    zf.write(path, arcname=f"documents/{fname}")
                    files_added += 1
            zf.writestr(
                "documents_meta.json",
                json.dumps(docs, ensure_ascii=False, indent=1, default=str),
            )

            # ── Словарь алиасов (entity_aliases) ────────────────────────────
            try:
                from src.indexing.knowledge_graph import kg_service
                aliases = kg_service.list_alias_pairs(include_pending=True)
                zf.writestr("aliases.json", json.dumps(aliases, ensure_ascii=False, indent=1, default=str))
            except Exception as e:
                zf.writestr("aliases.json", json.dumps({"error": str(e)}, ensure_ascii=False))

            # ── Настройки (config_store: все категории из system_configs) ──
            try:
                from src.api.services.config_store import config_store
                from src.database.session import get_session_local
                from sqlalchemy import text as _text
                categories = []
                try:
                    _maker = get_session_local()
                    _s = _maker()
                    try:
                        rows = _s.execute(_text("SELECT DISTINCT category FROM system_configs")).fetchall()
                        categories = [r[0] for r in rows if r[0]]
                    finally:
                        _s.close()
                except Exception:
                    categories = []
                cfg_all = {}
                for cat in categories:
                    cfg_all[cat] = config_store.get_all(cat)
                zf.writestr("config_store.json", json.dumps(cfg_all, ensure_ascii=False, indent=1, default=str))
            except Exception as e:
                zf.writestr("config_store.json", json.dumps({"error": str(e)}, ensure_ascii=False))

            # ── Чат-истории (chat_sessions + chat_messages) ────────────────
            try:
                from src.database.chat_models import ChatSession, ChatMessage
                chat = {"sessions": [], "messages": []}
                _maker = get_session_local()
                _s = _maker()
                try:
                    for _r in _s.query(ChatSession).all():
                        chat["sessions"].append(
                            {c.name: getattr(_r, c.name) for c in ChatSession.__table__.columns}
                        )
                    for _r in _s.query(ChatMessage).all():
                        chat["messages"].append(
                            {c.name: getattr(_r, c.name) for c in ChatMessage.__table__.columns}
                        )
                finally:
                    _s.close()
                zf.writestr("chat_history.json", json.dumps(chat, ensure_ascii=False, indent=1, default=str))
            except Exception as e:
                zf.writestr("chat_history.json", json.dumps({"error": str(e)}, ensure_ascii=False))
    except Exception as e:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        return {"status": "error", "message": f"Ошибка формирования ZIP: {e}"}

    logger.info(f"[backup] ZIP готов: {len(docs)} документов, файлов: {files_added}, {os.path.getsize(tmp_path)} байт")
    return FileResponse(
        tmp_path,
        media_type="application/zip",
        filename="kag_documents_backup.zip",
        background=BackgroundTask(lambda: _safe_unlink(tmp_path)),
    )


def _safe_unlink(path: str):
    try:
        os.unlink(path)
    except Exception:
        pass
