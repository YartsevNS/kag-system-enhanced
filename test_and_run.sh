#!/bin/bash
# Скрипт проверки и запуска KAG проекта

set -e

echo "========================================="
echo "  KAG - Проверка и запуск"
echo "========================================="
echo ""

# Цвета
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Функция проверки
check() {
    if [ $1 -eq 0 ]; then
        echo -e "${GREEN}✅ $2${NC}"
    else
        echo -e "${RED}❌ $2${NC}"
        FAILED=$((FAILED + 1))
    fi
}

FAILED=0

echo "📋 Шаг 1: Проверка окружения"
echo "-----------------------------------------"

# Проверка Docker
command -v docker &> /dev/null
check $? "Docker установлен"

# Проверка Docker Compose
docker compose version &> /dev/null
check $? "Docker Compose установлен"

# Проверка Python
command -v python3 &> /dev/null
check $? "Python3 установлен"

echo ""
echo "📋 Шаг 2: Проверка файлов проекта"
echo "-----------------------------------------"

# Проверка ключевых файлов
test -f docker-compose.yml
check $? "docker-compose.yml"

test -f .env
check $? ".env файл"

test -f src/api/main.py
check $? "src/api/main.py"

test -f src/llm/__init__.py
check $? "src/llm/__init__.py"

test -f src/llm/embeddings.py
check $? "src/llm/embeddings.py"

test -f src/api/services/chat_service.py
check $? "src/api/services/chat_service.py"

test -f src/api/services/model_manager.py
check $? "src/api/services/model_manager.py"

test -f src/api/routes/admin_models.py
check $? "src/api/routes/admin_models.py"

test -f src/indexing/embeddings_service.py
check $? "src/indexing/embeddings_service.py"

echo ""
echo "📋 Шаг 3: Проверка подключения к Ollama"
echo "-----------------------------------------"

# Проверка доступности сервера Ollama
OLLAMA_URL="http://192.168.50.41:11434"
curl -s --connect-timeout 5 "$OLLAMA_URL/api/tags" > /dev/null 2>&1
check $? "Ollama сервер доступен ($OLLAMA_URL)"

if [ $? -eq 0 ]; then
    echo "  Доступные модели:"
    curl -s "$OLLAMA_URL/api/tags" 2>/dev/null | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    for model in data.get('models', [])[:5]:
        print(f\"    • {model['name']} ({model['details'].get('parameter_size', 'N/A')})\")
except:
    print('    Не удалось получить список моделей')
" 2>/dev/null || echo "    Не удалось получить список моделей"
fi

echo ""
echo "📋 Шаг 4: Проверка синтаксиса Python"
echo "-----------------------------------------"

python3 -m py_compile src/llm/embeddings.py 2>/dev/null
check $? "src/llm/embeddings.py"

python3 -m py_compile src/llm/vllm_client.py 2>/dev/null
check $? "src/llm/vllm_client.py"

python3 -m py_compile src/llm/ollama_client.py 2>/dev/null
check $? "src/llm/ollama_client.py"

python3 -m py_compile src/llm/openai_client.py 2>/dev/null
check $? "src/llm/openai_client.py"

python3 -m py_compile src/llm/router.py 2>/dev/null
check $? "src/llm/router.py"

python3 -m py_compile src/indexing/embeddings_service.py 2>/dev/null
check $? "src/indexing/embeddings_service.py"

python3 -m py_compile src/api/services/chat_service.py 2>/dev/null
check $? "src/api/services/chat_service.py"

python3 -m py_compile src/api/services/model_manager.py 2>/dev/null
check $? "src/api/services/model_manager.py"

python3 -m py_compile src/api/routes/admin_models.py 2>/dev/null
check $? "src/api/routes/admin_models.py"

python3 -m py_compile src/api/main.py 2>/dev/null
check $? "src/api/main.py"

echo ""
echo "========================================="
if [ $FAILED -eq 0 ]; then
    echo -e "${GREEN}✅ Все проверки пройдены!${NC}"
    echo ""
    echo "🚀 Для запуска выполните:"
    echo ""
    echo "  docker compose --profile dev up -d"
    echo ""
    echo "📡 После запуска:"
    echo "  • API:         http://localhost:8000"
    echo "  • Health:      http://localhost:8000/api/v1/health"
    echo "  • Docs:        http://localhost:8000/docs"
    echo "  • Admin Models:http://localhost:8000/api/v1/admin/models/admin"
    echo ""
else
    echo -e "${YELLOW}⚠️  Пройдено не все проверки ($FAILED ошибок)${NC}"
    echo ""
    echo "Для запуска нужны:"
    echo "  • Docker и Docker Compose"
    echo "  • Доступ к серверу Ollama: 192.168.50.41:11434"
fi
echo "========================================="
