#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

mkdir -p "$USER_UNIT_DIR"

write_user_unit() {
  local src="$1"
  local out="$2"
  sed \
    -e '/^User=/d' \
    -e 's/^WantedBy=multi-user.target$/WantedBy=default.target/' \
    -e "s|/home/bigpapi/Desktop/Sniper|$ROOT_DIR|g" \
    -e "s|/home/pablo/.openclaw/workspace/sniper|$ROOT_DIR|g" \
    "$src" > "$TMP_DIR/$out"
  install -m 0644 "$TMP_DIR/$out" "$USER_UNIT_DIR/$out"
}

echo "Installing user services into $USER_UNIT_DIR"
write_user_unit "$ROOT_DIR/systemd/sniper-web.service" "sniper-web.service"
write_user_unit "$ROOT_DIR/systemd/sniper-analyzer.service" "sniper-analyzer.service"
write_user_unit "$ROOT_DIR/systemd/sniper-stream.service" "sniper-stream.service"
write_user_unit "$ROOT_DIR/systemd/sniper-paper.service" "sniper-paper.service"
write_user_unit "$ROOT_DIR/systemd/sniper-alerter.service" "sniper-alerter.service"
write_user_unit "$ROOT_DIR/systemd/sniper-dashboard.service" "sniper-dashboard.service"
write_user_unit "$ROOT_DIR/systemd/sniper-real.service" "sniper-real.service"
write_user_unit "$ROOT_DIR/systemd/sniper-strategy.service" "sniper-strategy.service"

systemctl --user daemon-reload

echo "Enabling and restarting services"
systemctl --user enable sniper-web.service sniper-analyzer.service sniper-stream.service sniper-paper.service sniper-alerter.service sniper-dashboard.service sniper-real.service sniper-strategy.service >/dev/null
systemctl --user restart sniper-web.service sniper-analyzer.service sniper-stream.service sniper-paper.service sniper-alerter.service sniper-dashboard.service sniper-real.service sniper-strategy.service

echo "Done. Current status:"
systemctl --user --no-pager --full status sniper-web.service sniper-analyzer.service sniper-stream.service sniper-paper.service sniper-alerter.service sniper-dashboard.service sniper-real.service sniper-strategy.service | sed -n '1,200p'
