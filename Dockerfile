# ===========================================
# Stage 1: Build - установка зависимостей
# ===========================================
FROM python:3.11-slim as builder

# Установка системных зависимостей для сборки
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    git \
    openssh-client \
    sshpass \
    # OCR: Tesseract + русский язык + poppler для pdf2image
    tesseract-ocr \
    tesseract-ocr-rus \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# Создание виртуального окружения
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Копирование файлов зависимостей
COPY requirements.txt .

# Установка Python зависимостей
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ===========================================
# Stage 2: Production - финальный образ
# ===========================================
FROM python:3.11-slim as production

# Метки для Docker Hub
LABEL maintainer="KAG Team"
LABEL description="KAG - Knowledge Augmentation Generation. AI-powered knowledge management with RAG."
LABEL version="0.3.0"
LABEL org.opencontainers.image.source="https://github.com/your-org/kag"
LABEL org.opencontainers.image.description="KAG - AI-powered knowledge management system with RAG, Ollama integration, and document processing"

# Установка системных зависимостей для runtime
RUN apt-get update && apt-get install -y \
    curl \
    openssh-client \
    sshpass \
    # OCR: Tesseract + русский язык + poppler для pdf2image
    tesseract-ocr \
    tesseract-ocr-rus \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# Копирование виртуального окружения из builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Создание непривилегированного пользователя
RUN groupadd -r kag && useradd -r -g kag -d /app -s /sbin/nologin kag

# Установка рабочей директории
WORKDIR /app

# Копирование исходного кода
COPY --chown=kag:kag src/ /app/src/

# Добавление пользователя kag в группу docker для доступа к сокету
RUN groupadd -f docker && usermod -aG docker kag

# Создание директорий для данных
RUN mkdir -p /app/data/audit /app/data/annotations /app/data/quality_tracking /app/data/ab_tests /app/.ssh && \
    chmod -R 777 /app/data && \
    chmod 700 /app/.ssh && \
    chown -R kag:kag /app/data

# Переменные окружения
ENV PYTHONPATH=/app
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PATH="/opt/venv/bin:$PATH"

# Порт приложения
EXPOSE 8000

# Переключение на непривилегированного пользователя
USER kag

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8000/api/v1/health || exit 1

# Команда запуска
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
