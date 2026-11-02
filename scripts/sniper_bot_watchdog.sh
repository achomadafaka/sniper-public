#!/usr/bin/env bash
# Sniper AI Bot watchdog. Keeps the Telegram <-> Claude Code relay (bot/relay.py)
# alive across harness reaps / reboots, same pattern as sniper_web_watchdog.sh.
#
# relay.py loads the repo .env itself (python-dotenv), so no env export is needed here.
# It refuses to serve anyone if ALLOWED_TELEGRAM_USER_IDS is unset, and raises at import
# if TELEGRAM_BOT_TOKEN_AI is unset -- so we pre-check both to avoid a 1/min crash loop.
set -u

REPO=/home/bigpapi/Desktop/Sniper
cd "$REPO" || exit 1

PY=/home/bigpapi/.pyenv/versions/3.13.0/bin/python3
PIDFILE="$REPO/data/sniper_bot.pid"
LOGFILE="$REPO/logs/sniper_bot.log"
WATCHDOG_LOG="$REPO/logs/sniper_bot_watchdog.log"
MATCH="bot/relay.py"

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >> "$WATCHDOG_LOG"; }

mkdir -p "$REPO/logs" "$REPO/data"

# already running? nothing to do.
if pgrep -f "$MATCH" >/dev/null 2>&1; then
  exit 0
fi

# Credential preflight -- without these relay.py either crashes on import or
# rejects every message. Stay down rather than crash-loop; log once per hour.
missing=""
grep -qE '^TELEGRAM_BOT_TOKEN_AI=.+' "$REPO/.env" 2>/dev/null || missing="$missing TELEGRAM_BOT_TOKEN_AI"
grep -qE '^ALLOWED_TELEGRAM_USER_IDS=[0-9]' "$REPO/.env" 2>/dev/null || missing="$missing ALLOWED_TELEGRAM_USER_IDS"
if [ -n "$missing" ]; then
  if [ ! -f "$WATCHDOG_LOG" ] || [ -z "$(find "$WATCHDOG_LOG" -newermt '-60 minutes' 2>/dev/null)" ]; then
    log "NOT starting: missing in .env:$missing"
  fi
  exit 0
fi

log "relay not found; launching"
nohup env PYTHONPATH="$REPO" "$PY" -u "$REPO/bot/relay.py" >> "$LOGFILE" 2>&1 &
echo $! > "$PIDFILE"
log "launched pid $(cat "$PIDFILE")"
