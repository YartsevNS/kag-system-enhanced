"""
SSH Connection Manager для KAG

Хранит настройки SSH подключений в Redis (надежно, не теряется).
Использует GOST шифрование для защиты конфиденциальных данных.
"""

from typing import Dict, Any, Optional
from datetime import datetime
import json
from loguru import logger
from pydantic import BaseModel, Field

from src.security.gost_crypto import GOSTCrypto
from src.api.services.config_store import config_store


class SSHConnectionConfig(BaseModel):
    """Конфигурация SSH подключения"""
    host: str = Field(default="192.168.50.41", description="IP адрес или хост")
    port: int = Field(default=22, description="SSH порт", ge=1, le=65535)
    username: str = Field(default="nick", description="SSH пользователь")
    password: Optional[str] = Field(default=None, description="SSH пароль (зашифрован)")
    use_key: bool = Field(default=False, description="Использовать SSH ключ вместо пароля")
    key_path: Optional[str] = Field(default=None, description="Путь к SSH ключу")
    sudo_password: Optional[str] = Field(default=None, description="Пароль для sudo (зашифрован)")
    ollama_port: int = Field(default=11434, description="Порт Ollama API")
    ollama_service_name: str = Field(default="ollama", description="Имя сервиса Ollama")
    updated_at: datetime = Field(default_factory=datetime.utcnow, description="Время обновления")


class SSHConnectionManager:
    """
    Менеджер SSH подключений.

    Хранит конфигурацию подключений в Redis (надежно!).
    Пароли шифруются через GOST.
    """

    def __init__(self):
        """Инициализация менеджера"""
        self._crypto = GOSTCrypto()
        logger.info("SSHConnectionManager инициализирован (Redis storage)")

    def _encrypt_value(self, value: str) -> str:
        """Зашифровать значение"""
        if not value:
            return ""
        return self._crypto.encrypt_to_base64(value)

    def _decrypt_value(self, encrypted_value: str) -> str:
        """Расшифровать значение"""
        if not encrypted_value:
            return ""
        try:
            return self._crypto.decrypt_from_base64(encrypted_value).decode('utf-8')
        except Exception as e:
            logger.error(f"Ошибка расшифровки: {e}")
            return ""

    def get_config(self, connection_id: str = "default") -> SSHConnectionConfig:
        """
        Получить конфигурацию подключения из Redis.
        
        Пароли автоматически расшифровываются.
        """
        try:
            data = config_store.get("ssh", connection_id)
            
            if data is None:
                # Возвращаем конфигурацию по умолчанию
                return SSHConnectionConfig()
            
            # Расшифровываем чувствительные поля
            if data.get('password'):
                data['password'] = self._decrypt_value(data['password'])
            if data.get('sudo_password'):
                data['sudo_password'] = self._decrypt_value(data['sudo_password'])
            
            return SSHConnectionConfig(**data)
        except Exception as e:
            logger.error(f"Ошибка загрузки SSH конфигурации: {e}")
            return SSHConnectionConfig()

    def save_config(
        self,
        config: SSHConnectionConfig,
        connection_id: str = "default"
    ) -> bool:
        """
        Сохранить конфигурацию подключения в PostgreSQL.
        
        Пароли автоматически шифруются перед сохранением.
        """
        try:
            data = config.model_dump()
            
            # Преобразуем datetime в строку для JSON
            if isinstance(data.get('updated_at'), datetime):
                data['updated_at'] = data['updated_at'].isoformat()
            
            # Шифруем чувствительные поля
            if data.get('password'):
                data['password'] = self._encrypt_value(data['password'])
            if data.get('sudo_password'):
                data['sudo_password'] = self._encrypt_value(data['sudo_password'])
            
            success = config_store.set("ssh", connection_id, data)
            
            if success:
                logger.info(f"SSH конфигурация сохранена в PostgreSQL: {connection_id}")
            else:
                logger.error("Ошибка сохранения SSH конфигурации в PostgreSQL")
            
            return success
        except Exception as e:
            logger.error(f"Ошибка сохранения конфигурации SSH: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False

    def delete_config(self, connection_id: str = "default") -> bool:
        """Удалить конфигурацию подключения"""
        return config_store.delete("ssh", connection_id)

    def list_connections(self) -> Dict[str, Dict[str, Any]]:
        """
        Получить список всех подключений (без паролей).
        """
        all_configs = config_store.get_all("ssh")
        result = {}
        
        for conn_id, data in all_configs.items():
            result[conn_id] = {
                "host": data.get("host"),
                "port": data.get("port"),
                "username": data.get("username"),
                "use_key": data.get("use_key", False),
                "ollama_port": data.get("ollama_port"),
                "has_password": bool(data.get("password")),
                "has_sudo_password": bool(data.get("sudo_password")),
                "updated_at": data.get("updated_at")
            }
        
        return result

    def test_connection(self, config: SSHConnectionConfig) -> Dict[str, Any]:
        """
        Протестировать SSH подключение.
        """
        import subprocess
        
        try:
            # Формируем команду с sshpass для автоматической передачи пароля
            if config.password:
                ssh_cmd = f"sshpass -p '{config.password}' ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 -p {config.port} {config.username}@{config.host}"
            else:
                ssh_cmd = f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 -p {config.port} {config.username}@{config.host}"
            
            # Тестируем подключение
            if config.sudo_password:
                test_cmd = f"{ssh_cmd} 'echo {config.sudo_password} | sudo -S echo OK'"
            else:
                test_cmd = f"{ssh_cmd} 'echo OK'"
            
            logger.debug(f"Тест SSH: {test_cmd}")
            
            result = subprocess.run(
                test_cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=20
            )
            
            logger.debug(f"SSH результат: returncode={result.returncode}")
            
            if result.returncode == 0:
                # Проверяем Ollama
                import httpx
                try:
                    response = httpx.get(
                        f"http://{config.host}:{config.ollama_port}/",
                        timeout=10.0
                    )
                    ollama_healthy = response.status_code == 200
                except:
                    ollama_healthy = False
                
                return {
                    "success": True,
                    "ssh_connected": True,
                    "ollama_healthy": ollama_healthy,
                    "message": "Подключение успешно" + (" • Ollama работает" if ollama_healthy else " • Ollama не отвечает")
                }
            else:
                return {
                    "success": False,
                    "ssh_connected": False,
                    "message": f"SSH ошибка: {result.stderr.strip()[:300]}"
                }
                
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "ssh_connected": False,
                "message": "Таймаут подключения (20 секунд)"
            }
        except Exception as e:
            return {
                "success": False,
                "ssh_connected": False,
                "message": f"Ошибка: {str(e)}"
            }


# Глобальный экземпляр
ssh_manager = SSHConnectionManager()
