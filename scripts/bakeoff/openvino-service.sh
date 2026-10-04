#!/usr/bin/env bash
# Копия службы OCR на 41 с движком OpenVINO (порт 8021). Штатная служба на 8020 (CPU) не трогается.
# Замер 04.10.2026: полный пайплайн страницы 2,35x; через API 1,6-2,1x; на стенде шаг parse 7099 -> 3337 мс (2,13x);
# строки, числа и таблица совпадают, подстановок цифр 0.
#   start | stop | restart | status | check   (check — код возврата 1, если порт не слушается)
set -u
PORT=8021
DIR=/home/yartsevn/ocr-bakeoff

is_up() { ss -ltn 2>/dev/null | grep -q ":$PORT "; }

case "${1:-status}" in
  start)
    if is_up; then echo "уже работает на $PORT"; exit 0; fi
    cd "$DIR" || exit 1
    setsid nohup .venv/bin/python ocr_service.py --port "$PORT" --lang cyrillic --engine openvino --warmup \
      > /tmp/ocr_openvino.log 2>&1 < /dev/null &
    for _ in $(seq 1 30); do sleep 2; is_up && break; done
    if is_up; then
      curl -s -m 5 "http://127.0.0.1:$PORT/health" | head -c 100; echo; exit 0
    fi
    echo "не поднялось, смотрите /tmp/ocr_openvino.log"; exit 1 ;;
  stop)    fuser -k "$PORT/tcp" 2>/dev/null; echo "остановлено"; exit 0 ;;
  restart) "$0" stop; sleep 2; "$0" start ;;
  status)  if is_up; then echo "работает на $PORT"; else echo "не запущено"; fi ;;
  check)   if is_up; then exit 0; else echo "служба на $PORT не отвечает"; exit 1; fi ;;
  *)       echo "использование: $0 {start|stop|restart|status|check}"; exit 2 ;;
esac
