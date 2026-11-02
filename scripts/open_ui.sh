#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PID_FILE="$ROOT_DIR/logs/web.pid"
LOG_FILE="$ROOT_DIR/logs/web.log"
ENV_FILE="$ROOT_DIR/.env"
mkdir -p "$ROOT_DIR/logs"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

# Build URL AFTER sourcing .env so the port tracks FLASK_PORT (was hardcoded to :5000). Always reach
# the server over loopback (works whether it binds 127.0.0.1 or 0.0.0.0; xdg-open needs a real host).
URL="http://127.0.0.1:${FLASK_PORT:-5000}"
HEALTH_URL="$URL/health"

PYTHON_BIN="${PYTHON_BIN:-}"

select_python() {
  if [[ -n "$PYTHON_BIN" ]]; then
    "$PYTHON_BIN" -c "import flask" >/dev/null 2>&1
    return
  fi

  local candidate
  for candidate in "$ROOT_DIR/.venv/bin/python" python3 python; do
    if [[ "$candidate" == */* ]]; then
      [[ -x "$candidate" ]] || continue
    else
      command -v "$candidate" >/dev/null 2>&1 || continue
    fi
    if "$candidate" -c "import flask" >/dev/null 2>&1; then
      PYTHON_BIN="$candidate"
      return
    fi
  done

  echo "No Python interpreter with Flask found. Install requirements.txt or set PYTHON_BIN." >&2
  return 1
}

is_up() {
  curl -fsS "$HEALTH_URL" >/dev/null 2>&1
}

pid_is_web() {
  local pid="$1"
  [[ -n "$pid" ]] || return 1
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  ps -p "$pid" -o args= 2>/dev/null | grep -q "sniper.web.server"
}

read_pid() {
  cat "$PID_FILE" 2>/dev/null || true
}

find_running_pid() {
  local pid=""
  pid="$(read_pid)"
  if pid_is_web "$pid"; then
    echo "$pid"
    return 0
  fi

  # Fallback: discover an already-running server process and repair PID file.
  pid="$(pgrep -f "[p]ython[0-9]* .*sniper\\.web\\.server" | head -n 1 || true)"
  if pid_is_web "$pid"; then
    echo "$pid" > "$PID_FILE"
    echo "$pid"
    return 0
  fi

  return 1
}

start_web() {
  cd "$ROOT_DIR"
  local old_pid=""
  old_pid="$(find_running_pid || true)"
  if pid_is_web "$old_pid"; then
    return
  fi
  select_python
  rm -f "$PID_FILE"
  LOG_LEVEL="${LOG_LEVEL:-INFO}" \
  DB_PATH="${DB_PATH:-sniper_tracker.db}" \
  FLASK_HOST="${FLASK_HOST:-127.0.0.1}" \
  FLASK_PORT="${FLASK_PORT:-5000}" \
  FLASK_THREADED="${FLASK_THREADED:-true}" \
  SNIPER_API_KEY="${SNIPER_API_KEY:-}" \
  SNIPER_API_VERSION="${SNIPER_API_VERSION:-2026.02-sniper-v1}" \
  SNIPER_API_KEY_PROTECT_MUTATING="${SNIPER_API_KEY_PROTECT_MUTATING:-false}" \
    setsid "$PYTHON_BIN" -m sniper.web.server >> "$LOG_FILE" 2>&1 < /dev/null &
  echo $! > "$PID_FILE"
}

stop_web() {
  local pids=()
  mapfile -t pids < <(pgrep -f "[p]ython[0-9]* .*sniper\\.web\\.server" || true)
  for old_pid in "${pids[@]}"; do
    if ! pid_is_web "$old_pid"; then
      continue
    fi
    kill "$old_pid" 2>/dev/null || true
    sleep 0.3
    if pid_is_web "$old_pid"; then
      kill -9 "$old_pid" 2>/dev/null || true
    fi
  done
  rm -f "$PID_FILE"
}

status_web() {
  local old_pid=""
  old_pid="$(find_running_pid || true)"
  if pid_is_web "$old_pid" && is_up; then
    echo "running pid=$old_pid url=$URL"
    return 0
  fi
  echo "stopped url=$URL"
  return 1
}

cmd="${1:-open}"
case "$cmd" in
  start)
    start_web
    ;;
  stop)
    stop_web
    echo "stopped"
    exit 0
    ;;
  restart)
    stop_web
    start_web
    ;;
  status)
    status_web
    exit $?
    ;;
  open)
    if ! is_up; then
      start_web
    fi
    ;;
  *)
    echo "Usage: $0 [open|start|stop|restart|status]" >&2
    exit 2
    ;;
esac

for _ in {1..20}; do
  if is_up; then
    break
  fi
  sleep 0.5
done
if ! is_up; then
  echo "Web server failed to start. Check $LOG_FILE" >&2
  exit 1
fi

if command -v xdg-open >/dev/null 2>&1; then
  if [[ "$cmd" == "open" || "$cmd" == "restart" || "$cmd" == "start" ]]; then
    xdg-open "$URL" >/dev/null 2>&1 || true
  fi
fi
echo "$URL"
