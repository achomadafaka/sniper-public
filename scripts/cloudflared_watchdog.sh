#!/usr/bin/env bash
# Report the Sniper dashboard's public URL to Telegram when it changes.
#
# systemd owns the tunnel: sniper-tunnel.service is enabled, Restart=always, RestartSec=5. Do NOT
# start, stop, or supervise cloudflared here — that unit already does it.
#
# The gap this closes: the unit runs a QUICK tunnel, so cloudflared assigns a NEW random
# *.trycloudflare.com hostname on every restart. The host had rotated three times with nothing
# recording the change, so a phone outside the house held a dead bookmark and no way to learn the
# new address. This sends the new URL to the same Telegram account the alerts use.
#
# The tunnel exposes http://127.0.0.1:5000 to the public internet. HTTP basic auth
# (SNIPER_DASH_USER / SNIPER_DASH_PASS in .env) is the ONLY access control on it.
set -u

REPO=/home/bigpapi/Desktop/Sniper
cd "$REPO" || exit 1

LOG=$REPO/logs/cloudflared_url.log
URLFILE=$REPO/data/cloudflared_url.txt
UNIT=sniper-tunnel.service

mkdir -p "$REPO/logs" "$REPO/data"
log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >> "$LOG"; }

# One reporter at a time, so two cron minutes cannot send the same URL twice.
exec 9>"$REPO/data/cloudflared_url.lock"
flock -n 9 || exit 0

pid=$(systemctl show -p MainPID --value "$UNIT" 2>/dev/null)
[ -n "$pid" ] && [ "$pid" != "0" ] || exit 0        # tunnel down; systemd is restarting it

# Read the metrics port off the process itself. cloudflared picks it, and a hard-coded 20241 would
# go silently blind the day it picks another one.
port=$(ss -tlnp 2>/dev/null | grep -F "pid=$pid," | grep -oE '127\.0\.0\.1:[0-9]+' | head -1)
[ -n "$port" ] || exit 0                            # metrics not bound yet; try again next minute

host=$(curl -sf --max-time 5 "http://$port/quicktunnel" \
       | sed -n 's/.*"hostname":"\([^"]*\)".*/\1/p')
[ -n "$host" ] || exit 0
url="https://$host"

[ "$(cat "$URLFILE" 2>/dev/null || true)" = "$url" ] && exit 0   # unchanged: stay quiet

printf '%s\n' "$url" > "$URLFILE"
log "url -> $url"

tok=$(grep -m1 '^TELEGRAM_BOT_TOKEN_AI=' "$REPO/.env" | cut -d= -f2-)
chat=$(grep -m1 '^ALLOWED_TELEGRAM_USER_IDS=' "$REPO/.env" | cut -d= -f2- | cut -d, -f1)
if [ -n "$tok" ] && [ -n "$chat" ]; then
    curl -sf --max-time 10 "https://api.telegram.org/bot$tok/sendMessage" \
        --data-urlencode "chat_id=$chat" \
        --data-urlencode "text=Sniper dashboard moved: $url" > /dev/null \
        || log "telegram send failed"
fi
exit 0
