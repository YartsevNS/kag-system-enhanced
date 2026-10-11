#!/usr/bin/env bash
# Замер схемы v2 БЕЗ выката образа: правленый код кладём в КОПИЮ src внутри контейнера (/tmp/v2src)
# и запускаем прибор с PYTHONPATH на эту копию. Рабочие контейнеры api/worker не трогаются, образ
# не пересобирается — поэтому замер идёт секундами, а не минутами выката.
#
# Запуск НА СТЕНДЕ (18), файлы рядом со скриптом:
#   bash run_audit_v2.sh chunk <документ>          — один фрагмент, сырой ответ модели
#   bash run_audit_v2.sh set   <документ> 10       — набор фрагментов
#   bash run_audit_v2.sh doc   <документ>          — все фрагменты документа
#
# Отчёт с сырыми ответами забирается в каталог скрипта: audit_v2_<уровень>.json
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LEVEL="${1:?укажи уровень: chunk|set|doc}"
DOC="${2:?укажи идентификатор документа}"
COUNT="${3:-10}"
C=kag-api

echo "== 1. что лежит в контейнере сейчас (для протокола)"
docker exec "$C" sh -c 'md5sum /app/src/indexing/graph_ontology.py /app/src/indexing/extraction_v2.py 2>/dev/null || echo "файлов нет"'

echo "== 2. собираю копию src в контейнере (/tmp/v2src), рабочий код не трогаю"
docker exec "$C" sh -c 'rm -rf /tmp/v2src /tmp/audit && mkdir -p /tmp/v2src /tmp/audit && cp -r /app/src /tmp/v2src/src'

for f in graph_ontology.py extraction_v2.py entity_typing.py; do
  docker cp "$HERE/$f" "$C:/tmp/v2src/src/indexing/$f"
done
docker cp "$HERE/audit_v2.py" "$C:/tmp/audit/audit_v2.py"

echo "== 3. подложенный код (должен совпасть с ноутбуком)"
docker exec "$C" sh -c 'md5sum /tmp/v2src/src/indexing/graph_ontology.py /tmp/v2src/src/indexing/extraction_v2.py'

echo "== 4. замер уровня $LEVEL"
docker exec -w /tmp/audit -e PYTHONPATH=/tmp/v2src "$C" \
  python /tmp/audit/audit_v2.py "$LEVEL" "$DOC" "$COUNT" "/tmp/audit/audit_v2_${LEVEL}.json"

echo "== 5. забираю отчёт с сырыми ответами модели"
docker cp "$C:/tmp/audit/audit_v2_${LEVEL}.json" "$HERE/audit_v2_${LEVEL}.json"
echo "готово: $HERE/audit_v2_${LEVEL}.json"
