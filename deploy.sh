#!/bin/bash
# Самоисправление CRLF: если скрипт приехал с Windows-окончаниями строк (\r),
# убираем их и перезапускаемся — иначе heredoc сгенерит .env с \r в паролях.
if grep -q "$(printf '\r')" "$0" 2>/dev/null; then
    sed -i 's/\r$//' "$0"
    echo "deploy.sh: исправлены CRLF-окончания, перезапускаю..."
    exec bash "$0" "$@"
fi
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
QDRANT_API_KEY=$(generate_password)

# ── Нейросети: переопределяются при запуске (иначе дефолты) ──────────────
# Подключение после развёртывания:
#   OLLAMA_BASE_URL=http://ollama-host:11434 \
#   EMBEDDING_BASE_URL=http://emb-host:8090/v1 \
#   bash deploy.sh
OLLAMA_BASE_URL=${OLLAMA_BASE_URL:-http://192.168.50.41:11434}
OLLAMA_MODEL=${OLLAMA_MODEL:-phi4-mini:latest}
EMBEDDING_BASE_URL=${EMBEDDING_BASE_URL:-http://192.168.50.42:8090/v1}
EMBEDDING_MODEL=${EMBEDDING_MODEL:-Embeddings}
EMBEDDING_DIMENSIONS=${EMBEDDING_DIMENSIONS:-1024}

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

# Ollama (LLM)
OLLAMA_BASE_URL=${OLLAMA_BASE_URL}
OLLAMA_MODEL=${OLLAMA_MODEL}
LLM_OLLAMA_ENABLED=true

# Embedding
EMBEDDING_BASE_URL=${EMBEDDING_BASE_URL}
EMBEDDING_MODEL=${EMBEDDING_MODEL}
EMBEDDING_DIMENSIONS=${EMBEDDING_DIMENSIONS}

# GigaChat (Сбер) — через прокси gpt2giga (контейнер gigachat-proxy:8090).
# Ключ авторизации GigaChat. Пустой = ключ вводится в админке (pass-token).
GIGACHAT_CREDENTIALS=${GIGACHAT_CREDENTIALS:-}
GIGACHAT_SCOPE=${GIGACHAT_SCOPE:-GIGACHAT_API_PERS}
GIGACHAT_MODEL=${GIGACHAT_MODEL:-GigaChat-Max}

# Qdrant
QDRANT_API_KEY=${QDRANT_API_KEY}

# Прочее
QDRANT_PORT=6333
REDIS_PORT=6379
FASTAPI_DEBUG=false
ENVFILE

    echo "Created .env with generated passwords"
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
    echo "Qdrant API key:          ${QDRANT_API_KEY}"
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

# Права на bind-mount ./data: api теперь запускается от непривилегированного
# пользователя kag (USER kag в Dockerfile). Docker создаёт ./data от root —
# без этого шага kag не сможет писать (uploads/thumbnails/ocr).
mkdir -p data/uploads data/thumbnails data/ocr_results
chmod -R 777 data 2>/dev/null || true

if [ ! -f .env.before_deploy ]; then
    # Первый деплой — собираем образы
    echo "First deploy: building images..."
    docker-compose build --pull 2>&1
    touch .env.before_deploy
else
    # Повторный деплой — только запуск, без пересборки
    echo "Re-deploy: using existing images, no build"
fi
docker-compose up -d --no-build 2>&1

echo ""
echo "=== Waiting for API (через nginx) ==="
for i in $(seq 1 30); do
    if curl -s -o /dev/null http://localhost/setup; then
        echo "API ready (через nginx)!"
        break
    fi
    sleep 5
done

echo ""
echo "=== Deploy complete ==="
echo "Сайт:     http://<host>/setup  ->  Initialize ALL"
echo "HTTPS:    https://<host>/  (самоподписанный сертификат, сгенерирован выше)"
echo "Вход:     admin / ${ADMIN_PASSWORD}"
echo "Внимание: api:8000 НЕ публикуется наружу, весь трафик идёт через nginx (80/443)."
