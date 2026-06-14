#!/bin/bash
set -e

# ===========================================================
# KAG — РАЗВЁРТЫВАНИЕ С НУЛЯ
# ===========================================================
# Использование:
#   curl -sL https://raw.githubusercontent.com/YartsevNS/.../deploy.sh | bash
# ИЛИ вручную:
#   git clone https://github.com/YartsevNS/kag-system-enhanced.git
#   cd kag-system-enhanced
#   bash deploy.sh
# ===========================================================

echo "=== KAG Deployment ==="

# 1. Clone если не склонировано
if [ ! -f docker-compose.yml ]; then
    echo "Cloning repository..."
    git clone https://github.com/YartsevNS/kag-system-enhanced.git kag-system
    cd kag-system
fi

echo "=== 1/5 Creating .env ==="
if [ ! -f .env ]; then
    cat > .env << 'ENVFILE'
POSTGRES_PASSWORD=kagpass123
DB_NAME=kag
DB_USER=kag
DB_PASSWORD=kagpass123
KEYCLOAK_ADMIN=admin
KEYCLOAK_ADMIN_PASSWORD=KAGadmin2026
OLLAMA_BASE_URL=http://192.168.50.41:11434
OLLAMA_MODEL=phi4-mini:latest
LLM_OLLAMA_ENABLED=true
NEO4J_PASSWORD=kagneo4j2026
QDRANT_PORT=6333
REDIS_PORT=6379
CELERY_BROKER_URL=redis://redis:6379/1
CELERY_RESULT_BACKEND=redis://redis:6379/3
EMBEDDING_BASE_URL=http://192.168.50.41:11434
EMBEDDING_MODEL=nomic-embed-text:latest
EMBEDDING_DIMENSIONS=1024
ENVFILE
    echo "Created .env (edit for your Ollama IP)"
fi

echo "=== 2/5 Creating SSL certs ==="
mkdir -p docker/nginx/ssl
if [ ! -f docker/nginx/ssl/kag.key ]; then
    openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
        -keyout docker/nginx/ssl/kag.key \
        -out docker/nginx/ssl/kag.crt \
        -subj '/CN=kag.local' 2>/dev/null
fi

echo "=== 3/5 Creating Docker volumes ==="
docker volume create --name kag_pg_data 2>/dev/null || true
docker network create kag-system_kag_internal 2>/dev/null || true

echo "=== 4/5 Starting containers ==="
KAG_DB_URL=postgresql://kag:kagpass123@kag-pg:5432/kag \
docker-compose -f docker-compose.yml -f docker-compose.pg.yml --profile dev up -d

echo "=== 5/5 Waiting for API ==="
echo "Waiting for API to be ready..."
for i in $(seq 1 30); do
    if curl -s -o /dev/null http://localhost:8000/setup; then
        echo "API ready!"
        break
    fi
    sleep 5
done

echo ""
echo "=========================================="
echo "DEPLOYMENT COMPLETE"
echo "=========================================="
echo "Open: http://$(hostname -I 2>/dev/null | awk '{print $1}'):8000/setup"
echo "Or:  http://localhost:8000/setup"
echo ""
echo "Then click: Initialize ALL -> Copy creds -> Дальше"
echo ""
echo "Login: admin / admin123456"
echo "=========================================="
