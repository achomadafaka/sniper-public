#!/usr/bin/env python3
"""The Solana cohort canary's report (spec 2026-10-09 Solana pre-signal feed, step 1).

Read-only on the cohort DB and on the ledger. Makes no network call. It reports:
- what the stream cost (bytes, credits) and the monthly credits and plan of each lifetime choice;
- the binding rate per trigger, and lost bindings (graduation_unresolved);
- cap hits and evictions;
- the stream's connected live share; legacy epochs without connection boundaries stay unknown;
- coverage of funded Solana attempts AT decision_started_at, per trigger, with first sightings and
  never-sighted coins apart.

    python3 scripts/research/sol_cohort_canary_report.py --since 2026-10-10T00:00:00+00:00 \\
        --until 2026-10-11T00:00:00+00:00
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from sniper.services import memecoin_sol_cohort as co  # noqa: E402

MONTH_S = 30 * 86400.0


def _ts(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _interval_seconds(intervals, lo, hi):
    """Union, so reconnect overlap or simultaneous observers cannot invent uptime."""
    total, edge = 0.0, lo
    for start, end in sorted((max(a, lo), min(b, hi)) for a, b in intervals if b is not None):
        if end > max(start, edge):
            total += end - max(start, edge)
            edge = end
    return total


def report(store, ledger, since, until):
    lo, hi = _ts(since), _ts(until)
    minute_lo, minute_hi = int(lo // 60), int((hi - 1e-6) // 60)
    meter = store.execute(
        "SELECT COUNT(*), SUM(bytes), SUM(messages), SUM(rpc_calls) FROM meter WHERE minute >= ? "
        "AND minute <= ?", (minute_lo, minute_hi)).fetchone()
    minutes, bytes_, messages, rpc = (meter[0] or 0), (meter[1] or 0), (meter[2] or 0), (meter[3] or 0)
    tables = {r[0] for r in store.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    fragments = (store.execute('SELECT started, ended, elapsed_s, rpc_calls, rpc_credits, '
                               'unknown_rpc_calls, bytes, tariff_version FROM meter_fragments '
                               'WHERE minute>=? AND minute<=?', (minute_lo, minute_hi)).fetchall()
                 if 'meter_fragments' in tables else [])
    metered_seconds = _interval_seconds([(r[0], r[1]) for r in fragments
                                        if r[0] is not None and r[1] is not None], lo, hi)
    unknown_rpc = sum(r[5] for r in fragments) + max(0, rpc - sum(r[3] for r in fragments))
    rpc_credits = sum(r[4] or 0 for r in fragments)
    unknown_intervals = (not fragments or any(r[2] is None for r in fragments)
                         or sum(r[3] for r in fragments) != rpc or sum(r[6] for r in fragments) != bytes_
                         or any(r[0] is not None and (r[0] < lo or r[1] > hi) for r in fragments))
    span_s = None if unknown_intervals or metered_seconds <= 0 else metered_seconds
    stream_month = None if span_s is None else bytes_ / 100_000.0 * 2.0 * MONTH_S / span_s
    rpc_month = None if span_s is None or unknown_rpc else rpc_credits * MONTH_S / span_s
    counters = dict(store.execute("SELECT name, value FROM counters").fetchall())
    lifetimes = (co.lifetime_projection(counters, stream_credits_month=stream_month,
                                       rpc_credits_month=rpc_month)
                 if stream_month is not None and rpc_month is not None else
                 {name: None for name in ('1h', '6h', '24h')})
    epoch_columns = {r[1] for r in store.execute("PRAGMA table_info(epochs)")}
    observed = "connected_at, connection_observed" if {"connected_at", "connection_observed"} <= epoch_columns else "NULL, 0"
    observed_until = 'COALESCE(observed_until, ended)' if 'observed_until' in epoch_columns else 'ended'
    epochs = store.execute("SELECT started, ended, error, " + observed + ', ' + observed_until +
                           " FROM epochs WHERE (ended IS NULL OR ended > ? OR (ended = started AND started >= ?)) "
                           "AND started < ?", (lo, lo, hi)).fetchall()
    unknown_epochs = sum(not r[4] for r in epochs)
    observed_seconds = _interval_seconds([(r[0], r[5]) for r in epochs if r[4]], lo, hi)
    live_seconds = _interval_seconds([(r[3], r[1] if r[1] is not None else r[5])
                                     for r in epochs if r[4] and r[3] is not None], lo, hi)
    window_s = max(0.0, hi - lo)
    unknown_seconds = max(0.0, window_s - observed_seconds)
    bindings = {}
    for trigger, binding, n in store.execute(
            "SELECT trigger_first, COALESCE(binding, 'never_wanted'), COUNT(*) FROM coins "
            "GROUP BY 1, 2"):
        bindings.setdefault(trigger, {})[binding] = n
    return {
        "window": {"since": since, "until": until, "metered_minutes": minutes,
                   "metered_seconds": metered_seconds, 'meter_observation_unknown': bool(unknown_intervals)},
        "cost": {"bytes": bytes_, "messages": messages, "rpc_calls": rpc,
                 'known_rpc_credits': rpc_credits, 'unknown_rpc_calls': unknown_rpc,
                 'tariff_version': co.RPC_TARIFF_VERSION,
                 'observed_tariff_versions': sorted({r[7] for r in fragments}),
                 'basis': 'attempted requests at published rates; minute-window totals, not an invoice',
                 "stream_credits_month": None if stream_month is None else round(stream_month),
                 "rpc_credits_month": None if rpc_month is None else round(rpc_month)},
        "lifetimes": {name: {"credits_month": None if v is None else round(v),
                             "plan": (None, None) if v is None else co.plan_for(v)}
                      for name, v in lifetimes.items()},
        "bindings_by_trigger": bindings,
        "counters": {k: counters.get(k) for k in ("cap_hits", "evictions", "src:graduations",
                                                  "src:rebinds", "src:grad_unresolved",
                                                  "src:stream_errors", "src:rpc_429")},
        "stream": {"live_share": None if unknown_epochs or unknown_seconds > 1e-6 or not epochs else
                                 round(live_seconds / max(1.0, window_s), 4),
                   'observed_live_share': None if observed_seconds <= 0 else round(live_seconds / observed_seconds, 4),
                   'observation_coverage': None if window_s <= 0 else round(observed_seconds / window_s, 4),
                   'unknown_seconds': unknown_seconds, 'open_epochs': sum(r[1] is None for r in epochs),
                   "observed_live_seconds": live_seconds, "unknown_epochs": unknown_epochs,
                   "epochs": len(epochs), "epoch_errors": sum(r[2] is not None for r in epochs)},
        "coverage": co.coverage(ledger, store, since=since, until=until),
    }


def main():
    from sniper.persistence.db_core import get_db_path, get_readonly_connection
    from sniper.util.state_paths import state_path
    p = argparse.ArgumentParser()
    p.add_argument("--since", required=True)
    p.add_argument("--until", required=True)
    p.add_argument("--store", default=str(state_path("data/memecoin_sol_cohort.db")))
    p.add_argument("--ledger", default=get_db_path())
    a = p.parse_args()
    store = sqlite3.connect("file:%s?mode=ro" % a.store, uri=True)
    store.row_factory = sqlite3.Row
    json.dump(report(store, get_readonly_connection(a.ledger), a.since, a.until), sys.stdout, indent=1)
    print()


if __name__ == "__main__":
    main()
