#!/bin/bash
# Загрузить регуляторный документ по URL в KAG: скачать → имя → upload → process.
# Использование: ./upload_reg_url.sh <url> [имя_файла]
# Логин берётся из .env стенда (ADMIN_PASSWORD), cookie — /tmp/kag.jar на сервере 18.
set -uo pipefail
URL="$1"
NAME="${2:-$(basename "$URL")}"
TMP="/tmp/reg_${NAME}"

curl -sL -o "$TMP" "$URL" || { echo "ОШИБКА: не скачался $URL"; exit 1; }
SIZE=$(stat -c %s "$TMP" 2>/dev/null || echo 0)
[ "$SIZE" -gt 1000 ] || { echo "ОШИБКА: файл слишком мал ($SIZE байт)"; exit 1; }
head -c 5 "$TMP" | grep -q '%PDF' || { echo "ОШИБКА: это не PDF"; exit 1; }

set -a; . /home/yartsevn/kag-system/.env; set +a
curl -sk -c /tmp/kag.jar -X POST https://127.0.0.1/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"admin\",\"password\":\"$ADMIN_PASSWORD\"}" -o /dev/null

R=$(curl -sk -b /tmp/kag.jar -X POST https://127.0.0.1/api/v1/upload/ \
  -F "file=@$TMP;filename=$NAME" -F "visibility=public")
ID=$(echo "$R" | python3 -c "import sys,json;print(json.load(sys.stdin).get('document_id',''))" 2>/dev/null)
[ -n "$ID" ] || { echo "ОШИБКА загрузки: $R"; exit 2; }

Q=$(curl -sk -b /tmp/kag.jar -X POST "https://127.0.0.1/api/v1/upload/$ID/process")
echo "загружен: $NAME ($((SIZE/1024)) КБ) -> $ID"
echo "обработка: $Q"
