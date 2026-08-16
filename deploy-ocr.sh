#!/bin/bash
set -e
echo "=== KAG-OCR Deployment ==="

generate_password() { openssl rand -base64 24 | tr -d '/+=' | cut -c1-24; }

if [ ! -f .env ]; then
    JWT_SECRET=$(openssl rand -base64 32 | tr -d '/+=' | cut -c1-32)
    NEO4J_PASSWORD=$(generate_password)
    KAG_DB_PASSWORD=$(generate_password)

    cat > .env << ENVFILE
JWT_SECRET=${JWT_SECRET}
KAG_DB_URL=postgresql://kag:${KAG_DB_PASSWORD}@kag-db:5432/kag
NEO4J_PASSWORD=${NEO4J_PASSWORD}
POSTGRES_PASSWORD=${KAG_DB_PASSWORD}
OLLAMA_BASE_URL=http://192.168.50.41:11434
EMBEDDING_MODEL=nomic-embed-text:latest
ENVFILE

    echo "=== CREDENTIALS ==="
    echo "JWT_SECRET: ${JWT_SECRET}"
    echo "KAG DB: kag / ${KAG_DB_PASSWORD}"
    echo "Neo4j: neo4j / ${NEO4J_PASSWORD}"
    echo "===================="
else
    echo ".env exists, using existing"
fi

echo "=== Building ==="
docker-compose build 2>&1

echo "=== Starting ==="
docker network create kag_internal 2>/dev/null || true
docker-compose up -d

echo "=== Waiting for API ==="
for i in $(seq 1 30); do
    if curl -s -o /dev/null http://localhost:8000/api/v1/health; then echo "API ready!"; break; fi
    sleep 3
done
echo "http://localhost:8000/docs"
