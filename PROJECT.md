# KAG-System-Enhanced — Логика проекта

## 1. Архитектура

### Сервисы (docker-compose.yml)
- **api** — FastAPI, порт 8000
- **worker** — Celery worker для индексации
- **mcp-server** — MCP протокол, порт 8001
- **qdrant** — векторная БД (порты 6333/6334)
- **redis** — кэш/брокер (порт 6379)
- **kag-db** — PostgreSQL 16 (порт 5432)
- **neo4j** — граф знаний (порты 7474/7687)
- **keycloak** — IdP (порт 8080)
- **nginx** — обратный прокси (порты 80/443)
- **scheduler** — планировщик задач

### OCR-система (иерархия)
1. **Основная: Occular OCR** — https://github.com/Bodhi42/Occular-ocr
   - Для сложных PDF/сканов
   - Модели: crnn_encoder.onnx, crnn_mobilenet_large.pth
   - Использует onnxruntime
2. **Вспомогательная: Tesseract OCR** — pytesseract + tesseract-ocr-rus
   - fallback для простых случаев
3. **PyPDF2 / PyMuPDF** — для текстовых PDF (без OCR)

### Базы данных
- PostgreSQL (kag-db) — основные данные + keycloak
- Qdrant — векторные эмбеддинги
- Neo4j — граф знаний
- Redis — кэш + Celery broker

## 2. Процесс развёртывания (deploy)
1. Генерация уникальных паролей для каждого деплоя
2. Запись в .env
3. Запуск контейнеров (docker-compose up -d)
4. Вывод credentials пользователю

Скрипт: `deploy.sh` (в корне проекта)

## 3. Переменные окружения (.env)

| Переменная | Описание | Где используется |
|---|---|---|
| JWT_SECRET | Подпись JWT | api |
| NEO4J_PASSWORD | Пароль Neo4j | neo4j, api, worker |
| KAG_DB_URL | URL PostgreSQL | api, worker |
| KC_DB_PASSWORD | Пароль БД keycloak | keycloak, kag-db |
| KEYCLOAK_ADMIN_PASSWORD | Пароль admin keycloak | keycloak |
| KEYCLOAK_CLIENT_SECRET | Секрет клиента | keycloak |
| OLLAMA_BASE_URL | URL Ollama сервера | api, worker |
| EMBEDDING_MODEL | Модель эмбеддингов | worker |

## 4. Текущие задачи

- [ ] Развернуть KAG-систему на сервере 192.168.50.18
- [ ] Настроить DNS на сервере
- [ ] Протестировать все сервисы после деплоя
- [ ] Интегрировать Occular OCR полностью
- [ ] Настроить мониторинг

## 5. Сервер
- Внутренний: 192.168.50.18 (ssh yartsevn@, ключ id_ed25519)
- Внешний: 7a6707a132c4.sn.mynetname.net
- Домен: qd.gostsecret.ru → 185.229.9.179
- SUDO пароль: Adminmano569!
