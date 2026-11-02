#!/usr/bin/env bash
# HL cluster-map lane watchdog. Public read-only HL data; no keys/orders.
set -u
cd /home/bigpapi/Desktop/Sniper-leadlag || exit 1
PY=/home/bigpapi/.pyenv/versions/3.13.0/bin/python3
DB=/home/bigpapi/Desktop/Sniper/data/hyperlive_engine.sqlite
PIDFILE=/home/bigpapi/Desktop/Sniper/data/cluster_map.pid
MATCH="run_cluster_map.py --db-path $DB"
LAUNCH="$PY -u scripts/run_cluster_map.py --db-path $DB --max-assets 12 --max-addresses 250 --interval-seconds 180 --min-notional 50000"
mkdir -p .logs
pid=$(cat "$PIDFILE" 2>/dev/null || true)
if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null | grep -Fq "$MATCH"; then exit 0; fi
ex=$(pgrep -f "$MATCH" | head -1 || true)
if [ -n "$ex" ]; then echo "$ex" > "$PIDFILE"; exit 0; fi
setsid nohup $LAUNCH >>.logs/cluster_map.log 2>&1 </dev/null &
sleep 3
echo "$(pgrep -f "$MATCH" | head -1)" > "$PIDFILE"
