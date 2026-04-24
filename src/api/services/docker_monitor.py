"""
Docker Monitor Service для KAG

Отслеживает состояние контейнеров и потребление ресурсов:
- Список контейнеров
- CPU usage
- Memory usage
- Disk usage
- Network I/O

Использует Python Docker SDK для прямого подключения к Docker API через сокет.
"""

from typing import Dict, Any, List, Optional
from datetime import datetime
import json
import os
from loguru import logger

try:
    import docker
    DOCKER_AVAILABLE = True
except ImportError:
    DOCKER_AVAILABLE = False
    logger.warning("Docker SDK не установлен. Установите: pip install docker")


class DockerMonitor:
    """
    Сервис мониторинга Docker контейнеров.
    
    Использует Docker Python SDK для получения информации о контейнерах.
    """

    def __init__(self):
        """Инициализация монитора"""
        self._client = None
        if DOCKER_AVAILABLE:
            self._connect()
    
    def _connect(self):
        """Подключиться к Docker через сокет"""
        if DOCKER_AVAILABLE:
            # Пробуем через сокет с правами root (host socket)
            socket_path = os.environ.get('DOCKER_SOCKET', '/var/run/docker.sock')
            
            # Сначала пробуем стандартный способ
            try:
                self._client = docker.from_env()
                self._client.ping()
                logger.info("DockerMonitor инициализирован (стандартное подключение)")
                return
            except Exception as e:
                logger.debug(f"Стандартное подключение не работает: {e}")
            
            # Пробуем через Unix сокет
            try:
                self._client = docker.DockerClient(base_url=f'unix://{socket_path}')
                self._client.ping()
                logger.info(f"DockerMonitor инициализирован через сокет: {socket_path}")
                return
            except Exception as e:
                logger.debug(f"Сокет не работает: {e}")
            
            # Пробуем через HTTP (если включен TCP)
            try:
                self._client = docker.DockerClient(base_url='http://localhost:2375')
                self._client.ping()
                logger.info("DockerMonitor инициализирован через HTTP")
                return
            except Exception as e:
                logger.debug(f"HTTP не работает: {e}")
                
            logger.warning(f"Не удалось подключиться к Docker: нет доступа к сокету")
        else:
            logger.warning("Docker SDK недоступен")

    def _ensure_client(self):
        """Убедиться что клиент подключён"""
        if not self._client and DOCKER_AVAILABLE:
            try:
                self._client = docker.from_env()
            except Exception as e:
                logger.error(f"Ошибка подключения к Docker: {e}")
                raise

    def get_containers_list(self) -> List[Dict[str, Any]]:
        """
        Получить список всех контейнеров.
        
        Returns:
            Список словарей с информацией о контейнерах
        """
        try:
            self._ensure_client()
            if not self._client:
                return []
            
            containers = self._client.containers.list(all=True)
            result = []
            
            for container in containers:
                attrs = container.attrs
                result.append({
                    "id": container.short_id,
                    "name": container.name,
                    "image": container.image.tags[0] if container.image.tags else str(container.image),
                    "status": container.status,
                    "state": "running" if container.status == "running" else "stopped",
                    "ports": json.dumps(attrs.get('NetworkSettings', {}).get('Ports', {})),
                    "created": container.attrs.get('Created', ''),
                    "command": container.attrs.get('Path', '')
                })
            
            return result
        except Exception as e:
            logger.error(f"Ошибка получения списка контейнеров: {e}")
            return []

    def get_container_stats(self) -> Dict[str, Dict[str, Any]]:
        """
        Получить статистику использования ресурсов контейнерами.
        
        Returns:
            Словарь {имя_контейнера: статистика}
        """
        try:
            self._ensure_client()
            if not self._client:
                return {}
            
            containers = self._client.containers.list(all=True)
            stats = {}
            
            for container in containers:
                try:
                    container_stats = container.stats(stream=False)
                    
                    # CPU расчет
                    cpu_delta = (
                        container_stats['cpu_stats']['cpu_usage']['total_usage'] -
                        container_stats['precpu_stats']['cpu_usage']['total_usage']
                    )
                    system_delta = (
                        container_stats['cpu_stats']['system_cpu_usage'] -
                        container_stats['precpu_stats']['system_cpu_usage']
                    )
                    
                    cpu_percent = 0.0
                    if system_delta > 0:
                        online_cpus = container_stats['cpu_stats'].get('online_cpus', 1)
                        cpu_percent = (cpu_delta / system_delta) * online_cpus * 100.0
                    
                    # Memory расчет
                    mem_usage = container_stats['memory_stats'].get('usage', 0) or 0
                    mem_limit = container_stats['memory_stats'].get('limit', 0) or 0
                    mem_percent = (mem_usage / mem_limit * 100.0) if mem_limit > 0 else 0.0
                    
                    # Network I/O
                    networks = container_stats.get('networks', {})
                    net_in = sum(n.get('rx_bytes', 0) for n in networks.values())
                    net_out = sum(n.get('tx_bytes', 0) for n in networks.values())
                    
                    # Block I/O
                    blkio = container_stats.get('blkio_stats', {}).get('io_service_bytes_recursive', [])
                    block_read = sum(b.get('value', 0) for b in blkio if b.get('op') == 'Read')
                    block_write = sum(b.get('value', 0) for b in blkio if b.get('op') == 'Write')
                    
                    stats[container.name] = {
                        "cpu_percent": round(cpu_percent, 2),
                        "mem_percent": round(mem_percent, 2),
                        "mem_used": mem_usage,
                        "mem_limit": mem_limit,
                        "net_in": net_in,
                        "net_out": net_out,
                        "block_read": block_read,
                        "block_write": block_write,
                        "pids": container_stats.get('pids_stats', {}).get('current', 0)
                    }
                except Exception as e:
                    logger.warning(f"Ошибка получения статистики {container.name}: {e}")
            
            return stats
        except Exception as e:
            logger.error(f"Ошибка получения статистики: {e}")
            return {}

    def get_system_info(self) -> Dict[str, Any]:
        """
        Получить общую информацию о Docker системе.
        
        Returns:
            Словарь с информацией о системе
        """
        try:
            self._ensure_client()
            if not self._client:
                return {}
            
            info = self._client.info()
            version = self._client.version()
            
            return {
                "docker_version": version.get('Version', 'unknown'),
                "containers_total": info.get('Containers', 0),
                "containers_running": info.get('ContainersRunning', 0),
                "containers_paused": info.get('ContainersPaused', 0),
                "containers_stopped": info.get('ContainersStopped', 0),
                "images_count": info.get('Images', 0),
                "driver": info.get('Driver', 'unknown'),
                "os": info.get('OperatingSystem', 'unknown'),
                "kernel": info.get('KernelVersion', 'unknown'),
                "architecture": info.get('Architecture', 'unknown'),
                "cpus": info.get('NCPU', 0),
                "memory_total": info.get('MemTotal', 0)
            }
        except Exception as e:
            logger.error(f"Ошибка получения информации о системе: {e}")
            return {}

    def get_detailed_stats(self) -> Dict[str, Any]:
        """
        Получить детальную статистику всех контейнеров.
        
        Returns:
            Полный отчёт о состоянии Docker
        """
        containers = self.get_containers_list()
        stats = self.get_container_stats()
        system_info = self.get_system_info()
        
        # Объединяем данные
        result_containers = []
        for container in containers:
            name = container["name"]
            container_stats = stats.get(name, {})
            
            result_containers.append({
                **container,
                "stats": container_stats
            })
        
        return {
            "system": system_info,
            "containers": result_containers,
            "timestamp": datetime.utcnow().isoformat()
        }

    def get_container_logs(self, container_name: str, lines: int = 100) -> str:
        """
        Получить логи контейнера.
        
        Args:
            container_name: Имя контейнера
            lines: Количество строк
            
        Returns:
            Логи контейнера
        """
        try:
            self._ensure_client()
            if not self._client:
                return "Docker недоступен"
            
            container = self._client.containers.get(container_name)
            return container.logs(tail=lines).decode('utf-8')
        except Exception as e:
            return f"Ошибка получения логов: {e}"

    def restart_container(self, container_name: str) -> bool:
        """Перезапустить контейнер"""
        try:
            self._ensure_client()
            container = self._client.containers.get(container_name)
            container.restart()
            return True
        except Exception as e:
            logger.error(f"Ошибка перезапуска: {e}")
            return False

    def stop_container(self, container_name: str) -> bool:
        """Остановить контейнер"""
        try:
            self._ensure_client()
            container = self._client.containers.get(container_name)
            container.stop()
            return True
        except Exception as e:
            logger.error(f"Ошибка остановки: {e}")
            return False

    def start_container(self, container_name: str) -> bool:
        """Запустить контейнер"""
        try:
            self._ensure_client()
            container = self._client.containers.get(container_name)
            container.start()
            return True
        except Exception as e:
            logger.error(f"Ошибка запуска: {e}")
            return False


# Глобальный экземпляр
docker_monitor = DockerMonitor()
