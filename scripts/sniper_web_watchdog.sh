#!/usr/bin/env bash
# Sniper web dashboard watchdog. Systemd is the sole process and port owner.
set -u

cd /home/bigpapi/Desktop/Sniper || exit 1

WATCHDOG_LOG=/home/bigpapi/Desktop/Sniper/logs/sniper_web_watchdog.log
SERVICE=sniper-web.service

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >> "$WATCHDOG_LOG"; }

mkdir -p /home/bigpapi/Desktop/Sniper/logs

state=$(systemctl is-active "$SERVICE" 2>/dev/null || true)
case "$state" in
  active|activating|reloading) exit 0 ;;
esac

log "service state=$state; requesting systemd start"
if systemctl start "$SERVICE"; then
  log "systemd start requested"
  exit 0
fi
log "systemd start failed"
exit 1
