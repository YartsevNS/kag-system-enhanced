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
from fastapi import APIRouter, HTTPException, Body
from fastapi.responses import HTMLResponse
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


class SwitchEmbeddingRequest(BaseModel):
    """Запрос на переключение embedding модели"""
    model_name: str = Field(..., description="Название embedding модели")


class PullModelRequest(BaseModel):
    """Запрос на загрузку модели"""
    model_name: str = Field(..., description="Название модели для загрузки")


# ===========================================
# HTML админ-панель
# ===========================================

@router.get("/admin", response_class=HTMLResponse, summary="Админ-панель управления моделями")
async def models_admin_page():
    """
    HTML страница для управления моделями.

    Предоставляет веб-интерфейс для:
    - Просмотра всех моделей
    - Переключения между бэкендами
    - Выбора активной модели
    """
    return """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>KAG - Управление моделями</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #0f172a;
            color: #e2e8f0;
            min-height: 100vh;
            padding: 2rem;
        }
        .container { max-width: 1400px; margin: 0 auto; }
        h1 {
            font-size: 2rem;
            margin-bottom: 0.5rem;
            background: linear-gradient(135deg, #3b82f6, #8b5cf6);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        .subtitle { color: #94a3b8; margin-bottom: 2rem; }
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(400px, 1fr));
            gap: 1.5rem;
            margin-bottom: 2rem;
        }
        .card {
            background: #1e293b;
            border-radius: 12px;
            padding: 1.5rem;
            border: 1px solid #334155;
        }
        .card h2 {
            font-size: 1.25rem;
            margin-bottom: 1rem;
            color: #f8fafc;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }
        .status-indicator {
            width: 12px;
            height: 12px;
            border-radius: 50%;
            display: inline-block;
        }
        .status-healthy { background: #22c55e; }
        .status-error { background: #ef4444; }
        .status-warning { background: #f59e0b; }
        .status-badge {
            display: inline-block;
            padding: 0.25rem 0.75rem;
            border-radius: 9999px;
            font-size: 0.75rem;
            font-weight: 500;
        }
        .badge-success { background: #064e3b; color: #6ee7b7; }
        .badge-error { background: #7f1d1d; color: #fca5a5; }
        table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 1rem;
        }
        th, td {
            padding: 0.75rem;
            text-align: left;
            border-bottom: 1px solid #334155;
        }
        th { color: #94a3b8; font-weight: 500; font-size: 0.875rem; }
        td { font-size: 0.875rem; }
        .btn {
            padding: 0.5rem 1rem;
            border-radius: 6px;
            border: none;
            cursor: pointer;
            font-size: 0.875rem;
            font-weight: 500;
            transition: all 0.2s;
        }
        .btn-primary {
            background: #3b82f6;
            color: white;
        }
        .btn-primary:hover { background: #2563eb; }
        .btn-secondary {
            background: #475569;
            color: white;
        }
        .btn-secondary:hover { background: #374151; }
        .btn-sm {
            padding: 0.375rem 0.75rem;
            font-size: 0.75rem;
        }
        .btn-active {
            background: #22c55e;
            color: white;
        }
        select, input {
            background: #0f172a;
            border: 1px solid #475569;
            color: #e2e8f0;
            padding: 0.5rem;
            border-radius: 6px;
            font-size: 0.875rem;
            width: 100%;
        }
        .form-group { margin-bottom: 1rem; }
        .form-group label {
            display: block;
            margin-bottom: 0.5rem;
            color: #94a3b8;
            font-size: 0.875rem;
        }
        .info-grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 1rem;
        }
        .info-item {
            background: #0f172a;
            padding: 1rem;
            border-radius: 8px;
        }
        .info-item .label {
            color: #94a3b8;
            font-size: 0.75rem;
            margin-bottom: 0.25rem;
        }
        .info-item .value {
            color: #f8fafc;
            font-size: 1.125rem;
            font-weight: 600;
        }
        .loading {
            text-align: center;
            padding: 2rem;
            color: #94a3b8;
        }
        .refresh-btn {
            position: fixed;
            bottom: 2rem;
            right: 2rem;
            width: 56px;
            height: 56px;
            border-radius: 50%;
            background: #3b82f6;
            color: white;
            border: none;
            cursor: pointer;
            font-size: 1.5rem;
            box-shadow: 0 4px 12px rgba(59, 130, 246, 0.4);
            transition: all 0.2s;
        }
        .refresh-btn:hover {
            transform: scale(1.1);
            background: #2563eb;
        }
        #notification {
            position: fixed;
            top: 2rem;
            right: 2rem;
            padding: 1rem 1.5rem;
            border-radius: 8px;
            background: #22c55e;
            color: white;
            display: none;
            z-index: 1000;
            animation: slideIn 0.3s ease;
        }
        @keyframes slideIn {
            from { transform: translateX(100%); opacity: 0; }
            to { transform: translateX(0); opacity: 1; }
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>🎛️ Управление моделями KAG</h1>
        <p class="subtitle">Админ-панель для конфигурации LLM и Embedding моделей</p>

        <div id="notification"></div>

        <!-- Статус системы -->
        <div class="card" id="status-card">
            <h2>📊 Статус системы</h2>
            <div class="info-grid" id="system-status">
                <div class="loading">Загрузка...</div>
            </div>
        </div>

        <div class="grid">
            <!-- LLM Модели -->
            <div class="card">
                <h2>🤖 LLM Модели</h2>
                <div id="llm-models">
                    <div class="loading">Загрузка...</div>
                </div>
            </div>

            <!-- Embedding Модели -->
            <div class="card">
                <h2>📐 Embedding Модели</h2>
                <div id="embedding-models">
                    <div class="loading">Загрузка...</div>
                </div>
            </div>

            <!-- Все модели Ollama -->
            <div class="card">
                <h2>📦 Все модели Ollama</h2>
                <div id="all-ollama-models">
                    <div class="loading">Загрузка...</div>
                </div>
            </div>

            <!-- Загрузить модель -->
            <div class="card">
                <h2>⬇️ Загрузить новую модель</h2>
                <div class="form-group">
                    <label>Название модели (например, llama2:7b, mistral:latest)</label>
                    <input type="text" id="model-name-input" placeholder="Введите название модели...">
                </div>
                <button class="btn btn-primary" onclick="pullModel()">Загрузить модель</button>
            </div>
        </div>
    </div>

    <button class="refresh-btn" onclick="loadAll()" title="Обновить">↻</button>

    <script>
        const API_BASE = '/api/v1/admin/models';

        async function loadStatus() {
            try {
                const response = await fetch(`${API_BASE}/status`);
                const data = await response.json();
                
                const statusHtml = `
                    <div class="info-item">
                        <div class="label">Активный бэкенд</div>
                        <div class="value">${data.active_config.llm_backend || 'N/A'}</div>
                    </div>
                    <div class="info-item">
                        <div class="label">LLM модель</div>
                        <div class="value">${data.active_config.llm_model || 'N/A'}</div>
                    </div>
                    <div class="info-item">
                        <div class="label">Embedding модель</div>
                        <div class="value">${data.active_config.embedding_model || 'N/A'}</div>
                    </div>
                    <div class="info-item">
                        <div class="label">Размерность embedding</div>
                        <div class="value">${data.embedding.dimensions}</div>
                    </div>
                `;
                document.getElementById('system-status').innerHTML = statusHtml;

                // Обновляем статусы бэкендов
                updateBackendStatus(data.llm_backends);
            } catch (error) {
                console.error('Ошибка загрузки статуса:', error);
            }
        }

        function updateBackendStatus(backends) {
            // Добавляем индикаторы к заголовкам карточек
        }

        async function loadLlmModels() {
            try {
                const response = await fetch(`${API_BASE}/llm`);
                const models = await response.json();
                
                if (models.length === 0) {
                    document.getElementById('llm-models').innerHTML = 
                        '<div class="loading">Нет доступных моделей</div>';
                    return;
                }

                const tableHtml = `
                    <table>
                        <thead>
                            <tr>
                                <th>Модель</th>
                                <th>Бэкенд</th>
                                <th>Статус</th>
                                <th>Действие</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${models.map(m => `
                                <tr>
                                    <td><strong>${m.name}</strong></td>
                                    <td>${m.backend}</td>
                                    <td>
                                        <span class="status-badge ${m.is_active ? 'badge-success' : 'badge-error'}">
                                            ${m.is_active ? 'Активна' : 'Неактивна'}
                                        </span>
                                    </td>
                                    <td>
                                        ${!m.is_active ? `
                                            <button class="btn btn-secondary btn-sm" 
                                                onclick="switchLlmModel('${m.backend}', '${m.name}')">
                                                Активировать
                                            </button>
                                        ` : '<span class="btn-active btn btn-sm">✓ Активна</span>'}
                                    </td>
                                </tr>
                            `).join('')}
                        </tbody>
                    </table>
                `;
                
                document.getElementById('llm-models').innerHTML = tableHtml;
            } catch (error) {
                console.error('Ошибка загрузки LLM моделей:', error);
            }
        }

        async function loadEmbeddingModels() {
            try {
                const response = await fetch(`${API_BASE}/embeddings`);
                const models = await response.json();
                
                if (models.length === 0) {
                    document.getElementById('embedding-models').innerHTML = 
                        '<div class="loading">Нет доступных embedding моделей</div>';
                    return;
                }

                const tableHtml = `
                    <table>
                        <thead>
                            <tr>
                                <th>Модель</th>
                                <th>Статус</th>
                                <th>Действие</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${models.map(m => `
                                <tr>
                                    <td><strong>${m.name}</strong></td>
                                    <td>
                                        <span class="status-badge ${m.is_active ? 'badge-success' : 'badge-error'}">
                                            ${m.is_active ? 'Активна' : 'Неактивна'}
                                        </span>
                                    </td>
                                    <td>
                                        ${!m.is_active ? `
                                            <button class="btn btn-secondary btn-sm" 
                                                onclick="switchEmbeddingModel('${m.name}')">
                                                Активировать
                                            </button>
                                        ` : '<span class="btn-active btn btn-sm">✓ Активна</span>'}
                                    </td>
                                </tr>
                            `).join('')}
                        </tbody>
                    </table>
                `;
                
                document.getElementById('embedding-models').innerHTML = tableHtml;
            } catch (error) {
                console.error('Ошибка загрузки embedding моделей:', error);
            }
        }

        async function loadAllOllamaModels() {
            try {
                const response = await fetch(`${API_BASE}/ollama-models`);
                const models = await response.json();
                
                if (models.length === 0) {
                    document.getElementById('all-ollama-models').innerHTML = 
                        '<div class="loading">Нет моделей</div>';
                    return;
                }

                const tableHtml = `
                    <table>
                        <thead>
                            <tr>
                                <th>Модель</th>
                                <th>Размер</th>
                                <th>Изменена</th>
                                <th>Действие</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${models.map(m => `
                                <tr>
                                    <td><strong>${m.name}</strong></td>
                                    <td>${formatBytes(m.size)}</td>
                                    <td>${new Date(m.modified_at).toLocaleDateString('ru-RU')}</td>
                                    <td>
                                        <button class="btn btn-secondary btn-sm" 
                                            onclick="deleteModel('${m.name}')" 
                                            style="background: #ef4444;">
                                            Удалить
                                        </button>
                                    </td>
                                </tr>
                            `).join('')}
                        </tbody>
                    </table>
                `;
                
                document.getElementById('all-ollama-models').innerHTML = tableHtml;
            } catch (error) {
                console.error('Ошибка загрузки всех моделей:', error);
            }
        }

        function formatBytes(bytes) {
            if (bytes === 0) return '0 B';
            const k = 1024;
            const sizes = ['B', 'KB', 'MB', 'GB'];
            const i = Math.floor(Math.log(bytes) / Math.log(k));
            return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
        }

        async function switchLlmModel(backend, modelName) {
            try {
                showNotification(`Переключение на ${modelName}...`);
                
                const response = await fetch(`${API_BASE}/switch-llm`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        backend_type: backend,
                        model_name: modelName
                    })
                });

                if (response.ok) {
                    showNotification(`✓ Модель переключена на ${modelName}`);
                    setTimeout(loadAll, 500);
                } else {
                    const error = await response.json();
                    showNotification(`✗ Ошибка: ${error.detail}`, true);
                }
            } catch (error) {
                showNotification(`✗ Ошибка: ${error.message}`, true);
            }
        }

        async function switchEmbeddingModel(modelName) {
            try {
                showNotification(`Переключение embedding на ${modelName}...`);
                
                const response = await fetch(`${API_BASE}/switch-embedding`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ model_name: modelName })
                });

                if (response.ok) {
                    showNotification(`✓ Embedding модель переключена на ${modelName}`);
                    setTimeout(loadAll, 500);
                } else {
                    const error = await response.json();
                    showNotification(`✗ Ошибка: ${error.detail}`, true);
                }
            } catch (error) {
                showNotification(`✗ Ошибка: ${error.message}`, true);
            }
        }

        async function pullModel() {
            const modelName = document.getElementById('model-name-input').value.trim();
            if (!modelName) {
                showNotification('Введите название модели', true);
                return;
            }

            try {
                showNotification(`Загрузка ${modelName}...`);
                
                const response = await fetch(`${API_BASE}/pull`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ model_name: modelName })
                });

                const result = await response.json();
                
                if (result.status === 'success') {
                    showNotification(`✓ Модель ${modelName} загружена`);
                    document.getElementById('model-name-input').value = '';
                    setTimeout(loadAll, 500);
                } else {
                    showNotification(`⚠ ${result.message || result.error}`, true);
                }
            } catch (error) {
                showNotification(`✗ Ошибка: ${error.message}`, true);
            }
        }

        async function deleteModel(modelName) {
            if (!confirm(`Удалить модель ${modelName}?`)) return;

            try {
                showNotification(`Удаление ${modelName}...`);
                
                const response = await fetch(`${API_BASE}/delete/${modelName}`, {
                    method: 'DELETE'
                });

                if (response.ok) {
                    showNotification(`✓ Модель ${modelName} удалена`);
                    setTimeout(loadAll, 500);
                } else {
                    showNotification('✗ Ошибка удаления', true);
                }
            } catch (error) {
                showNotification(`✗ Ошибка: ${error.message}`, true);
            }
        }

        function showNotification(message, isError = false) {
            const notification = document.getElementById('notification');
            notification.textContent = message;
            notification.style.background = isError ? '#ef4444' : '#22c55e';
            notification.style.display = 'block';
            
            setTimeout(() => {
                notification.style.display = 'none';
            }, 3000);
        }

        async function loadAll() {
            await Promise.all([
                loadStatus(),
                loadLlmModels(),
                loadEmbeddingModels(),
                loadAllOllamaModels()
            ]);
        }

        // Загрузка при старте
        loadAll();
        
        // Автообновление каждые 30 секунд
        setInterval(loadStatus, 30000);
    </script>
</body>
</html>
"""


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


@router.post("/switch-llm", summary="Переключить активную LLM модель")
async def switch_llm_model(request: SwitchModelRequest):
    """
    Переключить активную LLM модель.

    - **backend_type**: Тип бэкенда (ollama, vllm, openai)
    - **model_name**: Название модели
    """
    try:
        success = await model_manager.switch_llm_model(
            request.backend_type,
            request.model_name
        )
        
        if success:
            return {
                "status": "success",
                "message": f"Модель переключена на {request.model_name}"
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


@router.get("/ext-llm", summary="Получить настройки внешнего LLM")
async def get_ext_llm():
    """Вернуть текущие настройки внешнего LLM для анализа документов."""
    return {
        "url": _ext_llm_config.url,
        "model": _ext_llm_config.model,
        "provider": _ext_llm_config.provider,
        "api_key": _ext_llm_config.api_key
    }


@router.post("/ext-llm", summary="Сохранить настройки внешнего LLM")
async def save_ext_llm(config: ExtLLMConfig):
    """Сохранить настройки внешнего LLM."""
    global _ext_llm_config
    _ext_llm_config = config
    logger.info(f"Внешний LLM настроен: {config.provider}/{config.model} @ {config.url}")
    return {"status": "ok", "message": "Настройки сохранены"}


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
        else:
            return {"ok": False, "error": f"Провайдер {_ext_llm_config.provider} пока не поддерживается для теста"}
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return {"ok": True, "response": data.get("response", "")}
                else:
                    return {"ok": False, "error": f"HTTP {resp.status}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

_graph_model_config = {"model": "mistral:7b", "provider": "ollama"}

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
