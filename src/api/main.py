"""
Главный модуль FastAPI приложения
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from loguru import logger
import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

from src.api.routes import chat, upload, admin, health, admin_models, auth, watchers, notifications, knowledge_graph, process_logs, web_monitor, chunks
from src.api.routes.chat import router_export
from src.api.routes import setup
from src.api.routes import branding
from src.api.routes import system_state
from src.api.middleware.security import SecurityMiddleware
from src.api.middleware.setup_checker import SetupCheckMiddleware
from src.monitoring.opentelemetry import setup_opentelemetry
from src.monitoring.prometheus import setup_prometheus_metrics
from src.api.services.model_manager import model_manager
from src.config import get_settings


def _is_admin_request(request: Request) -> bool:
    """Админ ли текущий пользователь (роли ставит SecurityMiddleware).

    Зачем: HTML-страницы /admin, /docker и т.п. не под /api/v1/admin —
    middleware их не защищает. Проверяем request.state.roles, который
    SecurityMiddleware заполнил из JWT (локальный admin → roles=["admin"],
    keycloak → realm_access.roles).
    """
    roles = getattr(request.state, "roles", None) or set()
    return bool(roles & {"admin", "kag-admin"})

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Жизненный цикл приложения"""
    # Журналы: каждая строка несёт trace_id запроса (см. src/api/trace.py)
    configure_logging_with_trace()

    # Пул потоков для СИНХРОННЫХ вызовов (Neo4j, Qdrant-клиент, чтения БД в потоках).
    # Пул по умолчанию — min(32, CPU+4), и при десятках одновременных запросов он становится
    # узким местом: каждый чат делает несколько вызовов через asyncio.to_thread. Ставим свой
    # пул и логируем его размер, чтобы это было видно в замерах (обоснование — разбор и
    # консультация по плану async-перехода: docs/guides/async-migration-plan.md).
    try:
        from src.config import get_settings
        _threads = int(getattr(get_settings(), "ASYNC_BLOCKING_THREADS", 64) or 64)
        _threads = max(8, min(256, _threads))
        asyncio.get_running_loop().set_default_executor(
            ThreadPoolExecutor(max_workers=_threads, thread_name_prefix="kag-blocking"))
        logger.info(f"Пул потоков для блокирующих вызовов: {_threads}")
    except Exception as e:
        logger.warning(f"Не удалось поднять пул потоков для блокирующих вызовов: {e}")

    # Инициализация при запуске
    try:
        setup_opentelemetry()
    except Exception as e:
        logger.warning(f"OpenTelemetry не инициализирован: {e}")
    
    try:
        setup_prometheus_metrics()
    except Exception as e:
        logger.warning(f"Prometheus не инициализирован: {e}")

    # Индексы payload для фильтров поиска (domain/visibility/level/standard_number/clause).
    # Зачем на старте: ветка «коллекция уже существует» в embeddings_service проверяет только
    # размерность, поэтому на сервере с готовыми данными этих индексов не было бы — а без них
    # фильтр по level даёт p99 104,8 мс в КАЖДОМ поиске против 11,2 мс с индексом (замер 26.09.2026).
    # Создание идемпотентно, ошибки не мешают запуску сервиса.
    try:
        from src.indexing.embeddings_service import embeddings_service
        client = getattr(embeddings_service, "_qdrant_client", None)
        if client is not None:
            from qdrant_client.models import PayloadSchemaType
            for _field in ("domain", "visibility", "level", "standard_number", "clause"):
                try:
                    await asyncio.to_thread(
                        client.create_payload_index,
                        collection_name=embeddings_service.collection_name,
                        field_name=_field,
                        field_schema=PayloadSchemaType.KEYWORD)
                except Exception as e:
                    logger.debug(f"[qdrant] индекс по {_field} не создан: {e}")
            logger.info("Индексы payload для фильтров проверены")
    except Exception as e:
        logger.warning(f"Проверка индексов payload не выполнена: {e}")

    # Инициализация менеджера моделей
    try:
        logger.info("Инициализация ModelManager...")
        await model_manager.initialize()
        logger.info("ModelManager инициализирован")
    except Exception as e:
        logger.warning(f"Ошибка инициализации: {e}")

    # Доменная схема сущностей: применяем сохранённую. В памяти процесса её нет
    # (это единственная копия состояния) — без этого после рестарта api пресет
    # сбрасывался на «universal».
    try:
        from src.indexing.entity_extractor import entity_extractor
        mode = entity_extractor.apply_stored_domain_schema()
        logger.info(f"Доменная схема сущностей: {mode}")
    except Exception as e:
        logger.warning(f"Доменная схема сущностей не применена: {e}")

    # Промпты: засеять настройки из prompts/*.txt, если там пусто.
    # Нужно, чтобы на развёрнутом стенде промпты ЖИЛИ в настройках и правились
    # из админки, а файл оставался версионируемым дефолтом (см. provider_service).
    try:
        from src.api.services.provider_service import provider_service
        seed = provider_service.seed_prompts_from_files()
        if seed.get("seeded"):
            logger.info(f"Промпты засеяны из файлов в настройки: {', '.join(seed['seeded'])}")
        if seed.get("no_file"):
            logger.info(f"Файлов промптов нет (остаются как есть): {', '.join(seed['no_file'])}")
    except Exception as e:
        logger.warning(f"Засев промптов не выполнен: {e}")

    # Инициализация EmbeddingsService
    try:
        from src.indexing.embeddings_service import embeddings_service
        logger.info("Инициализация EmbeddingsService...")
        await embeddings_service.initialize()
        logger.info("EmbeddingsService инициализирован")
    except Exception as e:
        logger.warning(f"EmbeddingsService не инициализирован: {e}")
    
    # Запуск Hot Folder Watcher
    try:
        from src.indexing.hot_folder_watcher import hot_folder_watcher
        await hot_folder_watcher.start()
        logger.info("HotFolderWatcher запущен")
    except Exception as e:
        logger.warning(f"HotFolderWatcher не запущен: {e}")
    
    yield
    
    # Остановка Hot Folder Watcher
    try:
        from src.indexing.hot_folder_watcher import hot_folder_watcher
        await hot_folder_watcher.stop()
    except Exception:
        pass
    
    # Закрытие сервисов
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

# CORS middleware — конкретные origin для безопасности (cookie с credentials)
cors_origins = os.getenv("CORS_ORIGINS", "http://localhost:8000,http://192.168.50.18:8000,https://qd.gostsecret.ru")
if cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in cors_origins.split(",")],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "X-Trace-ID"],
    )

# SecurityMiddleware (JWT через JWKS + локальный fallback)
app.add_middleware(SecurityMiddleware)

# TraceMiddleware добавляем ПОСЛЕДНИМ: в Starlette последний добавленный — самый внешний,
# поэтому trace_id видит все запросы (включая ошибки в других middleware) и попадает в метрики.
from src.api.trace import TraceMiddleware, configure_logging_with_trace  # noqa: E402
app.add_middleware(TraceMiddleware)

# Подключение роутеров
app.include_router(health.router, prefix="/api/v1", tags=["health"])
app.include_router(setup.router, prefix="/api/v1", tags=["setup"])
app.include_router(branding.router, prefix="/api/v1", tags=["branding"])
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
app.include_router(chunks.router, prefix="/api/v1", tags=["chunks"])
app.include_router(system_state.router, tags=["system"])


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics():
    """Метрики в формате Prometheus (для скрейпа Prometheus/Grafana).

    Раньше метрики только объявлялись в src/monitoring/prometheus.py, но никуда не отдавались
    и почти никем не записывались — то есть наблюдаемость была нулевой. Теперь: скрейп отсюда,
    запись HTTP-запросов в TraceMiddleware, стадии RAG — в chat_service.
    """
    try:
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
        from fastapi import Response as _Response
        return _Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
    except Exception as e:  # метрики не должны ломать сервис
        from fastapi import Response as _Response
        return _Response(content=f"# метрики недоступны: {e}\n", media_type="text/plain")

# Статические файлы и веб-интерфейс
static_path = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_path):
    app.mount("/static", StaticFiles(directory=static_path), name="static")


# ── Общая функция отдачи HTML с no-cache ─────────────────────────────

async def _html_response(path: str) -> FileResponse:
    """Отдать HTML-файл с заголовками против кеширования.

    Проверка существования файла — в потоке: страницы отдаются на каждый переход
    в интерфейсе, а os.path.exists в async-обработчике блокирует event loop.
    """
    exists = await asyncio.to_thread(os.path.exists, path)
    if exists:
        return FileResponse(
            path,
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )
    return JSONResponse({"error": "Page not found"}, status_code=404)


@app.get("/", summary="Веб-интерфейс KAG")
async def root_web():
    """Перенаправление на /documents (если настроено) или /setup."""
    from starlette.responses import RedirectResponse
    try:
        from src.api.services.config_store import config_store
        status = config_store.get("setup", "status", {})
        if status.get("configured"):
            return RedirectResponse(url="/documents")
    except Exception:
        pass
    return RedirectResponse(url="/setup")


@app.get("/admin", summary="Админ-панель управления моделями")
async def admin_web(request: Request):
    """Страница админ-панели (только для admin).

    Зачем проверка: HTML-страница /admin НЕ под /api/v1/admin — security
    middleware её не защищал, и не-admin открывал её, но все API-запросы
    /admin/models/* падали 403 → «ошибки отображения провайдера».
    """
    if not _is_admin_request(request):
        return RedirectResponse(url="/documents", status_code=302)
    return await _html_response(os.path.join(static_path, "admin.html"))


@app.get("/experiments", summary="Опыты и модели (замеры, только для admin)")
async def experiments_page(request: Request):
    """Страница результатов замеров моделей (только для admin).

    Данные — из /api/v1/admin/models/experiments (config_store), поэтому обновляются
    без пересборки образа; если данных нет, страница показывает встроенный набор.
    """
    if not _is_admin_request(request):
        return RedirectResponse(url="/documents", status_code=302)
    return await _html_response(os.path.join(static_path, "experiments.html"))


@app.get("/docker", summary="Docker Dashboard")
async def docker_dashboard(request: Request):
    """Страница Docker Dashboard (только для admin)."""
    if not _is_admin_request(request):
        return RedirectResponse(url="/documents", status_code=302)
    docker_path = os.path.join(static_path, "docker.html")
    if os.path.exists(docker_path):
        return FileResponse(docker_path)
    return {"error": "Docker dashboard not found"}


@app.get("/setup", summary="Страница первоначальной настройки")
async def setup_page():
    """Страница Setup Wizard"""
    return await _html_response(os.path.join(static_path, "setup.html"))


@app.get("/login", summary="Страница входа")
async def login_page():
    """Страница аутентификации"""
    return await _html_response(os.path.join(static_path, "login.html"))


@app.get("/documents", summary="Управление документами")
async def documents_page():
    """Страница управления документами"""
    return await _html_response(os.path.join(static_path, "documents.html"))


@app.get("/qdrant", summary="Qdrant Database Dashboard")
async def qdrant_dashboard(request: Request):
    """Страница Qdrant Dashboard (только для admin)."""
    if not _is_admin_request(request):
        return RedirectResponse(url="/documents", status_code=302)
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
    return await _html_response(os.path.join(static_path, "chat.html"))


@app.get("/monitoring", summary="Мониторинг")
async def monitoring_page():
    """Страница мониторинга"""
    mon_path = os.path.join(static_path, "monitoring.html")
    if os.path.exists(mon_path):
        return FileResponse(mon_path)
    return {"error": "Monitoring page not found"}


@app.get("/system", summary="Состояние системы")
async def system_page():
    """Страница состояния системы: метрики изнутри api (отдаётся через _html_response,
    то есть без кеша браузера — иначе после правок видно старую версию страницы)."""
    return await _html_response(os.path.join(static_path, "system.html"))


@app.get("/users", summary="Пользователи и группы")
async def users_page(request: Request):
    """Страница управления пользователями (только для admin)."""
    if not _is_admin_request(request):
        return RedirectResponse(url="/documents", status_code=302)
    users_path = os.path.join(static_path, "users.html")
    if os.path.exists(users_path):
        return FileResponse(users_path)
    return {"error": "Users page not found"}


@app.get("/know", summary="База знаний KAG")
async def know_web():
    return await _html_response(os.path.join(static_path, "know.html"))


@app.get("/logs", summary="Логи системы")
async def logs_page(request: Request):
    """Страница просмотра логов (только для admin)."""
    if not _is_admin_request(request):
        return RedirectResponse(url="/documents", status_code=302)
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

@app.get("/embedding-guide", summary="Руководство по выбору embedding-модели")
async def embedding_guide_page():
    """Страница-инструкция: выбор embedding-модели, связка Qdrant/Neo4j.

    Доступна из админки (ссылка в разделе «Настройки чанкинга») и из
    бокового меню. Содержит подробное объяснение, как связаны embedding,
    размерность вектора, Qdrant и Neo4j, и что делать при смене модели.
    """
    guide_path = os.path.join(static_path, "embedding-guide.html")
    if os.path.exists(guide_path):
        return FileResponse(guide_path)
    return {"error": "Embedding guide page not found"}

@app.get("/kg", summary="Граф знаний")
async def kg_page():
    """Страница графа знаний Neo4j"""
    kg_path = os.path.join(static_path, "kg.html")
    if os.path.exists(kg_path):
        return FileResponse(kg_path)
    return {"error": "KG page not found"}


@app.get("/prompts-help", summary="Справка: промпты, типы и маркеры")
async def prompts_help_page():
    """Страница-справка: как настраиваются системные промпты, типы документов,
    маркеры классификации, домены запросов и доменные схемы. Доступна по ссылке
    из админки (раздел «Привязка функций к провайдерам»)."""
    help_path = os.path.join(static_path, "prompts-help.html")
    if os.path.exists(help_path):
        return FileResponse(help_path, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    return {"error": "Prompts help page not found"}


@app.get("/monitor", summary="Веб-мониторинг")
async def monitor_page():
    return await _html_response(os.path.join(static_path, "monitor.html"))


@app.get("/news", summary="Лента новостей")
async def news_page():
    return await _html_response(os.path.join(static_path, "news.html"))


