#!/bin/bash
set -e

echo "=== KAG Deployment ==="

# ============================================
# 1. Генерация уникальных паролей (единая точка)
# ============================================
generate_password() {
    openssl rand -base64 24 | tr -d '/+=' | cut -c1-24
}

JWT_SECRET=$(openssl rand -base64 32 | tr -d '/+=' | cut -c1-32)
NEO4J_PASSWORD=$(generate_password)
KEYCLOAK_ADMIN_PASSWORD=$(generate_password)
KC_DB_PASSWORD=$(generate_password)
KAG_DB_PASSWORD=$(generate_password)
ADMIN_PASSWORD=$(generate_password)

# ============================================
# 2. Создание .env
# ============================================
if [ ! -f .env ]; then
    cat > .env << ENVFILE
# === Сгенерировано deploy.sh $(date) ===

# JWT
JWT_SECRET=${JWT_SECRET}

# Базы данных
KAG_DB_URL=postgresql://kag:${KAG_DB_PASSWORD}@kag-db:5432/kag
KAG_DB_PASSWORD=${KAG_DB_PASSWORD}
NEO4J_PASSWORD=${NEO4J_PASSWORD}
KC_DB_USERNAME=keycloak
KC_DB_PASSWORD=${KC_DB_PASSWORD}
POSTGRES_PASSWORD=${KC_DB_PASSWORD}

# Keycloak
KEYCLOAK_ADMIN=admin
KEYCLOAK_ADMIN_PASSWORD=${KEYCLOAK_ADMIN_PASSWORD}
KEYCLOAK_CLIENT_ID=kag-api
KEYCLOAK_CLIENT_SECRET=${JWT_SECRET}
KEYCLOAK_REALM=kag

# Admin (веб-интерфейс KAG)
ADMIN_PASSWORD=${ADMIN_PASSWORD}

# Ollama
OLLAMA_BASE_URL=http://192.168.50.41:11434
OLLAMA_MODEL=phi4-mini:latest
LLM_OLLAMA_ENABLED=true

# Embedding
EMBEDDING_BASE_URL=http://192.168.50.41:11434
EMBEDDING_MODEL=nomic-embed-text:latest

# Прочее
QDRANT_PORT=6333
REDIS_PORT=6379
FASTAPI_DEBUG=false
ENVFILE

    echo "Created .env with generated passwords"
    touch .env.before_deploy
else
    echo ".env exists — не перегенерация, используем существующий. Удали .env для новых паролей"
fi

# ============================================
# 3. Вывод credentials (скачать в txt)
# ============================================
CREDS_FILE="kag-credentials.txt"
{
    echo "=== KAG CREDENTIALS ==="
    echo "Сгенерировано: $(date)"
    echo ""
    echo "JWT_SECRET:              ${JWT_SECRET}"
    echo "KAG DB (kag-db):         kag / ${KAG_DB_PASSWORD}"
    echo "Neo4j:                   neo4j / ${NEO4J_PASSWORD}"
    echo "Keycloak admin:          admin / ${KEYCLOAK_ADMIN_PASSWORD}"
    echo "Keycloak DB (keycloak):  keycloak / ${KC_DB_PASSWORD}"
    echo "KAG Admin (веб):         admin / ${ADMIN_PASSWORD}"
    echo "=============================="
} | tee "$CREDS_FILE"

echo ""
echo "Credentials сохранены в $CREDS_FILE (удали после скачивания!)"
echo ""

# ============================================
# 4. SSL сертификаты (если нет)
# ============================================
mkdir -p docker/nginx/ssl
if [ ! -f docker/nginx/ssl/kag.key ]; then
    openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
        -keyout docker/nginx/ssl/kag.key \
        -out docker/nginx/ssl/kag.crt \
        -subj '/CN=kag.local' 2>/dev/null
    echo "SSL certificate created"
fi

# ============================================
# 5. Запуск контейнеров
# ============================================
echo ""
echo "=== Starting containers ==="
docker network create kag_internal 2>/dev/null || true

if [ ! -f .env.before_deploy ]; then
    # Первый деплой — собираем образы
    echo "First deploy: building images..."
    docker-compose build --pull 2>&1
else
    # Повторный деплой — только запуск, без пересборки
    echo "Re-deploy: using existing images, no build"
fi
docker-compose up -d --no-build 2>&1

echo ""
echo "=== Waiting for API ==="
for i in $(seq 1 30); do
    if curl -s -o /dev/null http://localhost:8000/setup; then
        echo "API ready!"
        break
    fi
    sleep 5
done

echo ""
echo "=== Deploy complete ==="
echo "Open http://localhost:8000/setup -> Initialize ALL"
echo "Login: admin / ${ADMIN_PASSWORD}"
