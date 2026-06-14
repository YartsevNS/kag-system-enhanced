"""
Административные маршруты
"""

from typing import Optional
from fastapi import APIRouter, HTTPException
from loguru import logger
from datetime import datetime

from src.models import SystemStatus
from src.config import get_settings

router = APIRouter()


@router.get("/status", response_model=SystemStatus, summary="Статус системы")
async def get_system_status():
    """
    Получить общий статус системы и всех компонентов.
    
    Возвращает информацию о:
    - Версии сервиса
    - Времени работы
    - Статусе компонентов (БД, кэш, LLM, очереди)
    """
    logger.debug("Запрос статуса системы")

    settings = get_settings()

    return SystemStatus(
        service="kag-api",
        version=settings.APP_VERSION,
        status="running",
        uptime=0.0,
        components={
            "api": {"status": "ok"},
            "qdrant": {"status": "unknown"},
            "redis": {"status": "unknown"},
            "celery": {"status": "unknown"},
            "keycloak": {"status": "unknown"}
        }
    )


@router.get("/dependencies", summary="Зависимости (SBOM)")
async def get_dependencies():
    """
    Получить Software Bill of Materials (SBOM).
    
    Возвращает список всех зависимостей с версиями,
    сгенерированный через syft.
    """
    logger.debug("Запрос SBOM")
    
    # TODO: Загрузить реальный SBOM из файла
    
    return {
        "sbom_version": "1.0",
        "generated_at": datetime.utcnow().isoformat(),
        "dependencies": [
            {"name": "fastapi", "version": "0.115.6"},
            {"name": "pydantic", "version": "2.10.4"},
            {"name": "celery", "version": "5.4.0"},
            {"name": "redis", "version": "5.2.1"},
            {"name": "qdrant-client", "version": "1.12.1"},
        ]
    }


@router.get("/metrics", summary="Метрики производительности")
async def get_metrics():
    """
    Получить метрики производительности системы.
    
    Включает:
    - Количество запросов в секунду
    - Среднее время ответа
    - Использование памяти/CPU
    - Размер векторной БД
    """
    logger.debug("Запрос метрик")
    
    # TODO: Интеграция с Prometheus
    
    return {
        "requests_per_second": 0,
        "avg_response_time_ms": 0,
        "memory_usage_mb": 0,
        "cpu_usage_percent": 0,
        "qdrant_documents": 0
    }


@router.post("/cache/clear", summary="Очистить кэш")
async def clear_cache():
    """
    Очистить весь кэш в Redis.
    
    ВНИМАНИЕ: Это действие временное снизит производительность.
    """
    logger.warning("Запрос на очистку кэша")
    
    # TODO: Реализовать очистку кэша
    
    return {"status": "ok", "message": "Кэш очищен"}


@router.get("/users", summary="Список пользователей")
async def list_users():
    """
    Получить список пользователей (только для администраторов).
    """
    logger.debug("Запрос списка пользователей")
    
    from src.database.session import get_db as _get_db
    from src.database.user_models import User
    
    db_gen = _get_db()
    db = next(db_gen)
    try:
        users = db.query(User).all()
        return {
            "users": [
                {
                    "id": u.id,
                    "username": u.username,
                    "email": u.email,
                    "is_admin": u.is_admin,
                    "is_active": u.is_active,
                    "created_at": u.created_at.isoformat() if u.created_at else None
                }
                for u in users
            ]
        }
    finally:
        db.close()


@router.get("/groups", summary="Список групп")
async def list_groups():
    """Получить список групп."""
    from src.database.session import get_db as _get_db
    from src.database.user_models import Group
    
    db_gen = _get_db()
    db = next(db_gen)
    try:
        groups = db.query(Group).all()
        return {
            "groups": [
                {"id": g.id, "name": g.name, "description": g.description}
                for g in groups
            ]
        }
    finally:
        db.close()


@router.post("/groups", summary="Создать группу")
async def create_group(data: dict):
    """Создать новую группу. Body: {name, description}"""
    from src.database.session import get_db as _get_db
    from src.database.user_models import Group
    import uuid as _uuid
    
    name = data.get("name", "")
    description = data.get("description", "")
    
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    
    db_gen = _get_db()
    db = next(db_gen)
    try:
        group = Group(id=str(_uuid.uuid4()), name=name, description=description)
        db.add(group)
        db.commit()
        return {"id": group.id, "name": group.name, "description": group.description}
    finally:
        db.close()


@router.get("/ad-config", summary="Конфигурация Active Directory")
async def get_ad_config():
    """Получить настройки AD/LDAP."""
    cfg = config_store.get("ad", "default") or {}
    # Don't return encrypted password
    return {
        "ldap_url": cfg.get("ldap_url", ""),
        "base_dn": cfg.get("base_dn", ""),
        "users_dn": cfg.get("users_dn", ""),
        "bind_dn": cfg.get("bind_dn", ""),
        "domain": cfg.get("domain", ""),
        "enabled": cfg.get("enabled", False),
        "configured": bool(cfg.get("ldap_url"))
    }


@router.post("/ad-config", summary="Сохранить конфигурацию AD")
async def save_ad_config(data: dict):
    """Сохранить настройки AD. Пароль шифруется в config_store."""
    from src.security.gost_crypto import GOSTCrypto
    crypto = GOSTCrypto()
    
    cfg = {
        "ldap_url": data.get("ldap_url", ""),
        "base_dn": data.get("base_dn", ""),
        "users_dn": data.get("users_dn", ""),
        "bind_dn": data.get("bind_dn", ""),
        "domain": data.get("domain", ""),
        "enabled": data.get("enabled", False),
    }
    
    # Encrypt bind password if provided
    if data.get("bind_password"):
        cfg["bind_password"] = crypto.encrypt_to_base64(data["bind_password"])
    
    config_store.set("ad", "default", cfg)
    
    # If enabled, configure Keycloak LDAP federation
    if cfg["enabled"] and cfg["ldap_url"]:
        try:
            result = await _configure_keycloak_ldap(cfg, data.get("bind_password", ""))
            return {"success": True, "keycloak": result}
        except Exception as e:
            return {"success": True, "warning": f"Сохранено, но Keycloak не настроен: {e}"}
    
    return {"success": True}


@router.post("/ad-config/test", summary="Проверить подключение к AD")
async def test_ad_connection(data: dict):
    """Проверить LDAP подключение."""
    try:
        import ldap3
        server = ldap3.Server(data.get("ldap_url", ""), get_info=ldap3.ALL)
        conn = ldap3.Connection(
            server,
            user=data.get("bind_dn", ""),
            password=data.get("bind_password", ""),
            auto_bind=True
        )
        # Search for users
        conn.search(
            data.get("users_dn", "CN=Users,DC=company,DC=local"),
            "(objectClass=user)",
            attributes=['sAMAccountName', 'mail', 'displayName']
        )
        users = [{"username": e.sAMAccountName.value, "email": e.mail.value, "name": e.displayName.value}
                 for e in conn.entries[:10]]
        conn.unbind()
        return {"success": True, "users_found": len(users), "sample": users[:5]}
    except ImportError:
        return {"success": False, "message": "ldap3 не установлен"}
    except Exception as e:
        return {"success": False, "message": str(e)}


async def _configure_keycloak_ldap(cfg: dict, bind_password: str):
    """Configure Keycloak LDAP federation via REST API."""
    import httpx
    from src.config import get_settings
    settings = get_settings()
    
    kc_url = "http://keycloak:8080"
    realm = settings.KEYCLOAK_REALM
    
    # Get admin token
    async with httpx.AsyncClient(timeout=10) as client:
        token_resp = await client.post(
            f"{kc_url}/realms/master/protocol/openid-connect/token",
            data={
                "client_id": "admin-cli",
                "username": "admin",
                "password": settings.KEYCLOAK_ADMIN_PASSWORD,
                "grant_type": "password"
            }
        )
        admin_token = token_resp.json().get("access_token")
        
        # Create LDAP federation component
        ldap_component = {
            "name": "ad-ldap",
            "providerId": "ldap",
            "providerType": "org.keycloak.storage.UserStorageProvider",
            "parentId": realm,
            "config": {
                "enabled": ["true"],
                "priority": ["1"],
                "fullSyncPeriod": ["-1"],
                "changedSyncPeriod": ["-1"],
                "cachePolicy": ["DEFAULT"],
                "batchSizeForSync": ["1000"],
                "editMode": ["READ_ONLY"],
                "importEnabled": ["true"],
                "syncRegistrations": ["false"],
                "vendor": ["ad"],
                "usernameLDAPAttribute": ["sAMAccountName"],
                "rdnLDAPAttribute": ["sAMAccountName"],
                "uuidLDAPAttribute": ["objectGUID"],
                "userObjectClasses": ["person, organizationalPerson, user"],
                "connectionUrl": [cfg["ldap_url"]],
                "usersDn": [cfg.get("users_dn", "")],
                "authType": ["simple"],
                "bindDn": [cfg.get("bind_dn", "")],
                "bindCredential": [bind_password],
                "searchScope": ["2"],
                "validatePasswordPolicy": ["false"],
                "trustEmail": ["false"],
                "useTruststoreSPI": ["ldapsOnly"],
                "connectionPooling": ["true"],
                "pagination": ["true"],
            }
        }
        
        create_resp = await client.post(
            f"{kc_url}/admin/realms/{realm}/components",
            json=ldap_component,
            headers={"Authorization": f"Bearer {admin_token}"}
        )
        
        if create_resp.status_code in (201, 200, 409):
            return "Keycloak LDAP federation configured"
        else:
            raise Exception(f"Keycloak error: {create_resp.status_code} {create_resp.text}")


@router.get("/audit-log", summary="Журнал аудита")
async def get_audit_log(
    limit: Optional[int] = 100,
    user: Optional[str] = None,
    action: Optional[str] = None
):
    """
    Получить журнал аудита действий.
    
    - **limit**: Лимит записей
    - **user**: Фильтр по пользователю
    - **action**: Фильтр по действию
    """
    logger.debug("Запрос журнала аудита")
    
    # TODO: Получить записи из Loki
    
    return {"entries": []}


@router.get("/disk-usage", summary="Использование диска по директориям")
async def get_disk_usage():
    """Детальная информация об использовании дискового пространства."""
    import subprocess
    import os
    
    result = {"disks": [], "directories": [], "docker_volumes": []}
    
    # df -h
    try:
        out = subprocess.check_output(["df", "-h", "--type=ext4", "--type=xfs", "--type=btrfs", "--type=overlay"], timeout=5).decode()
        for line in out.strip().split("\n")[1:]:
            parts = line.split()
            if len(parts) >= 6:
                result["disks"].append({
                    "filesystem": parts[0],
                    "size": parts[1],
                    "used": parts[2],
                    "available": parts[3],
                    "use_pct": parts[4],
                    "mountpoint": parts[5]
                })
    except Exception:
        pass
    
    # du по ключевым директориям
    dirs_to_check = ["/app/data", "/app/user_data", "/home", "/var/lib/docker", "/var/log", "/tmp"]
    for d in dirs_to_check:
        try:
            if os.path.exists(d):
                out = subprocess.check_output(["du", "-sh", d], timeout=10, stderr=subprocess.DEVNULL).decode()
                size = out.split()[0] if out else "?"
                result["directories"].append({"path": d, "size": size})
        except Exception:
            pass
    
    # Подробно по /app/data
    if os.path.exists("/app/data"):
        try:
            out = subprocess.check_output(["du", "-sh", "/app/data/*"], timeout=10, stderr=subprocess.DEVNULL, shell=True).decode()
            for line in out.strip().split("\n"):
                if line.strip():
                    parts = line.split()
                    if len(parts) >= 2:
                        result["directories"].append({"path": parts[1], "size": parts[0]})
        except Exception:
            pass
    
    # Docker volumes через docker (если доступен)
    try:
        out = subprocess.check_output(["docker", "system", "df", "-v"], timeout=5, stderr=subprocess.DEVNULL).decode()
        # Парсим вывод docker system df
        result["docker_raw"] = out[:2000]
    except Exception:
        result["docker_raw"] = "Docker недоступен из контейнера"
    
    return result
