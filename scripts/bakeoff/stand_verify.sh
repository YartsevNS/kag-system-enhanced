#!/usr/bin/env bash
# Проверка стенда после выката: что реально в контейнерах и как ведут себя детекторы.
#
# Запускать с машины, откуда есть ssh на стенд:  STAND=18 bash scripts/bakeoff/stand_verify.sh
# Ничего не меняет, кроме переобработки одного документа (это и есть проверка поведения).
set -uo pipefail
STAND=${STAND:-18}
TAG=${TAG:-2026.10.05.1}
S="ssh -o BatchMode=yes $STAND"

echo "=== 1) образы и здоровье ==="
$S "docker ps --format '{{.Names}} | {{.Image}} | {{.Status}}' | grep -E 'kag-api|worker|mcp'"

echo
echo "=== 2) модели в контейнере (должны быть обе, INT8) ==="
$S "docker exec kag-system_worker_1 sh -lc 'ls -la /app/models/ && sha256sum /app/models/*.onnx | cut -c1-40'"

echo
echo "=== 3) onnxruntime: есть ли OpenVINO провайдер внутри образа ==="
$S "docker exec kag-system_worker_1 python -c \"
import onnxruntime as ort
print('версия:', ort.__version__)
print('провайдеры:', ort.get_available_providers())
\""

echo
echo "=== 4) настройки: OCR-движок и детекторы ==="
$S "docker exec kag-api python -c \"
from src.api.services.config_store import config_store
c = config_store.get('ocr','settings') or {}
for k in ('service_enabled','service_url','engine','detector_enabled','detector_path',
          'cells_detector_enabled','cells_detector_path'):
    print(f'  {k} = {c.get(k)}')
\"" 2>&1 | grep -v Warning

echo
echo "=== 5) статус модулей детекторов (по коду приложения) ==="
$S "docker exec kag-system_worker_1 python -c \"
from src.indexing import table_detector, table_cells
print('область:', table_detector.status())
print('ячейки:', table_cells.status())
\"" 2>&1 | grep -vE "Warning|INFO"

echo
echo "=== 6) поведение: переобработка документа-скана и разбор логов ==="
DOC=${DOC:-}
if [ -z "$DOC" ]; then
  DOC=$($S "docker exec kag-postgres psql -U kag -d kag -tAc \"select id from documents where filename ilike '%Договор от 09.11.2020%' limit 1;\"" | tr -d '[:space:]')
fi
echo "документ: $DOC"
$S "docker exec kag-api python /tmp/reprocess_doc.py $DOC" 2>&1 | tail -1
sleep 75
$S "docker logs --since 3m kag-system_worker_1 2>&1 | grep -E 'детектор области|по ячейкам|тип:|восстановлено таблиц|parse \(' | tail -6"

echo
echo "Готово. Ожидаемо: таблица восстановлена, в причине видно тип (numeric/prose), при необходимости —"
echo "строку про пересборку по ячейкам. Если детекторы выключены настройками — строк про них не будет."
