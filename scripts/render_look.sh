#!/bin/bash
# Рендер страниц-кандидатов в PNG для просмотра глазами.
# Запуск на стенде:  bash /home/yartsevn/render_look.sh <doc_id_префикс>:<страница> ...
# Файлы кладутся в /tmp/, оттуда копируются на ноутбук.
set -u
cd /home/yartsevn/kag-system/data/uploads || exit 1
for spec in "$@"; do
  DOC="${spec%%:*}"
  PAGE="${spec##*:}"
  F=$(ls | grep "^${DOC}" | head -1)
  if [ -z "$F" ]; then
    echo "  нет файла для ${DOC}"
    continue
  fi
  docker exec kag-api pdftoppm -f "$PAGE" -l "$PAGE" -r 110 -png "/app/data/uploads/${F}" "/tmp/look_${DOC}" >/dev/null 2>&1
  PRODUCED=$(docker exec kag-api sh -c "ls /tmp/look_${DOC}* 2>/dev/null" | head -1)
  if [ -z "$PRODUCED" ]; then
    echo "  не отрендерилось: ${DOC} стр. ${PAGE} (файл ${F})"
    continue
  fi
  docker cp "kag-api:${PRODUCED}" "/tmp/look_${DOC}.png" >/dev/null 2>&1
  echo "  ок: ${DOC} стр. ${PAGE} -> /tmp/look_${DOC}.png (${F})"
done
ls -la /tmp/look_*.png 2>/dev/null | awk '{print "  готово: "$NF" "$5" байт"}'
