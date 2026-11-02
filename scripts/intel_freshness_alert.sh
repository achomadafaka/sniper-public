#!/usr/bin/env bash
# Pages Telegram when the Intel producer stops writing news.
#
# Data-freshness based, NOT process-liveness based. Every other watchdog in this
# repo restarts a daemon; news_intel is a cron oneshot, so there is no process to
# watch. That is exactly why the 2026-04 -> 2026-07 outage was invisible: the tab
# kept serving a frozen snapshot and nothing anywhere noticed (audit §15).
#
# Plan: docs/superpowers/plans/2026-07-29-intel-geopolitics-revival.md (Task 7)
# Cron: 23 * * * * /usr/bin/env bash <repo>/scripts/intel_freshness_alert.sh >/dev/null 2>&1
set -u

ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd -P)"
PY="${SNIPER_PYTHON:-/home/bigpapi/.pyenv/versions/3.13.0/bin/python3}"
SENTINEL="$ROOT/data/intel_stale_paged"
STALE_HOURS="${INTEL_STALE_HOURS:-6}"

AGE="$(cd "$ROOT" && "$PY" -c "
import sqlite3, datetime as dt
c = sqlite3.connect('file:sniper_tracker.db?mode=ro', uri=True)
row = c.execute('SELECT MAX(fetched_at) FROM news_items').fetchone()
if not row or not row[0]:
    print(99999); raise SystemExit
ts = dt.datetime.fromisoformat(str(row[0]).replace('Z','+00:00'))
if ts.tzinfo is None: ts = ts.replace(tzinfo=dt.timezone.utc)
print(round((dt.datetime.now(dt.timezone.utc) - ts).total_seconds()/3600, 1))
" 2>/dev/null)"

# Unreadable DB / query failure: stay silent rather than page on our own bug.
[ -n "$AGE" ] || exit 0

if awk "BEGIN{exit !($AGE < $STALE_HOURS)}"; then
  rm -f "$SENTINEL"      # fresh again — re-arm the pager for the next outage
  exit 0
fi

[ -f "$SENTINEL" ] && exit 0   # already paged for this outage
date -u +%FT%TZ > "$SENTINEL"

PYTHONPATH="$ROOT" "$PY" -c "
from sniper.util.envfiles import load_env_files
load_env_files('$ROOT')
from sniper.services.memecoin_telegram import send_telegram
send_telegram('Intel producer STALE: newest news item is ${AGE}h old (threshold ${STALE_HOURS}h). '
              'The Geopolitics tab is now serving archive data.')" \
  >/dev/null 2>&1 || true
