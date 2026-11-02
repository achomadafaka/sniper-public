#!/bin/bash
# Stop alerter service

set -euo pipefail

cd /home/bigpapi/Desktop/Sniper

if [ -f alerter.pid ]; then
    PID="$(cat alerter.pid)"
    if kill -0 "$PID" 2>/dev/null; then
        kill "$PID" 2>/dev/null || true
        echo "✅ Alerter stopped (PID: $PID)"
    else
        echo "⚠️  PID $PID is not running"
    fi
    rm -f alerter.pid
else
    echo "⚠️  No alerter.pid found"
fi
