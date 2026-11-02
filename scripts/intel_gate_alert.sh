#!/usr/bin/env bash
# Pages Telegram ONCE when the pre-registered Intel Gate A fires, so the read does
# not sit unread. Cron-hourly; the sentinel makes it one-shot.
#
# Gate A decides whether the Intel program is viable at all: if the market has
# already repriced by the time we decide, no picking improvement recovers it.
#
# Plan: docs/superpowers/plans/2026-07-29-intel-geopolitics-revival.md (Task 11)
# Cron: 43 * * * * /usr/bin/env bash <repo>/scripts/intel_gate_alert.sh >/dev/null 2>&1
set -u

ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd -P)"
PY="${SNIPER_PYTHON:-/home/bigpapi/.pyenv/versions/3.13.0/bin/python3}"
SENTINEL="$ROOT/data/intel_gate_a_fired"

[ -f "$SENTINEL" ] && exit 0

# stderr is kept: a crash must not be indistinguishable from "gate not reached"
ERRLOG="$(mktemp)"
OUT="$(cd "$ROOT" && PYTHONPATH="$ROOT" "$PY" scripts/intel_gate_read.py --gate a 2>"$ERRLOG")"
RC=$?
if [ "$RC" -ne 0 ] && [ -s "$ERRLOG" ] && grep -qi "traceback" "$ERRLOG"; then
  echo "intel_gate_read crashed:" >&2
  cat "$ERRLOG" >&2
fi
rm -f "$ERRLOG"
[ "$RC" -eq 0 ] || exit 0   # gate not reached yet — stay silent, no peeking

date -u +%FT%TZ > "$SENTINEL"

PYTHONPATH="$ROOT" "$PY" -c "
import sys
from sniper.util.envfiles import load_env_files
load_env_files('$ROOT')
from sniper.services.memecoin_telegram import send_telegram
send_telegram('Intel GATE A fired — read it now:\n' + sys.argv[1])" "$OUT" \
  >/dev/null 2>&1 || true
