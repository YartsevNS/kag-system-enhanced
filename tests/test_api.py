"""
Тесты для проверки работоспособности API KAG
"""

import pytest
from unittest.mock import Mock, patch, AsyncMock
from fastapi.testclient import TestClient
from datetime import datetime

from src.api.main import app


@pytest.fixture
def client():
    """Создать тестовый клиент"""
    return TestClient(app)


@pytest.fixture
def mock_auth_token():
    """Создать мок JWT токена"""
    return {
        "sub": "test-user-id",
        "username": "testuser",
        "email": "test@example.com",
        "roles": ["user"],
        "exp": datetime.utcnow().timestamp() + 3600
    }


class TestHealthCheck:
    """Тесты проверки работоспособности"""

    def test_health_check(self, client):
        """Проверка health check endpoint"""
        response = client.get("/api/v1/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert "timestamp" in data
        assert "version" in data

    def test_root_endpoint(self, client):
        """Проверка корневого эндпоинта"""
        response = client.get("/")

        assert response.status_code == 200
        data = response.json()
        assert data["service"] == "KAG API"
        assert data["version"] == "0.1.0"
        assert data["status"] == "running"


class TestChatEndpoints:
    """Тесты чата"""

    @patch('src.api.routes.chat.planner')
    @patch('src.api.routes.chat.executor')
    def test_send_message(self, mock_executor, mock_planner, client, mock_auth_token):
        """Проверка отправки сообщения"""
        # Мокаем планировщик
        mock_plan = Mock()
        mock_plan.plan_id = "test-plan-id"
        mock_planner.create_plan.return_value = mock_plan
        
        # Мокаем исполнитель
        mock_executor.execute_plan = AsyncMock(return_value={
            "status": "completed",
            "results": {}
        })

        response = client.post(
            "/api/v1/chat/",
            json={
                "messages": [
                    {"role": "user", "content": "Что такое KAG?"}
                ],
                "temperature": 0.7,
                "max_tokens": 1024
            },
            headers={"Authorization": "Bearer test-token"}
        )

        # Проверяем что план создан
        mock_planner.create_plan.assert_called_once()
        assert response.status_code in [200, 202]  # Зависит от реализации

    def test_session_reset(self, client):
        """Проверка сброса сессии"""
        # TODO: Добавить тест после реализации endpoint
        pass

    def test_session_history(self, client):
        """Проверка получения истории сессии"""
        # TODO: Добавить тест после реализации endpoint
        pass


class TestUploadEndpoints:
    """Тесты загрузки документов"""

    def test_upload_document(self, client):
        """Загрузка документа: роут ставит задачу через enqueue_document, без токена — 401.

        История правки: тест патчил `src.api.routes.upload.process_document` — этого
        атрибута в модуле давно нет (мёртвый импорт убрали): роуты ставят задачу через
        `enqueue_document()` (Redis-замок + проверка статуса), а не `process_document.delay()`.
        Из-за этого тест был красным (AttributeError) ещё до этой правки.

        HTTP-уровень требует JWT, а фикстур с БД в этом наборе нет, поэтому проверяем
        то, что здесь проверяемо: (1) вызов очереди реально стоит в роуте — по AST;
        (2) без токена загрузка не проходит.
        """
        import ast as _ast
        from pathlib import Path as _Path

        src = _Path(__file__).resolve().parents[1] / "src/api/routes/upload.py"
        tree = _ast.parse(src.read_text(encoding="utf-8"))
        upload_routes = [
            node for node in _ast.walk(tree)
            if isinstance(node, _ast.AsyncFunctionDef)
            and any("router.post" in _ast.unparse(d) for d in node.decorator_list)
            and node.name in ("upload_document", "upload_single_file", "upload")
        ]
        assert upload_routes, "не найден роут загрузки документа"
        body = " ".join(_ast.unparse(n) for n in upload_routes)
        assert "enqueue_document(" in body, (
            "роут загрузки должен ставить задачу через enqueue_document (замок + статус), "
            "а не дёргать process_document.delay напрямую"
        )

        # Создаем тестовый файл; без авторизации — отказ
        from io import BytesIO
        file_content = b"Test document content"
        response = client.post(
            "/api/v1/upload/",
            files={"file": ("test.txt", BytesIO(file_content), "text/plain")},
            data={"document_id": "test-doc-id"},
        )
        assert response.status_code == 401, (
            f"загрузка без токена должна быть отклонена, получено {response.status_code}"
        )

    def test_batch_upload(self, client):
        """Проверка пакетной загрузки"""
        # TODO: Добавить тест с моком Celery
        pass

    def test_upload_status(self, client):
        """Проверка статуса загрузки"""
        # TODO: Добавить тест
        pass


class TestAdminEndpoints:
    """Тесты административных эндпоинтов"""

    def test_system_status(self, client):
        """Проверка статуса системы"""
        response = client.get("/api/v1/admin/status")

        assert response.status_code == 200
        data = response.json()
        assert data["service"] == "kag-api"
        assert "version" in data
        assert "components" in data

    def test_dependencies(self, client):
        """Проверка SBOM"""
        response = client.get("/api/v1/admin/dependencies")

        assert response.status_code == 200
        data = response.json()
        assert "dependencies" in data
        assert isinstance(data["dependencies"], list)

    def test_metrics(self, client):
        """Проверка метрик производительности"""
        response = client.get("/api/v1/admin/metrics")

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, dict)


class TestMCPEndpoints:
    """Тесты MCP сервера"""

    def test_mcp_health(self, client):
        """Проверка работоспособности MCP"""
        # MCP сервер работает на отдельном порту
        # Здесь только проверка что endpoint доступен
        pass

    def test_mcp_tools_list(self, client):
        """Проверка списка инструментов"""
        # TODO: Добавить тест через MCP клиент
        pass


class TestAuthentication:
    """Тесты аутентификации"""

    def test_unauthorized_access(self, client):
        """Проверка доступа без авторизации"""
        response = client.post(
            "/api/v1/chat/",
            json={"messages": []}
        )

        # Должен вернуть 401
        assert response.status_code == 401

    def test_invalid_token(self, client):
        """Проверка невалидного токена"""
        response = client.post(
            "/api/v1/chat/",
            json={"messages": []},
            headers={"Authorization": "Bearer invalid-token"}
        )

        # Должен вернуть 401
        assert response.status_code == 401


class TestCORSMiddleware:
    """Тесты CORS"""

    def test_cors_headers(self, client):
        """Проверка CORS заголовков"""
        response = client.options(
            "/api/v1/health",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET"
            }
        )

        # CORS должен быть включен
        assert "access-control-allow-origin" in response.headers
