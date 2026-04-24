"""
System Monitor Service для KAG

Мониторинг ресурсов хостовой машины:
- CPU usage (per-core)
- Memory usage  
- Disk usage
- Load average
"""

from typing import Dict, Any
import os
import psutil
from loguru import logger


class SystemMonitor:
    """Мониторинг ресурсов хоста"""
    
    def __init__(self):
        self._psutil_available = True
        try:
            psutil.cpu_percent(interval=0.1)
        except Exception:
            self._psutil_available = False
            logger.warning("psutil недоступен")
    
    def get_cpu_info(self) -> Dict[str, Any]:
        """Информация о CPU"""
        if not self._psutil_available:
            return {"error": "psutil недоступен"}
        
        try:
            return {
                "physical_cores": psutil.cpu_count(logical=False),
                "logical_cores": psutil.cpu_count(logical=True),
                "usage_percent": psutil.cpu_percent(interval=1, percpu=False),
                "per_core_percent": psutil.cpu_percent(interval=1, percpu=True),
                "load_avg": os.getloadavg() if hasattr(os, 'getloadavg') else [0, 0, 0],
            }
        except Exception as e:
            logger.error(f"Ошибка получения CPU info: {e}")
            return {"error": str(e)}
    
    def get_memory_info(self) -> Dict[str, Any]:
        """Информация о памяти"""
        if not self._psutil_available:
            return {"error": "psutil недоступен"}
        
        try:
            mem = psutil.virtual_memory()
            swap = psutil.swap_memory()
            return {
                "total": mem.total,
                "available": mem.available,
                "used": mem.used,
                "percent": mem.percent,
                "free": mem.free,
                "swap_total": swap.total,
                "swap_used": swap.used,
                "swap_percent": swap.percent,
            }
        except Exception as e:
            logger.error(f"Ошибка получения memory info: {e}")
            return {"error": str(e)}
    
    def get_disk_info(self) -> Dict[str, Any]:
        """Информация о дисках"""
        if not self._psutil_available:
            return {"error": "psutil недоступен"}
        
        try:
            disks = []
            for part in psutil.disk_partitions():
                try:
                    usage = psutil.disk_usage(part.mountpoint)
                    disks.append({
                        "device": part.device,
                        "mountpoint": part.mountpoint,
                        "fstype": part.fstype,
                        "total": usage.total,
                        "used": usage.used,
                        "free": usage.free,
                        "percent": usage.percent,
                    })
                except PermissionError:
                    continue
            return disks
        except Exception as e:
            logger.error(f"Ошибка получения disk info: {e}")
            return [{"error": str(e)}]
    
    def get_network_info(self) -> Dict[str, Any]:
        """Информация о сети"""
        if not self._psutil_available:
            return {"error": "psutil недоступен"}
        
        try:
            net = psutil.net_io_counters(pernic=True)
            interfaces = {}
            for iface, stats in net.items():
                interfaces[iface] = {
                    "bytes_sent": stats.bytes_sent,
                    "bytes_recv": stats.bytes_recv,
                    "packets_sent": stats.packets_sent,
                    "packets_recv": stats.packets_recv,
                    "errin": stats.errin,
                    "errout": stats.errout,
                }
            return interfaces
        except Exception as e:
            logger.error(f"Ошибка получения network info: {e}")
            return {"error": str(e)}
    
    def get_system_info(self) -> Dict[str, Any]:
        """Полная информация о системе"""
        import platform
        import uptime
        
        return {
            "hostname": platform.node(),
            "os": {
                "system": platform.system(),
                "release": platform.release(),
                "version": platform.version(),
                "machine": platform.machine(),
            },
            "uptime": uptime.uptime() if hasattr(uptime, 'uptime') else 0,
            "cpu": self.get_cpu_info(),
            "memory": self.get_memory_info(),
            "disk": self.get_disk_info(),
            "network": self.get_network_info(),
        }


# Глобальный экземпляр
system_monitor = SystemMonitor()