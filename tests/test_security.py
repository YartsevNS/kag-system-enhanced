"""
Тесты для модулей безопасности KAG
"""

import pytest
import os
from pathlib import Path
from unittest.mock import Mock, patch

from src.security.gost_crypto import GOSTCrypto, GOSTCryptoError
from src.security.audit import AuditLogger, AuditEventType
from src.security.validator import SecurityValidator, SecurityValidationError


# ===========================================
# Тесты GOST криптографии
# ===========================================

class TestGOSTCrypto:
    """Тесты GOST криптографии"""

    @pytest.fixture
    def crypto(self):
        """Создать экземпляр GOSTCrypto"""
        return GOSTCrypto()

    @pytest.fixture
    def crypto_with_key(self):
        """Создать экземпляр с известным ключом"""
        key = b'test_key_32_bytes_long_test12'
        return GOSTCrypto(key=key)

    def test_encrypt_decrypt_string(self, crypto):
        """Тест шифрования/расшифровки строки"""
        plaintext = "Тестовое сообщение для шифрования"
        
        encrypted = crypto.encrypt(plaintext)
        decrypted = crypto.decrypt(encrypted)
        
        assert decrypted.decode('utf-8') == plaintext

    def test_encrypt_decrypt_bytes(self, crypto):
        """Тест шифрования/расшифровки байтов"""
        plaintext = b"Test bytes data"
        
        encrypted = crypto.encrypt(plaintext)
        decrypted = crypto.decrypt(encrypted)
        
        assert decrypted == plaintext

    def test_encrypt_decrypt_empty_string(self, crypto):
        """Тест шифрования пустой строки"""
        plaintext = ""
        
        encrypted = crypto.encrypt(plaintext)
        decrypted = crypto.decrypt(encrypted)
        
        assert decrypted.decode('utf-8') == plaintext

    def test_encrypt_produces_different_output(self, crypto):
        """Тест что шифрование дает разные результаты (из-за nonce)"""
        plaintext = "Same message"
        
        encrypted1 = crypto.encrypt(plaintext)
        encrypted2 = crypto.encrypt(plaintext)
        
        # Зашифрованные данные должны отличаться (разные nonce)
        assert encrypted1 != encrypted2

    def test_decrypt_invalid_data(self, crypto):
        """Тест расшифровки невалидных данных"""
        with pytest.raises(GOSTCryptoError):
            crypto.decrypt(b"invalid_data")

    def test_decrypt_too_short(self, crypto):
        """Тест расшифровки слишком коротких данных"""
        with pytest.raises(GOSTCryptoError):
            crypto.decrypt(b"short")

    def test_encrypt_to_base64(self, crypto):
        """Тест шифрования в Base64"""
        plaintext = "Test message"
        
        encrypted_b64 = crypto.encrypt_to_base64(plaintext)
        decrypted = crypto.decrypt_from_base64(encrypted_b64)
        
        assert decrypted.decode('utf-8') == plaintext
        assert isinstance(encrypted_b64, str)

    def test_hash_gost_streebog(self):
        """Тест хэширования GOST Streebog"""
        data = "Test data for hashing"
        
        hash_256 = GOSTCrypto.hash_gost_streebog(data, 256)
        hash_512 = GOSTCrypto.hash_gost_streebog(data, 512)
        
        # Проверяем что хэши разные
        assert hash_256 != hash_512
        
        # Проверяем длину HEX
        assert len(hash_256) == 64  # 256 бит = 32 байта = 64 hex chars
        assert len(hash_512) == 128  # 512 бит = 64 байта = 128 hex chars

    def test_hash_consistency(self):
        """Тест консистентности хэширования"""
        data = "Consistent data"
        
        hash1 = GOSTCrypto.hash_gost_streebog(data, 256)
        hash2 = GOSTCrypto.hash_gost_streebog(data, 256)
        
        assert hash1 == hash2

    def test_hash_key_for_cache(self):
        """Тест хэширования ключа для кэша"""
        key = "user:123:personal_data"
        
        cache_key = GOSTCrypto.hash_key_for_cache(key)
        
        assert isinstance(cache_key, str)
        assert len(cache_key) == 64  # SHA-256 or Streebog-256

    def test_save_and_load_key(self, crypto, tmp_path):
        """Тест сохранения и загрузки ключа"""
        key_file = tmp_path / "test.key"
        
        crypto.save_key(key_file)
        loaded_crypto = GOSTCrypto.load_key(key_file)
        
        # Проверяем что ключи совпадают
        assert crypto.key == loaded_crypto.key
        
        # Проверяем что шифрование работает
        plaintext = "Test message"
        encrypted = crypto.encrypt(plaintext)
        decrypted = loaded_crypto.decrypt(encrypted)
        
        assert decrypted.decode('utf-8') == plaintext

    def test_generate_key(self):
        """Тест генерации ключа"""
        crypto1 = GOSTCrypto()
        crypto2 = GOSTCrypto()
        
        # Ключи должны быть разными
        assert crypto1.key != crypto2.key
        
        # Длина ключа 32 байта
        assert len(crypto1.key) == 32


# ===========================================
# Тесты аудита
# ===========================================

class TestAuditLogger:
    """Тесты логгера аудита"""

    @pytest.fixture
    def audit_logger(self, tmp_path):
        """Создать логгер аудита"""
        log_file = tmp_path / "audit.log"
        return AuditLogger(log_file=log_file, enable_console=False)

    def test_log_auth_success(self, audit_logger):
        """Тест логирования успешной аутентификации"""
        audit_logger.log_auth_success(
            user_id="user-123",
            method="keycloak",
            ip_address="192.168.1.1"
        )
        
        # Проверяем что файл создан
        assert audit_logger._log_file.exists()
        content = audit_logger._log_file.read_text()
        assert "auth_success" in content
        assert "user-123" in content

    def test_log_auth_failure(self, audit_logger):
        """Тест логирования неудачной аутентификации"""
        audit_logger.log_auth_failure(
            user_id="user-456",
            method="keycloak",
            ip_address="10.0.0.1",
            reason="Invalid credentials"
        )
        
        content = audit_logger._log_file.read_text()
        assert "auth_failure" in content
        assert "Invalid credentials" in content

    def test_log_permission_denied(self, audit_logger):
        """Тест логирования отказа в доступе"""
        audit_logger.log_permission_denied(
            user_id="user-789",
            resource="/admin/settings",
            action="GET",
            ip_address="192.168.1.100"
        )
        
        content = audit_logger._log_file.read_text()
        assert "permission_denied" in content
        assert "/admin/settings" in content

    def test_log_policy_change(self, audit_logger):
        """Тест логирования изменения политики"""
        audit_logger.log_policy_change(
            user_id="admin-1",
            policy_name="rbac_policy",
            old_value="user, /api, read",
            new_value="user, /api, write",
            ip_address="192.168.1.1"
        )
        
        content = audit_logger._log_file.read_text()
        assert "policy_change" in content
        assert "rbac_policy" in content

    def test_log_data_access(self, audit_logger):
        """Тест логирования доступа к данным"""
        audit_logger.log_data_access(
            user_id="user-123",
            resource="document-456",
            ip_address="192.168.1.1"
        )
        
        content = audit_logger._log_file.read_text()
        assert "data_access" in content

    def test_log_data_modification(self, audit_logger):
        """Тест логирования изменения данных"""
        audit_logger.log_data_modification(
            user_id="user-123",
            resource="document-456",
            action="update",
            details={"field": "title"},
            ip_address="192.168.1.1"
        )
        
        content = audit_logger._log_file.read_text()
        assert "data_modification" in content

    def test_log_llm_request(self, audit_logger):
        """Тест логирования запроса к LLM"""
        audit_logger.log_llm_request(
            user_id="user-123",
            model="gpt-4",
            prompt_length=100,
            response_length=500,
            duration_seconds=2.5,
            ip_address="192.168.1.1"
        )
        
        content = audit_logger._log_file.read_text()
        assert "llm_request" in content
        assert "gpt-4" in content

    def test_log_admin_action(self, audit_logger):
        """Тест логирования действия администратора"""
        audit_logger.log_admin_action(
            user_id="admin-1",
            action="delete_user",
            details={"user_id": "user-789"},
            ip_address="192.168.1.1"
        )
        
        content = audit_logger._log_file.read_text()
        assert "admin_action" in content

    def test_log_format_is_json(self, audit_logger):
        """Тест что логи в формате JSON"""
        audit_logger.log_auth_success(
            user_id="user-123",
            method="keycloak"
        )
        
        content = audit_logger._log_file.read_text().strip()
        
        # Проверяем что это валидный JSON
        import json
        try:
            log_entry = json.loads(content)
            assert "timestamp" in log_entry
            assert "event_type" in log_entry
        except json.JSONDecodeError:
            pytest.fail("Лог не в формате JSON")


# ===========================================
# Тесты валидатора
# ===========================================

class TestSecurityValidator:
    """Тесты валидатора безопасности"""

    def test_sanitize_text_xss(self):
        """Тест санитизации XSS"""
        malicious_text = "<script>alert('XSS')</script>Hello"
        clean_text = SecurityValidator.sanitize_text(malicious_text)
        
        assert "<script>" not in clean_text
        assert "Hello" in clean_text

    def test_sanitize_text_javascript_protocol(self):
        """Тест санитизации javascript: протокола"""
        malicious_text = "javascript:alert('XSS')"
        clean_text = SecurityValidator.sanitize_text(malicious_text)
        
        assert "javascript:" not in clean_text.lower()

    def test_sanitize_text_sql_injection(self):
        """Тест санитизации SQL инъекции"""
        malicious_text = "SELECT * FROM users WHERE 1=1; DROP TABLE users;"
        clean_text = SecurityValidator.sanitize_text(malicious_text)
        
        # SQL паттерны должны быть удалены
        assert "SELECT" not in clean_text or "DROP" not in clean_text

    def test_sanitize_text_path_traversal(self):
        """Тест санитизации path traversal"""
        malicious_text = "../../etc/passwd"
        clean_text = SecurityValidator.sanitize_text(malicious_text)
        
        assert "../" not in clean_text

    def test_validate_text_input_valid(self):
        """Тест валидации корректного текста"""
        text = "Это нормальный текст"
        result = SecurityValidator.validate_text_input(text)
        
        assert result == text

    def test_validate_text_input_too_long(self):
        """Тест валидации слишком длинного текста"""
        text = "a" * 10001
        
        with pytest.raises(SecurityValidationError):
            SecurityValidator.validate_text_input(text, max_length=10000)

    def test_validate_text_input_empty(self):
        """Тест валидации пустого текста"""
        with pytest.raises(SecurityValidationError):
            SecurityValidator.validate_text_input("")

    def test_validate_text_input_malicious(self):
        """Тест валидации вредоносного текста"""
        text = "<script>alert('XSS')</script>"
        
        with pytest.raises(SecurityValidationError):
            SecurityValidator.validate_text_input(text)

    def test_validate_file_upload_valid(self, tmp_path):
        """Тест валидации корректного файла"""
        file_path = tmp_path / "test.pdf"
        file_path.write_bytes(b"test content")
        
        result = SecurityValidator.validate_file_upload(
            file_path=str(file_path),
            filename="test.pdf",
            file_size=1024,
            mime_type="application/pdf"
        )
        
        assert result is True

    def test_validate_file_upload_too_large(self, tmp_path):
        """Тест валидации слишком большого файла"""
        file_path = tmp_path / "large.pdf"
        file_path.write_bytes(b"x" * (101 * 1024 * 1024))  # 101 MB
        
        with pytest.raises(SecurityValidationError):
            SecurityValidator.validate_file_upload(
                file_path=str(file_path),
                filename="large.pdf",
                file_size=101 * 1024 * 1024
            )

    def test_validate_file_upload_empty(self, tmp_path):
        """Тест валидации пустого файла"""
        file_path = tmp_path / "empty.pdf"
        file_path.write_bytes(b"")
        
        with pytest.raises(SecurityValidationError):
            SecurityValidator.validate_file_upload(
                file_path=str(file_path),
                filename="empty.pdf",
                file_size=0
            )

    def test_validate_file_upload_invalid_extension(self, tmp_path):
        """Тест валидации файла с недопустимым расширением"""
        file_path = tmp_path / "test.exe"
        file_path.write_bytes(b"malicious content")
        
        with pytest.raises(SecurityValidationError):
            SecurityValidator.validate_file_upload(
                file_path=str(file_path),
                filename="test.exe",
                file_size=1024
            )

    def test_validate_user_id_valid(self):
        """Тест валидации корректного ID пользователя"""
        user_id = "user-123_abc"
        result = SecurityValidator.validate_user_id(user_id)
        
        assert result == user_id

    def test_validate_user_id_invalid(self):
        """Тест валидации некорректного ID"""
        with pytest.raises(SecurityValidationError):
            SecurityValidator.validate_user_id("<script>alert('XSS')</script>")

    def test_validate_api_key_valid(self):
        """Тест валидации API ключа"""
        api_key = "sk-a7f449719f624d11a7e17a068f6035a8"
        
        result = SecurityValidator.validate_api_key(api_key)
        assert result is True

    def test_validate_api_key_too_short(self):
        """Тест валидации короткого API ключа"""
        api_key = "short_key"
        
        result = SecurityValidator.validate_api_key(api_key)
        assert result is False

    def test_rate_limit_check(self):
        """Тест проверки лимита запросов"""
        from datetime import datetime, timedelta
        
        client_id = "client-123"
        now = datetime.utcnow()
        
        # Создаем список запросов
        requests = [now - timedelta(seconds=i) for i in range(50)]
        
        # Проверяем лимит (100 запросов в минуту)
        result = SecurityValidator.rate_limit_check(
            client_id=client_id,
            requests=requests,
            window_seconds=60,
            max_requests=100
        )
        
        assert result is True

    def test_rate_limit_exceeded(self):
        """Тест превышения лимита запросов"""
        from datetime import datetime, timedelta
        
        client_id = "client-123"
        now = datetime.utcnow()
        
        # Создаем больше запросов чем лимит
        requests = [now - timedelta(seconds=i) for i in range(150)]
        
        result = SecurityValidator.rate_limit_check(
            client_id=client_id,
            requests=requests,
            window_seconds=60,
            max_requests=100
        )
        
        assert result is False

    def test_validate_query_params(self):
        """Тест валидации параметров запроса"""
        params = {
            "search": "normal query",
            "page": "1",
            "limit": "10"
        }
        
        result = SecurityValidator.validate_query_params(params)
        
        assert isinstance(result, dict)
        assert "search" in result

    def test_validate_query_params_malicious(self):
        """Тест валидации вредоносных параметров"""
        params = {
            "search": "<script>alert('XSS')</script>",
            "callback": "javascript:evil()"
        }
        
        result = SecurityValidator.validate_query_params(params)
        
        # Опасные паттерны должны быть удалены
        assert "<script>" not in result.get("search", "")
