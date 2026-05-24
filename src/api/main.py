"""
Главный модуль FastAPI приложения
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from loguru import logger
import os

from src.api.routes import chat, upload, admin, health, admin_models, auth, watchers, notifications, knowledge_graph, process_logs, web_monitor
from src.api.routes.chat import router_export
from src.api.routes import setup
from src.api.middleware.auth import AuthMiddleware
from src.api.middleware.setup_checker import SetupCheckMiddleware
from src.monitoring.opentelemetry import setup_opentelemetry
from src.monitoring.prometheus import setup_prometheus_metrics
from src.api.services.model_manager import model_manager
from src.config import get_settings

settings = get_settings()


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
    
    # Запуск сторожа перестроения графа (если есть необработанные документы)
    try:
        from src.api.services.config_store import config_store
        docs = config_store.get_all("documents") or {}
        completed = sum(1 for d in docs.values() if isinstance(d, dict) and d.get("status") == "completed")
        from src.indexing.knowledge_graph import kg_service
        kg_stats = kg_service.get_stats()
        kg_docs = kg_stats.get("documents", 0)
        if completed > 0 and (kg_docs < completed * 0.8 or kg_stats.get("entities", 0) < 10):
            logger.info(f"Запуск Watchdog: {kg_docs}/{completed} документов в графе")
            from src.indexing.rebuild_watchdog import rebuild_watchdog
            rebuild_watchdog.start()
        else:
            logger.info(f"Watchdog не нужен: {kg_docs}/{completed} доков в графе")
    except Exception as e:
        logger.warning(f"Watchdog не запущен: {e}")
    
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
    version=settings.APP_VERSION,
    lifespan=lifespan,
    docs_url="/api/docs",       # Swagger → /api/docs
    redoc_url="/api/redoc",     # ReDoc → /api/redoc
)

# Middleware для проверки setup (должен быть первым)
app.add_middleware(SetupCheckMiddleware)

# CORS middleware (включен для разработки)
cors_origins = os.getenv("CORS_ORIGINS", "*")
if cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in cors_origins.split(",")],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Middleware аутентификации (Keycloak JWT + статический токен)
app.add_middleware(AuthMiddleware)

# Подключение роутеров
app.include_router(health.router, prefix="/api/v1", tags=["health"])
app.include_router(setup.router, prefix="/api/v1", tags=["setup"])
app.include_router(chat.router, prefix="/api/v1/chat", tags=["chat"])
app.include_router(router_export, prefix="/api/v1/chat/export", tags=["export"])
app.include_router(upload.router, prefix="/api/v1/upload", tags=["upload"])
app.include_router(admin.router, prefix="/api/v1/admin", tags=["admin"])
app.include_router(admin_models.router, prefix="/api/v1/admin/models", tags=["models"])
app.include_router(knowledge_graph.router, prefix="/api/v1/kg", tags=["knowledge-graph"])
app.include_router(process_logs.router, prefix="/api/v1/process-logs", tags=["process-logs"])
app.include_router(web_monitor.router, prefix="/api/v1/monitor", tags=["web-monitor"])
app.include_router(auth.router, prefix="/api/v1/auth", tags=["auth"])
app.include_router(watchers.router, prefix="/api/v1/watchers", tags=["watchers"])
app.include_router(notifications.router, prefix="/api/v1/notifications", tags=["notifications"])

# Статические файлы и веб-интерфейс
static_path = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_path):
    app.mount("/static", StaticFiles(directory=static_path), name="static")


@app.get("/", summary="Веб-интерфейс KAG")
async def root_web():
    """Перенаправление на страницу настройки"""
    from starlette.responses import RedirectResponse
    return RedirectResponse(url="/setup")


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


@app.get("/login", summary="Страница входа")
async def login_page():
    """Страница аутентификации"""
    login_path = os.path.join(static_path, "login.html")
    if os.path.exists(login_path):
        return FileResponse(login_path)
    return {"error": "Login page not found"}


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


@app.get("/chunks", summary="Чанки документов", response_class=HTMLResponse)
async def chunks_page():
    """Страница чанков документов"""
    chunks_path = os.path.join(static_path, "chunks.html")
    if os.path.exists(chunks_path):
        return FileResponse(chunks_path, media_type="text/html")
    return {"error": "Chunks page not found"}


@app.get("/chat", summary="Чат с AI")
async def chat_page():
    """Страница чата"""
    chat_path = os.path.join(static_path, "chat.html")
    if os.path.exists(chat_path):
        return FileResponse(chat_path)
    return {"error": "Chat page not found"}


@app.get("/ocr", summary="OCR Демо")
async def ocr_page():
    """Страница демонстрации OCR"""
    ocr_path = os.path.join(static_path, "ocr.html")
    if os.path.exists(ocr_path):
        return FileResponse(ocr_path)
    return {"error": "OCR page not found"}


@app.get("/monitoring", summary="Мониторинг")
async def monitoring_page():
    """Страница мониторинга"""
    mon_path = os.path.join(static_path, "monitoring.html")
    if os.path.exists(mon_path):
        return FileResponse(mon_path)
    return {"error": "Monitoring page not found"}


@app.get("/users", summary="Пользователи и группы")
async def users_page():
    """Страница управления пользователями"""
    users_path = os.path.join(static_path, "users.html")
    if os.path.exists(users_path):
        return FileResponse(users_path)
    return {"error": "Users page not found"}


@app.get("/logs", summary="Логи системы")
async def logs_page():
    """Страница просмотра логов"""
    logs_path = os.path.join(static_path, "logs.html")
    if os.path.exists(logs_path):
        return FileResponse(logs_path)
    return {"error": "Logs page not found"}


@app.get("/search", summary="Поиск по метаданным")
async def search_page():
    """Страница поиска документов по метаданным"""
    search_path = os.path.join(static_path, "search.html")
    if os.path.exists(search_path):
        return FileResponse(search_path)
    return {"error": "Search page not found"}


@app.get("/viewer", summary="Просмотр документа")
async def viewer_page():
    """Страница просмотра документа"""
    viewer_path = os.path.join(static_path, "viewer.html")
    if os.path.exists(viewer_path):
        return FileResponse(viewer_path, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    return {"error": "Viewer page not found"}

@app.get("/api", summary="API и архитектура")
async def api_page():
    """Техническая документация API и архитектуры"""
    api_path = os.path.join(static_path, "docs.html")
    if os.path.exists(api_path):
        return FileResponse(api_path)
    return {"error": "API page not found"}

@app.get("/docs", summary="Документация проекта")
async def docs_page():
    """Читаемая документация по проекту"""
    guide_path = os.path.join(static_path, "guide.html")
    if os.path.exists(guide_path):
        return FileResponse(guide_path)
    return {"error": "Docs page not found"}

@app.get("/kg", summary="Граф знаний")
async def kg_page():
    """Страница графа знаний Neo4j"""
    kg_path = os.path.join(static_path, "kg.html")
    if os.path.exists(kg_path):
        return FileResponse(kg_path)
    return {"error": "KG page not found"}


@app.get("/monitor", summary="Веб-мониторинг")
async def monitor_page():
    monitor_path = os.path.join(static_path, "monitor.html")
    if os.path.exists(monitor_path):
        return FileResponse(monitor_path)
    return {"error": "Monitor page not found"}


@app.get("/news", summary="Лента новостей")
async def news_page():
    news_path = os.path.join(static_path, "news.html")
    if os.path.exists(news_path):
        return FileResponse(news_path)
    return {"error": "News page not found"}


@app.get("/know", summary="База знаний")
async def know_page():
    know_path = os.path.join(static_path, "know.html")
    if os.path.exists(know_path):
        return FileResponse(know_path)
    return {"error": "Know page not found"}
