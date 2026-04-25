"""
Главный модуль FastAPI приложения
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from loguru import logger
import os

from src.api.routes import chat, upload, admin, health, admin_models
from src.api.routes.chat import router_export
from src.api.routes import setup
from src.api.middleware.auth import AuthMiddleware
from src.api.middleware.setup_checker import SetupCheckMiddleware
from src.monitoring.opentelemetry import setup_opentelemetry
from src.monitoring.prometheus import setup_prometheus_metrics
from src.api.services.model_manager import model_manager


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Жизненный цикл приложения"""
    # Инициализация при запуске
    try:
        setup_opentelemetry()
    except Exception as e:
        logger.warning(f"OpenTelemetry не инициализирован: {e}")
    
    try:
        setup_prometheus_metrics()
    except Exception as e:
        logger.warning(f"Prometheus не инициализирован: {e}")
    
    # Инициализация менеджера моделей
    try:
        logger.info("Инициализация ModelManager...")
        await model_manager.initialize()
        logger.info("ModelManager инициализирован")
    except Exception as e:
        logger.warning(f"Ошибка инициализации: {e}")

    # Инициализация EmbeddingsService
    try:
        from src.indexing.embeddings_service import embeddings_service
        logger.info("Инициализация EmbeddingsService...")
        await embeddings_service.initialize()
        logger.info("EmbeddingsService инициализирован")
    except Exception as e:
        logger.warning(f"EmbeddingsService не инициализирован: {e}")
    
    yield
    
    # Очистка при завершении
    try:
        from src.indexing.embeddings_service import embeddings_service
        await embeddings_service.close()
        logger.info("EmbeddingsService закрыт")
    except Exception as e:
        logger.warning(f"Ошибка закрытия EmbeddingsService: {e}")

    try:
        logger.info("Завершение работы ModelManager...")
        await model_manager.close()
    except Exception as e:
        logger.warning(f"Ошибка закрытия ModelManager: {e}")
    logger.info("Приложение закрыто")


app = FastAPI(
    title="KAG API",
    description="API для системы многоагентной обработки знаний (KAG)",
    version="0.1.0",
    lifespan=lifespan,
)

# Middleware для проверки setup (должен быть первым)
app.add_middleware(SetupCheckMiddleware)

# CORS middleware (optional - for browser)
if os.getenv("ENABLE_CORS", "false").lower() == "true":
    app.add_middleware(
        CORSMiddleware,
        allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Middleware аутентификации (все запросы разрешены для разработки)
app.add_middleware(AuthMiddleware)

# Подключение роутеров
app.include_router(health.router, prefix="/api/v1", tags=["health"])
app.include_router(setup.router, prefix="/api/v1", tags=["setup"])
app.include_router(chat.router, prefix="/api/v1/chat", tags=["chat"])
app.include_router(router_export, prefix="/api/v1/chat/export", tags=["export"])
app.include_router(upload.router, prefix="/api/v1/upload", tags=["upload"])
app.include_router(admin.router, prefix="/api/v1/admin", tags=["admin"])
app.include_router(admin_models.router, prefix="/api/v1/admin/models", tags=["models"])

# Статические файлы и веб-интерфейс
static_path = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_path):
    app.mount("/static", StaticFiles(directory=static_path), name="static")


@app.get("/", summary="Веб-интерфейс KAG")
async def root_web():
    """Главная страница - веб-интерфейс чата"""
    index_path = os.path.join(static_path, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {
        "service": "KAG API",
        "version": "0.1.0",
        "status": "running",
        "docs": "/docs",
        "web_ui": "/"
    }


@app.get("/admin", summary="Админ-панель управления моделями")
async def admin_web():
    """Страница админ-панели"""
    admin_path = os.path.join(static_path, "admin.html")
    if os.path.exists(admin_path):
        return FileResponse(admin_path)
    return {"error": "Admin page not found"}


@app.get("/docker", summary="Docker Dashboard")
async def docker_dashboard():
    """Страница Docker Dashboard"""
    docker_path = os.path.join(static_path, "docker.html")
    if os.path.exists(docker_path):
        return FileResponse(docker_path)
    return {"error": "Docker dashboard not found"}


@app.get("/setup", summary="Страница первоначальной настройки")
async def setup_page():
    """Страница Setup Wizard"""
    setup_path = os.path.join(static_path, "setup.html")
    if os.path.exists(setup_path):
        return FileResponse(setup_path)
    return {"error": "Setup page not found"}


@app.get("/documents", summary="Управление документами")
async def documents_page():
    """Страница управления документами"""
    docs_path = os.path.join(static_path, "documents.html")
    if os.path.exists(docs_path):
        return FileResponse(docs_path)
    return {"error": "Documents page not found"}


@app.get("/qdrant", summary="Qdrant Database Dashboard")
async def qdrant_dashboard():
    """Страница Qdrant Dashboard"""
    qdrant_path = os.path.join(static_path, "qdrant.html")
    if os.path.exists(qdrant_path):
        return FileResponse(qdrant_path)
    return {"error": "Qdrant dashboard not found"}


@app.get("/chunks", summary="Чанки документов")
async def chunks_page():
    """Страница чанков документов"""
    chunks_path = os.path.join(static_path, "chunks.html")
    if os.path.exists(chunks_path):
        return FileResponse(chunks_path)
    return {"error": "Chunks page not found"}
