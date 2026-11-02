#!/bin/bash
# Start alerter as background service

set -euo pipefail

cd /home/bigpapi/Desktop/Sniper
source .venv/bin/activate

mkdir -p logs

if [ -f alerter.pid ]; then
    EXISTING_PID="$(cat alerter.pid)"
    if kill -0 "$EXISTING_PID" 2>/dev/null; then
        echo "⚠️  Alerter already running (PID: $EXISTING_PID)"
        echo "📋 Logs: tail -f logs/alerter.log"
        exit 0
    fi
    rm -f alerter.pid
fi

nohup python -u alerter.py >> logs/alerter.log 2>&1 &
echo $! > alerter.pid

echo "✅ Alerter started (PID: $(cat alerter.pid))"
echo "📋 Logs: tail -f logs/alerter.log"
