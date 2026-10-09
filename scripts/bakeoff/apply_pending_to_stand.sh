#!/usr/bin/env bash
# Применение всего, что накопилось в ветке paddle-ocr, к стенду — одной командой.
#
# Зачем: во время сессии стенд оказался недоступен (внешний доступ режется списком доверенных
# адресов), а изменения уже в git. Этот скрипт выполняет их применение и проверку по порядку,
# чтобы не вспоминать команды заново.
#
# Запуск НА СТЕНДЕ (после git fetch ветки):
#   bash scripts/bakeoff/apply_pending_to_stand.sh
#
# Что делает:
#   1) выравнивает дерево сборки по origin/paddle-ocr;
#   2) применяет настройки мониторинга к дереву развёртывания точечно и с бэкапом
#      (закрытие Grafana от внешней сети, публичные дашборды, доступ api к Grafana, дедупликация);
#   3) копирует дашборд хранилища в каталог стенда;
#   4) пересобирает и выкатывает api, пересоздаёт grafana и nginx;
#   5) проверяет: что опубликовано наружу, закрыт ли /grafana/ без сессии, есть ли метрики,
#      отдаются ли сырые данные хранилища.
set -euo pipefail

BUILD_DIR="${BUILD_DIR:-/home/yartsevn/kag-build-new}"
DEPLOY_DIR="${DEPLOY_DIR:-/home/yartsevn/kag-system}"
TAG="${TAG:-2026.10.05.1}"
BRANCH="${BRANCH:-paddle-ocr}"

say() { printf '\n=== %s ===\n' "$1"; }

say "1. дерево сборки: $BRANCH"
cd "$BUILD_DIR"
git fetch origin "$BRANCH" -q
git checkout -f "origin/$BRANCH" >/dev/null 2>&1
git log --oneline -1

say "2. настройки мониторинга (точечно, с бэкапами)"
python3 "$BUILD_DIR/scripts/bakeoff/apply_monitoring_config.py" "$DEPLOY_DIR"

say "3. дашборд хранилища"
install -m 644 "$BUILD_DIR/docker/grafana/dashboards/kag-storage.json" \
    "$DEPLOY_DIR/docker/grafana/dashboards/kag-storage.json"
echo "скопирован kag-storage.json"

say "4. сборка и выкат"
docker build -q -t "kre44et/kag-api:$TAG" -f Dockerfile . >/dev/null
docker push -q "kre44et/kag-api:$TAG" >/dev/null
cd "$DEPLOY_DIR"
docker-compose up -d --force-recreate --no-deps api 2>&1 | tail -1
docker-compose --profile monitoring up -d --force-recreate grafana 2>&1 | tail -1
docker-compose up -d --force-recreate nginx 2>&1 | tail -1
echo "жду минуту: фоновый снимок статистики и первый съём метрик"
sleep 75

say "5. проверки"
echo "--- что опубликовано наружу (нас интересует отсутствие 3000 у Grafana) ---"
docker ps --format '{{.Names}}|{{.Ports}}' | grep -v '^$' | head -20

echo "--- /grafana/ без сессии должен быть закрыт ---"
curl -sk -o /dev/null -w 'без сессии: %{http_code}\n' https://127.0.0.1/grafana/ || true

PW_DB="$(docker exec kag-api printenv ADMIN_PASSWORD)"
JAR=/tmp/kag_cookies_apply.txt
rm -f "$JAR"
curl -sk -c "$JAR" -X POST -H "Content-Type: application/json" \
     -d "{\"username\":\"admin\",\"password\":\"$PW_DB\"}" \
     https://127.0.0.1/api/v1/auth/login -o /dev/null
echo "--- с сессией ---"
curl -sk -b "$JAR" -o /dev/null -w '/grafana/: %{http_code}\n' https://127.0.0.1/grafana/
curl -sk -b "$JAR" -o /dev/null -w 'дашборд: %{http_code}\n' https://127.0.0.1/grafana/public-dashboards/ \
    2>/dev/null || true

echo "--- метрики хранилища ---"
docker exec kag-api sh -lc "curl -s http://127.0.0.1:8000/metrics | grep -cE '^kag_(fs_free_bytes|dir_bytes|docker_bytes)'"

echo "--- сырые данные утилит (первая строка df) ---"
curl -sk -b "$JAR" https://127.0.0.1/api/v1/admin/models/storage/raw \
  | python3 -c "import sys, json; d = json.load(sys.stdin); print('ключи:', list(d.keys())); print((d.get('df_h') or '').splitlines()[0][:70])"

say "готово. Приёмник почты, если остался с проверок, убрать: docker rm -f kag-smtp-sink"
