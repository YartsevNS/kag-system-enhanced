# ===========================================
# kag-base — ОБЩИЙ базовый образ для api / worker / mcp
# ===========================================
# Зачем: api и worker почти идентичны по слоям (python, apt, venv,
# requirements, Occular, Playwright) и собирались независимо — слои
# дублировались в registry и при передаче.
#
# Теперь тяжёлая часть живёт здесь, а kag-api / kag-worker / kag-mcp —
# тонкие образы (FROM kag-base + COPY src + свой CMD), собираются за секунды.
#
# Сборка:
#   docker build -t kre44et/kag-base:2026.09.07 -f docker/base/Dockerfile .
#   docker push kre44et/kag-base:2026.09.07
#
# ВАЖНО (оптимизация размера, урок):
#  - chown -R по УЖЕ СОЗДАННЫМ каталогам выносим в ТОТ ЖЕ RUN, где каталог
#    наполняется. Отдельный слой с chown заставляет overlayfs копировать
#    файлы наверх (copy-up) и удваивает их вес (~1 ГБ на этом образе).
#  - Playwright браузер кладём в /opt/ms-playwright (внутри образа), а не в
#    /root/.cache, и задаём PLAYWRIGHT_BROWSERS_PATH — иначе в рантайме
#    браузер искался бы по пути bind-mount и не находился.

FROM python:3.11-slim

# Зеркало Debian (mirror.yandex.ru): deb.debian.org (Fastly CDN) недоступен
# из РФ-сети — apt-get update падает с "Unable to connect".
RUN sed -i 's|deb.debian.org|mirror.yandex.ru|g' /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list 2>/dev/null || true

# Системные зависимости ОДНИМ слоем + непривилегированный пользователь.
# git нужен только для pip install git+...Occular — удаляется в конце.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    openssh-client \
    sshpass \
    tesseract-ocr \
    tesseract-ocr-rus \
    tesseract-ocr-eng \
    poppler-utils \
    libgl1 \
    libglib2.0-0t64 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r kag && useradd -r -g kag -d /app -s /sbin/nologin kag \
    && mkdir -p /app/.cache/huggingface /app/.cache/ms-playwright /opt/venv \
    && chown -R kag:kag /app /opt/venv

# Базовые env. Заданы ДО установок — чтобы кэши/браузер легли в постоянные
# пути внутри образа и были доступны пользователю kag.
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/app/.cache/huggingface \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

WORKDIR /app

COPY requirements.txt .

# Python-зависимости + Occular (--no-deps: setup.py тянет torch+CUDA ~3.5 ГБ,
# а OCR работает на onnxruntime) + pyctcdecode. chown — в этом же слое.
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir --no-deps git+https://github.com/Bodhi42/Occular-ocr.git && \
    pip install --no-cache-dir pyctcdecode && \
    chown -R kag:kag /opt/venv

# Playwright Chromium: pip-пакет уже из requirements; здесь системные deps
# (apt, нужен root) и сам браузер в PLAYWRIGHT_BROWSERS_PATH. chown — тут же.
# Нужен и api, и worker: src/api/services/web_monitor.py импортирует playwright.
RUN python -m playwright install --with-deps chromium && \
    chown -R kag:kag /opt/ms-playwright && \
    rm -rf /root/.cache /var/lib/apt/lists/*

# Предзагрузка весов Occular с HuggingFace (Shivin11/occular-ocr).
# raw.githubusercontent.com (Fastly) из РФ недоступен; пакет 0.3.0 переименован
# occular, веса на HF. reading_order раскладывается в occular/weights/reading_order/.
# chown кэша — в этом же слое.
RUN python -c "from huggingface_hub import hf_hub_download as d; r='Shivin11/occular-ocr'; \
[d(r,f) for f in ['detector_dbnet_fp32.onnx','recognizer_svtr_fp32.onnx','recognizer_svtr_cyr12_fp32.onnx','recognizer_charset.txt','recognizer_charset_cyr12.txt','table_detect_v3_fp32.onnx','table_struct_split_v2_fp32.onnx']]; \
from occular import download_reading_order; download_reading_order(); \
print('Occular weights preloaded')" && \
    chown -R kag:kag /app/.cache

# git и его зависимости больше не нужны — чистим (без chown в этом слое,
# иначе overlayfs снова скопирует файлы наверх).
RUN apt-get purge -y git && apt-get autoremove -y && rm -rf /var/lib/apt/lists/* /root/.cache

LABEL maintainer="KAG Team"
LABEL description="KAG shared base image (python + OCR + Playwright) for api/worker/mcp"
LABEL version="2026.09.07"
