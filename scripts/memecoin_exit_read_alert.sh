#!/usr/bin/env bash
# Pages Telegram ONCE when the sealed post-cutover exit read gate fires (n>=30 closes), so the
# pre-registered read doesn't sit unread. Cron-hourly; the sentinel file makes it one-shot.
# Spec: docs/superpowers/specs/2026-07-21-memecoin-postcutover-exit-read.md
set -u

ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd -P)"
PY="${SNIPER_PYTHON:-/home/bigpapi/.pyenv/versions/3.13.0/bin/python3}"
SENTINEL="$ROOT/data/memecoin_exit_read_fired"

[ -f "$SENTINEL" ] && exit 0

OUT="$(cd "$ROOT" && "$PY" scripts/memecoin_postcutover_exit_read.py 2>/dev/null)"
RC=$?
[ "$RC" -eq 0 ] || exit 0   # gate not reached yet — stay silent

date -u +%FT%TZ > "$SENTINEL"
HEAD="$(printf '%s\n' "$OUT" | head -8)"
PYTHONPATH="$ROOT" "$PY" -c "
import sys
from sniper.util.envfiles import load_env_files
load_env_files('$ROOT')
from sniper.services.memecoin_telegram import send_telegram
send_telegram('memecoin post-cutover exit read GATE FIRED (n>=30) — read it now:\n' + sys.argv[1]
              + '\nFull output: python3 scripts/memecoin_postcutover_exit_read.py')" "$HEAD" \
  >/dev/null 2>&1 || true
