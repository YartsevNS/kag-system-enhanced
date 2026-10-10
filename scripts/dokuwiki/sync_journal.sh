#!/usr/bin/env bash
# Синхронизация журнала работ в вики: сессии Hermes → датированные страницы DokuWiki → темы.
#
# Запускается с ноутбука (в нём журнал сессий Hermes). Заливка идёт на самом хосте вики:
# XML-RPC там закрыт для внешних адресов правилом nginx, поэтому страницы копируются по scp,
# а вызов API делается через localhost.
#
# Запуск:  bash scripts/dokuwiki/sync_journal.sh
# Расписание: задача cron (см. docs/guides/dokuwiki-update-and-ops.md).
set -euo pipefail

REPO="/c/VSCODE_PROJECT/kag-system-tables"          # для команд оболочки
REPO_WIN="C:/VSCODE_PROJECT/kag-system-tables"      # для Windows-программ (питон не понимает /c/...)
SSH_KEY="$HOME/.ssh/hermes_41"
HOST="root@10.0.1.25"
SCRATCH="$(cygpath -u "$LOCALAPPDATA" 2>/dev/null || echo "$LOCALAPPDATA")/hermes/cache/scratch"
SCRATCH_WIN="$(cygpath -m "$SCRATCH" 2>/dev/null || echo "$SCRATCH")"
PY="$REPO/.venv/Scripts/python.exe"
D="$SCRATCH/dw_sessions"
LOG="$SCRATCH/dw_sync.log"
SSH=(ssh -i "$SSH_KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=15)
SCP=(scp -q -i "$SSH_KEY" -o IdentitiesOnly=yes)

echo "=== $(date '+%d.%m.%Y %H:%M') синхронизация журнала в вики ===" | tee "$LOG"

# 1. Свежая копия журнала сессий (саму базу не трогаем — Hermes может быть запущен)
cp -f "$HOME/AppData/Local/hermes/state.db" "$SCRATCH/state_copy.db"

# 2. Собираем страницы: пары «вопрос → итог хода» из журнала
rm -rf "$D"; mkdir -p "$D"
"$PY" "$REPO_WIN/scripts/bakeoff/sessions_to_dokuwiki.py" \
      --db "$SCRATCH_WIN/state_copy.db" --out "$SCRATCH_WIN/dw_sessions" --min-blocks 1 >> "$LOG" 2>&1
echo "собрано страниц: $(ls "$D"/*.txt 2>/dev/null | wc -l)" | tee -a "$LOG"

# 3. Копируем страницы и приборы на хост вики
"${SCP[@]}" "$REPO/scripts/bakeoff/sessions_to_dokuwiki.py" \
             "$REPO/scripts/dokuwiki/build_catalog.py" \
             "$REPO/scripts/dokuwiki/build_themes.py" "$HOST:/tmp/"
"${SSH[@]}" "$HOST" 'rm -rf /tmp/dw_sessions && mkdir -p /tmp/dw_sessions'
"${SCP[@]}" "$D"/*.txt "$HOST:/tmp/dw_sessions/"

# 4. Заливка и сборка навигации (только с localhost)
"${SSH[@]}" "$HOST" 'PASS=$(sed -n "s/^пароль: //p" /root/kag-agent-cred.txt)
python3 /tmp/sessions_to_dokuwiki.py --push-dir /tmp/dw_sessions --upload \
        --url http://127.0.0.1:8080/lib/exe/xmlrpc.php --user kag-agent --password "$PASS"
python3 /tmp/build_catalog.py
python3 /tmp/build_themes.py --apply
su www-data -s /bin/sh -c "cd /var/www/dokuwiki && php bin/indexer.php" >/dev/null 2>&1
chown -R www-data:www-data /var/www/dokuwiki/data
echo "страниц в разделе: $(find /var/www/dokuwiki/data/pages/kag -name "*.txt" | wc -l)"
echo "блоков: $(grep -rh "Вопрос:" /var/www/dokuwiki/data/pages/kag/ | wc -l)"' 2>&1 | tee -a "$LOG"

echo "=== готово $(date '+%H:%M') ===" | tee -a "$LOG"
