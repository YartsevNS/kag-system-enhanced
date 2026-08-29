# ===========================================
# Stage 1: Build - установка зависимостей
# ===========================================
FROM python:3.11-slim as builder

# Зеркало Debian (mirror.yandex.ru): deb.debian.org (Fastly CDN) недоступен из
# РФ-сети — apt-get update падает с "Unable to connect". Заменяем до первого update.
RUN sed -i 's|deb.debian.org|mirror.yandex.ru|g' /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list 2>/dev/null || true

# Установка системных зависимостей для сборки
RUN apt-get update && apt-get install -y \
    curl \
    git \
    openssh-client \
    sshpass \
    # OCR: Tesseract + русский язык + poppler для pdf2image
    tesseract-ocr \
    tesseract-ocr-rus \
    poppler-utils \
    # OpenCV/GL для Occular-ocr
    libgl1 \
    libglib2.0-0t64 \
    && rm -rf /var/lib/apt/lists/*

# Создание виртуального окружения
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Копирование файлов зависимостей
COPY requirements.txt .

# Установка Python зависимостей
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    # Occular-ocr — БЕЗ зависимостей (--no-deps): setup.py тянет torch+CUDA (3.5GB),
    # а torch/api-образу не нужен. Зависимости Occular (onnxruntime, opencv,
    # pyclipper, shapely, pymupdf, huggingface_hub) уже в requirements.txt.
    pip install --no-cache-dir --no-deps git+https://github.com/Bodhi42/Occular-ocr.git && \
    # pyctcdecode — зависимость Occular (CTC-декодирование), не ставится с --no-deps
    pip install --no-cache-dir pyctcdecode

# Предзагрузка весов Occular с HuggingFace (Shivin11/occular-ocr).
# raw.githubusercontent.com (Fastly CDN) недоступен из РФ-сети, а старый путь
# ocr_skel/weights/*.onnx устарел — пакет 0.3.0 переименован occular, веса на HF.
# reading_order кладём через download_reading_order() — она раскладывает в
# occular/weights/reading_order/ (reading_order_ready() проверяет именно локальный путь).
RUN python -c "from huggingface_hub import hf_hub_download as d; r='Shivin11/occular-ocr'; \
[d(r,f) for f in ['detector_dbnet_fp32.onnx','recognizer_svtr_fp32.onnx','recognizer_svtr_cyr12_fp32.onnx','recognizer_charset.txt','recognizer_charset_cyr12.txt','table_detect_v3_fp32.onnx','table_struct_split_v2_fp32.onnx']]; \
from occular import download_reading_order; download_reading_order(); \
print('Occular weights preloaded')"

# ===========================================
# Stage 2: Production - финальный образ
# ===========================================
FROM python:3.11-slim as production

# Зеркало Debian (mirror.yandex.ru) — deb.debian.org недоступен из РФ-сети.
RUN sed -i 's|deb.debian.org|mirror.yandex.ru|g' /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list 2>/dev/null || true

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
    # OpenCV/GL для Occular-ocr
    libgl1 \
    libglib2.0-0t64 \
    && rm -rf /var/lib/apt/lists/*

# Копирование виртуального окружения из builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Создание непривилегированного пользователя
RUN groupadd -r kag && useradd -r -g kag -d /app -s /sbin/nologin kag && \
    mkdir -p /opt/venv/lib/python3.11/site-packages/rapidocr/models && \
    mkdir -p /app/.cache/huggingface && \
    chown -R kag:kag /opt/venv/lib/python3.11/site-packages/rapidocr && \
    chown -R kag:kag /opt/venv/lib/python3.11/site-packages/ocr_skel/weights && \
    chown -R kag:kag /app/.cache

# Установка рабочей директории
WORKDIR /app

# Копирование исходного кода
COPY --chown=kag:kag src/ /app/src/

# Создание директорий для данных
RUN mkdir -p /app/data/audit /app/data/annotations /app/data/quality_tracking /app/data/ab_tests /app/.ssh && \
    chmod -R 750 /app/data && \
    chmod 700 /app/.ssh && \
    chown -R kag:kag /app/data

# Переменные окружения
ENV PYTHONPATH=/app
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV HF_HOME=/app/.cache/huggingface
ENV PATH="/opt/venv/bin:$PATH"

# Порт приложения
EXPOSE 8000

# Health check (Python urllib - не требует curl)
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/v1/health')" || exit 1

# Непривилегированный пользователь. Доступ к docker.sock — через group_add
# в docker-compose.yml (gid хостовой docker-группы), не через chmod 666.
# Права на /app/data (bind-mount) обеспечивает deploy.sh (chmod data).
USER kag

CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
