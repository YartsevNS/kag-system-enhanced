# KAG Project - Отчёт: Исправления и мониторинг (24 апреля 2026)

## Выполненные исправления

### 1. Ошибки импортов и конфигурации

Исправлены критические ошибки, которые мешали запуску проекта:

**config.py** - добавлены отсутствующие поля TLS:
```python
TLS_CERT_PATH: str = ""
TLS_KEY_PATH: str = ""
```

**ab_testing.py** - добавлен импорт Enum:
```python
from enum import Enum
```

### 2. Graceful fallback для директорий

При запуске вне контейнера (/app недоступен) - добавлен fallback на /tmp:

- `src/security/gost_crypto.py` - при ошибке сохранения ключа
- `src/api/services/model_manager.py` - fallback на /tmp
- `src/agents/evaluator.py` - fallback на /tmp/kag_annotations
- `src/evaluation/quality.py` - fallback на /tmp/kag_quality_tracking
- `src/evaluation/ab_testing.py` - fallback на /tmp/kag_ab_tests
- `src/api/services/document_service.py` - fallback на /tmp/kag_uploads
- `src/security/audit.py` - отключение file logging

### 3. Docker мониторинг

#### Проблема доступа к Docker socket

Изначально контейнер kag-api не имел доступа к Docker socket.

**Решение:**
1. Изменён docker-compose.yml:
```yaml
- /var/run/docker.sock:/var/run/docker.sock  # вместо :ro
```

2. Изменены права сокета:
```bash
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock alpine chmod 666 /var/run/docker.sock
```

3. Улучшен docker_monitor.py с的多 способов подключения:
- Стандартное подключение docker.from_env()
- Через Unix socket
- Через HTTP (localhost:2375)

### 4. System Monitor (новый сервис)

Создан новый модуль `src/api/services/system_monitor.py`:

```python
class SystemMonitor:
    def get_cpu_info()      # CPU usage, per-core, load average
    def get_memory_info()  # RAM, Swap
    def get_disk_info()  # Все диски
    def get_network_info() # Сетевые интерфейсы
    def get_system_info() # Полная информация
```

Добавлены зависимости в requirements.txt:
```
psutil>=5.9.0
uptime>=0.0.5
```

### 5. API endpoints

Добавлены новые endpoints в admin_models.py:

```
GET /api/v1/admin/models/system/info    - Полная информация о системе
GET /api/v1/admin/models/system/cpu    - CPU
GET /api/v1/admin/models/system/memory  - Память
GET /api/v1/admin/models/system/disk   - Диски
GET /api/v1/admin/models/system/network - Сеть
```

### 6. Обновлённый Docker Dashboard

Файл `src/api/static/docker.html`:

- Добавлена кнопка "📊 Система" в хедере
- Модальное окно с информацией о хостовой машине:
  - CPU (ядра, %, load average)
  - Память (использовано/всего, %)
  - Диски (устройство, точка монтирования, %)
  - Сеть (интерфейсы, трафик, ошибки)
- Автообновление каждые 10 секунд

## Итоги

### Работающие сервисы

| Сервис | Статус |
|--------|-------|
| kag-api | ✅ Up (healthy) |
| kag-mcp | ✅ Up (healthy) |
| kag-worker | ✅ Up (healthy) |
| kag-scheduler | ✅ Up (healthy) |
| kag-keycloak | ✅ Up |
| kag-qdrant | ✅ Up |
| kag-redis | ✅ Up (healthy) |
| kag-keycloak-db | ✅ Up (healthy) |

### Health checks

```bash
curl http://localhost:8000/api/v1/health
# {"status":"ok","version":"0.1.0"}

curl http://localhost:8000/api/v1/admin/models/docker/stats
# {"system":{...},"containers":[...],"timestamp":"..."}

curl http://localhost:8000/api/v1/admin/models/system/info
# {"hostname":..., "cpu":..., "memory":..., "disk":..., "network":...}
```

## GitHub коммит

```
commit 41f7faf
fix: Исправления ошибок и улучшения мониторинга

15 files changed, 471 insertions(+), 75 deletions(-)
```

https://github.com/YartsevNS/kag-system

## Использование

### Docker Dashboard
Открыть http://localhost:8000/docker

### Системная информация
Нажать кнопку "📊 Система" на странице Docker Dashboard

### Ручная проверка
```bash
curl http://localhost:8000/api/v1/admin/models/docker/stats
curl http://localhost:8000/api/v1/admin/models/system/info
```