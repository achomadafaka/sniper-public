#!/bin/bash
# Extract Telegram bot token from OpenClaw config

set -euo pipefail

CONFIG_PATH="$HOME/.openclaw/openclaw.json"

if [ ! -f "$CONFIG_PATH" ]; then
    echo "❌ OpenClaw config not found at $CONFIG_PATH"
    exit 1
fi

if command -v jq >/dev/null 2>&1; then
    TOKEN="$(jq -r '.channels.telegram.botToken // empty' "$CONFIG_PATH")"
else
    TOKEN="$(python3 - <<'PY' "$CONFIG_PATH"
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    raw = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    print("")
    raise SystemExit(0)

token = raw.get("channels", {}).get("telegram", {}).get("botToken", "")
print(str(token).strip())
PY
)"
fi

if [ -z "$TOKEN" ] || [ "$TOKEN" = "null" ]; then
    echo "❌ Telegram bot token not found in config"
    exit 1
fi

echo "✅ Telegram Bot Token:"
echo "$TOKEN"
echo ""
echo "Add this to alerter.py:"
echo "TELEGRAM_BOT_TOKEN = \"$TOKEN\""
