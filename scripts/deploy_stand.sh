#!/bin/bash
# Деплой стенда 18: pull с повторами, сборка, проверка маркера внутри образа, выкатка.
#
# Зачем повторы: путь до github.com с этого стенда даёт ~1 отказ на 20 полных
# HTTPS-запросов (TCP при этом 20/20), и pull иногда висит до таймаута. 3 попытки
# с паузой закрывают рядовой всплеск; если и они не прошли — скрипт честно падает,
# и коммит доставляется бандлом (git bundle create <файл> <коммит-стенда>..PREPROD,
# scp, git fetch /tmp/x.bundle PREPROD, git merge --ff-only FETCH_HEAD).
#
# Использование (с хоста):
#   ssh -o BatchMode=yes yartsevn@192.168.50.18 \
#     "MARKER=hw-table bash -s -- 2026.09.18.5" < scripts/deploy_stand.sh
#   MARKER — подстрока, которая обязана найтись в образе (доказательство, что собралось
#   новое дерево); не задан — проверка пропускается.
set -u
cd /home/yartsevn/kag-system || exit 1
TAG="${1:?нужен тег образа, например 2026.09.18.5}"
MARKER="${MARKER:-}"
SERVICES="${SERVICES:-api worker worker-maintenance}"
BASE_IMAGE="${BASE_IMAGE:-kre44et/kag-base:2026.09.07}"

echo "=== 1. git pull с повторами ==="
REMOTE=$(timeout 60 git ls-remote origin PREPROD 2>/dev/null | awk '{print $1}' | head -1)
echo "  на github: ${REMOTE:0:12}"
PULLED=0
for i in 1 2 3; do
  echo "  попытка $i"
  timeout 90 git pull origin PREPROD 2>&1 | tail -2
  if [ -n "$REMOTE" ] && [ "$(git rev-parse HEAD)" = "$REMOTE" ]; then PULLED=1; break; fi
  sleep 20
done
echo "  HEAD: $(git log --oneline -1)"
grep -o "kre44et/kag-api:2026[0-9.]*" docker-compose.yml | head -1
if [ "$PULLED" != "1" ]; then
  echo "  ДЕРЕВО НЕ СОВПАЛО С GITHUB (сеть или непроведённые коммиты)."
  echo "  Проверь: git status --porcelain (untracked-файлы мешают merge) и доступность github.com."
  echo "  Обходной путь: доставить коммит бандлом и запустить скрипт снова."
  exit 2
fi

echo "=== 2. база и сборка ==="
docker image inspect "$BASE_IMAGE" >/dev/null 2>&1 || docker pull "$BASE_IMAGE" 2>&1 | tail -1
docker build -q -t kre44et/kag-api:$TAG -f Dockerfile . 2>&1 | tail -1
docker build -q -t kre44et/kag-worker:$TAG -f Dockerfile.worker . 2>&1 | tail -1

echo "=== 3. маркер внутри образа (доказательство свежего дерева) ==="
if [ -n "$MARKER" ]; then
  CNT=$(docker run --rm --entrypoint sh kre44et/kag-api:$TAG -c "grep -rc '$MARKER' /app/src 2>/dev/null | grep -v ':0$' | head -5")
  echo "  '$MARKER': ${CNT:-НЕ НАЙДЕН}"
  [ -n "$CNT" ] || { echo "  ОСТАНОВ: маркер не найден — образ собран из старого дерева"; exit 3; }
else
  echo "  (MARKER не задан, проверка пропущена)"
fi

echo "=== 4. очередь документов ==="
BUSY=$(docker exec kag-redis redis-cli -n 1 LLEN documents < /dev/null | tr -d '\r')
echo "  задач в очереди: ${BUSY:-?}"

echo "=== 5. выкатка ==="
docker-compose up -d --force-recreate $SERVICES 2>&1 | tail -3
sleep 30
docker ps --format '{{.Names}} {{.Image}} {{.Status}}' | grep -E 'kag-api|worker' | head -4
curl -s -o /dev/null -w "  health api: %{http_code}\n" http://localhost:8000/api/v1/health
curl -s -o /dev/null -w "  health nginx: %{http_code}\n" http://localhost/api/v1/health

echo "=== 6. пуш образов в хаб ==="
for img in kre44et/kag-api:$TAG kre44et/kag-worker:$TAG; do
  docker push "$img" 2>&1 | tail -1
done
