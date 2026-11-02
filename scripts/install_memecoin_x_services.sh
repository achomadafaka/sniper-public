#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
TMP_DIR="$(mktemp -d)"
ENABLE=0
ENABLE_API=0
CHECK=0
trap 'rm -rf "$TMP_DIR"' EXIT

usage() {
  echo "Usage: $0 [--check] [--enable] [--enable-api]" >&2
}

while (($#)); do
  case "$1" in
    --check) CHECK=1 ;;
    --enable) ENABLE=1 ;;
    --enable-api) ENABLE_API=1 ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
  shift
done

resolve_python() {
  local candidate resolved
  local candidates=()
  if [[ -n "${MEMECOIN_X_PYTHON:-}" ]]; then
    candidates+=("$MEMECOIN_X_PYTHON")
  fi
  candidates+=("$ROOT_DIR/.venv/bin/python")
  if command -v python3 >/dev/null 2>&1; then
    candidates+=("$(command -v python3)")
  fi
  for candidate in "${candidates[@]}"; do
    [[ -x "$candidate" ]] || continue
    resolved=$("$candidate" -c 'import sys; print(sys.executable)' 2>/dev/null) || continue
    [[ -x "$resolved" ]] || resolved="$candidate"
    if "$resolved" -c 'import flask, bs4' >/dev/null 2>&1; then
      printf '%s\n' "$resolved"
      return 0
    fi
  done
  return 1
}

if ! PYTHON_BIN=$(resolve_python); then
  echo "No Python interpreter can import flask and bs4. Install requirements.txt or set MEMECOIN_X_PYTHON." >&2
  exit 1
fi
echo "Runtime Python OK: $PYTHON_BIN"
if (( CHECK == 1 )); then
  exit 0
fi

mkdir -p "$USER_UNIT_DIR"

write_user_unit() {
  local name="$1"
  sed -e "s|/home/bigpapi/Desktop/Sniper/.venv/bin/python|$PYTHON_BIN|g" \
    -e "s|/home/bigpapi/Desktop/Sniper|$ROOT_DIR|g" \
    "$ROOT_DIR/systemd/$name" > "$TMP_DIR/$name"
  install -m 0644 "$TMP_DIR/$name" "$USER_UNIT_DIR/$name"
}

write_user_unit sniper-memecoin-x-receiver.service
write_user_unit sniper-memecoin-x-correlator.service
write_user_unit sniper-memecoin-x-correlator.timer
write_user_unit sniper-memecoin-x-signals.service
write_user_unit sniper-memecoin-x-signals.timer
write_user_unit sniper-x-intel-capture.service
write_user_unit sniper-x-intel-notifier.service
write_user_unit sniper-x-intel-notifier.timer

systemctl --user daemon-reload

if (( ENABLE == 1 )); then
  systemctl --user enable --now \
    sniper-memecoin-x-receiver.service \
    sniper-memecoin-x-correlator.timer \
    sniper-memecoin-x-signals.timer
else
  echo "Units installed but not enabled. Run $0 --enable after operator review."
fi

if (( ENABLE_API == 1 )); then
  X_INTEL_ENV_FILE="$ROOT_DIR/.runtime/x-intel.env"
  if [[ ! -f "$X_INTEL_ENV_FILE" ]]; then
    echo "Missing X Intel environment file: $X_INTEL_ENV_FILE" >&2
    exit 1
  fi
  if [[ "$(stat -c %a "$X_INTEL_ENV_FILE")" != "600" ]]; then
    echo "X Intel environment file must have mode 0600: $X_INTEL_ENV_FILE" >&2
    exit 1
  fi
  systemctl --user enable --now \
    sniper-x-intel-capture.service \
    sniper-x-intel-notifier.timer
else
  echo "X API capture installed but disabled. Use --enable-api after adding the 0600 environment file."
fi
