#!/usr/bin/env bash
# =============================================================================
# Sniper VPS Bootstrap Script
# Provisions a fresh Ubuntu 24.04 VPS for the Sniper trading system
# Usage: curl -sL <raw-github-url> | bash
#   or: bash scripts/vps_bootstrap.sh
# =============================================================================
set -euo pipefail

APP_DIR="$HOME/sniper"
REPO_URL="https://github.com/achomadafaka/sniper.git"
BRANCH="main"
PYTHON_MIN="3.11"

echo "========================================"
echo "  Sniper VPS Bootstrap"
echo "========================================"

# --- System packages ---
echo "[1/7] Installing system dependencies..."
sudo apt-get update -qq
sudo apt-get install -y -qq \
  python3 python3-pip python3-venv \
  git sqlite3 curl jq \
  build-essential libffi-dev libssl-dev \
  > /dev/null 2>&1

# Verify Python version
PYVER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "  Python version: $PYVER"

# --- Clone or update repo ---
echo "[2/7] Setting up repository..."
if [ -d "$APP_DIR/.git" ]; then
  echo "  Repo exists, pulling latest..."
  git -C "$APP_DIR" pull --ff-only origin "$BRANCH"
else
  echo "  Cloning fresh..."
  git clone -b "$BRANCH" "$REPO_URL" "$APP_DIR"
fi

cd "$APP_DIR"

# --- Python virtual environment ---
echo "[3/7] Setting up Python environment..."
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q 2>/dev/null || pip install \
  flask websockets aiohttp py-clob-client python-dotenv requests \
  -q

# --- Environment file ---
echo "[4/7] Setting up environment..."
if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "  Created .env from template — EDIT THIS with your keys!"
  echo "  Required: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (for alerts)"
  echo "  Optional: SNIPER_API_KEY, NEWS_API_KEY"
else
  echo "  .env already exists, keeping it"
fi

# --- Initialize canonical schema ---
echo "[5/7] Initializing database schema..."
python3 - <<'PY'
from db import get_connection, init_db, ensure_paper_trades_slippage_columns
from db_discovery import init_discovery_schema

conn = get_connection("sniper_tracker.db")
init_db(conn)
init_discovery_schema(conn, commit=True)
ensure_paper_trades_slippage_columns(conn)
conn.close()
print("  Database schema initialized")
PY

# --- Install systemd services ---
echo "[6/7] Installing systemd services..."

# Create user systemd directory
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$USER_UNIT_DIR"

# Only install the 5 core services for the validation phase
CORE_SERVICES=(
  "sniper-web"
  "sniper-stream"
  "sniper-analyzer"
  "sniper-paper"
  "sniper-alerter"
)

for svc in "${CORE_SERVICES[@]}"; do
  src="$APP_DIR/systemd/${svc}.service"
  if [ -f "$src" ]; then
    # Strip User= lines (user services don't need them), fix WantedBy, and
    # rewrite repo-local paths to the actual VPS install root.
    sed \
      -e '/^User=/d' \
      -e 's/^WantedBy=multi-user.target$/WantedBy=default.target/' \
      -e "s|/home/bigpapi/Desktop/Sniper|$APP_DIR|g" \
      -e "s|/home/pablo/.openclaw/workspace/sniper|$APP_DIR|g" \
      "$src" > "$USER_UNIT_DIR/${svc}.service"
    echo "  Installed ${svc}.service"
  else
    echo "  WARNING: ${svc}.service not found in systemd/"
  fi
done

# Enable lingering so user services run without login
sudo loginctl enable-linger "$(whoami)" 2>/dev/null || true

systemctl --user daemon-reload

# --- Daily backup cron ---
echo "[7/7] Setting up daily SQLite backup..."
BACKUP_DIR="$APP_DIR/data/backups/daily"
mkdir -p "$BACKUP_DIR"
chmod +x "$APP_DIR/scripts/daily_backup.sh"

# Install cron job (daily at 04:00 UTC)
CRON_LINE="0 4 * * * $APP_DIR/scripts/daily_backup.sh >> $BACKUP_DIR/backup.log 2>&1"
ROTATE_LINE="17 * * * * $APP_DIR/scripts/sniper_logrotate.sh >/dev/null 2>&1"
(crontab -l 2>/dev/null | grep -v "daily_backup.sh" | grep -v "sniper_logrotate.sh"; \
 echo "$CRON_LINE"; echo "$ROTATE_LINE") | crontab -
echo "  Daily backup cron installed (04:00 UTC)"
echo "  Hourly size-based log rotation installed"

echo ""
echo "========================================"
echo "  Bootstrap complete!"
echo "========================================"
echo ""
echo "Next steps:"
echo "  1. Edit .env with your API keys:"
echo "     nano $APP_DIR/.env"
echo ""
echo "  2. Start core services:"
echo "     systemctl --user start sniper-web sniper-stream sniper-analyzer sniper-paper sniper-alerter"
echo ""
echo "  3. Enable auto-start on boot:"
echo "     systemctl --user enable sniper-web sniper-stream sniper-analyzer sniper-paper sniper-alerter"
echo ""
echo "  4. Check status:"
echo "     systemctl --user status sniper-web sniper-stream sniper-analyzer sniper-paper sniper-alerter"
echo ""
echo "  5. View logs:"
echo "     journalctl --user -u sniper-stream -f"
echo ""
