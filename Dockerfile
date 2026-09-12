# ===========================================
# KAG API — тонкий образ поверх kag-base
# ===========================================
# Вся тяжёлая часть (python, apt, venv, requirements, Occular, Playwright,
# веса) — в базовом образе kag-base. Здесь только код и CMD.
#
# Сборка (СНАЧАЛА должен существовать kag-base той же версии):
#   docker build -t kre44et/kag-api:2026.09.07 -f Dockerfile .
#   docker push kre44et/kag-api:2026.09.07
#
# При изменении requirements.txt/весов — пересобрать kag-base
# (docker/base/Dockerfile), затем этот образ.

FROM kre44et/kag-base:2026.09.07

LABEL maintainer="KAG Team"
LABEL description="KAG - Knowledge Augmentation Generation. AI-powered knowledge management with RAG."
LABEL version="0.3.0"

# Директории для данных и SSH-менеджера (bind-mount ./data снаружи)
RUN mkdir -p /app/data/audit /app/data/annotations /app/data/quality_tracking /app/data/ab_tests /app/.ssh && \
    chmod -R 750 /app/data && \
    chmod 700 /app/.ssh && \
    chown -R kag:kag /app/data

# Исходный код
COPY --chown=kag:kag src/ /app/src/

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/v1/health')" || exit 1

# Непривилегированный пользователь. Доступ к docker.sock — через group_add
# в docker-compose.yml (gid хостовой docker-группы), не через chmod 666.
USER kag

CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
