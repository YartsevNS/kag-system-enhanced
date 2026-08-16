#!/bin/sh
chmod 666 /var/run/docker.sock 2>/dev/null || true
chmod -R 777 /app/data 2>/dev/null || true
mkdir -p /app/data/thumbnails 2>/dev/null || true
# su с явным shell — у пользователя kag стоит /sbin/nologin
exec su -s /bin/sh kag -c 'uvicorn src.api.main:app --host 0.0.0.0 --port 8000'
