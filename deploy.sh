#!/bin/bash
set -e
echo "=== KAG Deployment ==="

if [ ! -f docker-compose.yml ]; then
    echo "Cloning repository..."
    git clone https://github.com/YartsevNS/kag-system-enhanced.git kag-system
    cd kag-system
fi

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
KAG_DB_URL=postgresql://kag:kagpass123@kag-db:5432/kag
ENVFILE
    echo "Created .env"
fi

mkdir -p docker/nginx/ssl
if [ ! -f docker/nginx/ssl/kag.key ]; then
    openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
        -keyout docker/nginx/ssl/kag.key \
        -out docker/nginx/ssl/kag.crt \
        -subj '/CN=kag.local' 2>/dev/null
fi

echo "=== Starting containers ==="
docker network create kag-system_kag_internal 2>/dev/null || true
docker-compose --profile dev up -d

echo "=== Waiting for API ==="
for i in $(seq 1 30); do
    if curl -s -o /dev/null http://localhost:8000/setup; then
        echo "API ready!"
        break
    fi
    sleep 5
done

echo ""
echo "Open http://localhost:8000/setup -> Initialize ALL"
echo "Login: admin / admin123456"
