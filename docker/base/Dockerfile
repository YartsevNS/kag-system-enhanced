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

# Python-зависимости + движок OCR. chown — в этом же слое.
#
# Occular (и его torch ~0,9 ГБ + pyctcdecode) из образа УБРАН: текст и ячейки распознаёт PP-OCRv5
# (RapidOCR/ONNX, Apache-2.0) — либо службой на сервере моделей, либо локально этим же движком.
# Замер 03.10.2026 на одном хосте (накладная): PP-OCRv5 28,0 с и 4 контрольных числа против 43,9 с и
# 3 чисел у Occular; лицензия Apache-2.0 вместо OpenRAIL-M. Локальный движок оставлен запасным
# путём: если служба недоступна, скан всё равно распознаётся, а не теряется.
# Предзагрузка ОБЯЗАНА покрывать оба языка: текст страницы — cyrillic, текст ячеек — eslav.
# Иначе RapidOCR тянет отсутствующие веса в рантайме (modelscope.cn) и в контейнере падает
# с PermissionError, записывая в site-packages: в закрытом контуре путь ячеек станет мёртвым.
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir rapidocr && \
    (pip uninstall -y opencv-python || true) && \
    pip install --no-cache-dir --force-reinstall --no-deps opencv-python-headless==4.12.0.88 && \
    # OpenVINO-провайдер обязан победить на диске: обычный onnxruntime тянут и requirements, и
    # rapidocr/flashrank, а последний установленный пакет перезаписывает каталог модуля (проверено:
    # в первой сборке оказался onnxruntime 1.30.0 без OpenVINO). Ставим OV-сборку ПОСЛЕДНЕЙ и сразу
    # проверяем провайдер — иначе сборка падает, а не теряет ускорение 1,6-2,35x тихо.
    pip install --no-cache-dir --force-reinstall --no-deps onnxruntime-openvino==1.24.1 && \
    python -c "import onnxruntime as ort; \
assert 'OpenVINOExecutionProvider' in ort.get_available_providers(), ort.get_available_providers(); \
print('OpenVINO EP доступен, onnxruntime', ort.__version__)" && \
    python -c "from rapidocr import RapidOCR; from rapidocr.utils.typings import LangRec, ModelType, OCRVersion;\
base={'Rec.ocr_version': OCRVersion.PPOCRV5, 'Rec.lang_type': LangRec.CYRILLIC, 'Rec.model_type': ModelType.MOBILE};\
RapidOCR(params=dict(base)); RapidOCR(params=dict(base, **{'Global.use_det': False}));\
base_eslav=dict(base, **{'Rec.lang_type': LangRec.ESLAV});\
RapidOCR(params=base_eslav); RapidOCR(params=dict(base_eslav, **{'Global.use_det': False}));\
print('PP-OCRv5 weights preloaded: cyrillic + eslav, page + cells')" && \
    chown -R kag:kag /opt/venv

# Playwright Chromium: pip-пакет уже из requirements; здесь системные deps
# (apt, нужен root) и сам браузер в PLAYWRIGHT_BROWSERS_PATH. chown — тут же.
# Нужен и api, и worker: src/api/services/web_monitor.py импортирует playwright.
RUN python -m playwright install --with-deps chromium && \
    chown -R kag:kag /opt/ms-playwright && \
    rm -rf /root/.cache /var/lib/apt/lists/*

# Веса OCR отдельной предзагрузкой больше не нужны: RapidOCR кладёт модели PP-OCRv5 внутрь пакета
# (site-packages/rapidocr/models) при первом создании движка — это делает pip-слой выше, поэтому
# образ остаётся пригодным для работы без сети. Ранее здесь качались веса Occular (Shivin11/*).

# git и его зависимости больше не нужны — чистим (без chown в этом слое,
# иначе overlayfs снова скопирует файлы наверх).
RUN apt-get purge -y git && apt-get autoremove -y && rm -rf /var/lib/apt/lists/* /root/.cache

# Модели детекторов (наш собственный экспорт из официальных весов PaddlePaddle, Apache-2.0):
#   pp_doclayout_v3_int8.onnx        — ОБЛАСТЬ таблицы на странице (второй шанс, если сетка не нашла);
#   rt_detr_wireless_cell_int8.onnx  — ЯЧЕЙКИ таблицы (безлинейные/прозаические: абзац остаётся в ячейке).
# INT8 выбран по замеру: точность держится (IoU 0,994 у макета; 15/15 ячеек и IoU 0,997 у ячеек),
# а размер 127,5+123,4 МБ → 35+32 МБ. Медленнее FP32 на Broadwell, но эти этапы включаются настройкой
# и работают только там, где базовый разбор не справился.
# Файлы лежат в build-контексте (assets/models), рецепт пересборки — docs/guides/table-structure-bakeoff.md.
COPY assets/models/ /app/models/

LABEL maintainer="KAG Team"
LABEL description="KAG shared base image (python + OCR + Playwright) for api/worker/mcp"
LABEL version="2026.09.07"
