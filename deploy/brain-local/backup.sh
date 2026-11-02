#!/usr/bin/env bash
# Nightly pg_dump of the local brain (public schema, custom format) to
# ~/.local/share/sniper-brain/backups, keeping the newest KEEP (default 7). Cron:
#   17 4 * * * /home/bigpapi/Desktop/Sniper/deploy/brain-local/backup.sh >> /home/bigpapi/Desktop/Sniper/logs/brain_backup.log 2>&1
set -euo pipefail
OUT="$HOME/.local/share/sniper-brain/backups"
KEEP="${KEEP:-7}"
mkdir -p "$OUT" && chmod 700 "$OUT"
NAME="brain_$(date -u +%Y%m%dT%H%M%SZ).dump"
docker exec sniper-brain-db-1 pg_dump -U postgres -d postgres --schema=public --no-owner -Fc \
  > "$OUT/$NAME.part"
mv "$OUT/$NAME.part" "$OUT/$NAME"
ls -1t "$OUT"/brain_*.dump | tail -n +"$((KEEP + 1))" | xargs -r rm -f
echo "$(date -u +%FT%TZ) brain backup $NAME $(du -h "$OUT/$NAME" | cut -f1)"
