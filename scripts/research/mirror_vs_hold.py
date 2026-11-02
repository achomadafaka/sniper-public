#!/usr/bin/env python3
"""Source-sell mirror exits against holding the same bag: exact +600 s quotes where recorded, mids
for the first two minutes.

Pre-registered with Alpha on 2026-09-27 before any outcome was read. Report only: the mirror is live
by operator choice (flag F, 2026-09-27), so this measures it and recommends nothing.

  Population  funded Solana closes with exit_reason mirror or mirror_source_sell, closed_at >= --since.
  E           the final exit fill notional (memecoin_fills, max id among exit kinds).
  H600        EXACT: memecoin_post_tp_results.quote_out_usdc at horizon_s=600, status 'healthy', for
              the whole bag the schedule names (memecoin_post_tp_schedules.token_qty_atomic). The
              schedule's qty and exit_usdc are checked against the fill before it is used.
  H60/H120    MID: memecoin_position_marks.mid (a price, no impact, so it OVERSTATES what holding
              would have fetched), the first sweep_ts at or after close + 60 s (+120 s) and within
              30 s of it, times the exit fill qty. Marks survive close only for
              MEMECOIN_POST_CLOSE_MARK_S (default 120 s; absent from .env on 2026-09-27).
  Delta       E - H. Positive = the mirror exit fetched more than holding would have.

Ledger read-only (read-only URI). No network.
    python3 scripts/research/mirror_vs_hold.py [--since 2026-09-20T21:05:00]
    python3 scripts/research/mirror_vs_hold.py --selftest
"""

import sys
import pathlib as _research_pathlib
sys.path.insert(0, str(_research_pathlib.Path(__file__).resolve().parents[2]))
from sniper.research.wallet_execution.publication import legacy_launch as _legacy_launch
_legacy_launch(__name__)
import argparse
import contextlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
EXIT_KINDS = ("bracket_exit", "mirror_exit", "partial_exit", "derisk", "manual_close")
MARK_TOL_S = 30.0


def ts(text):
    return datetime.fromisoformat(str(text).replace("Z", "+00:00"))


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def mark_at(marks, target):
    """marks: (sweep_ts, mid). The first positive mid at or after `target`, within MARK_TOL_S."""
    ok = [m for m in marks if m[1] and m[1] > 0 and ts(m[0]) >= target
          and (ts(m[0]) - target).total_seconds() <= MARK_TOL_S]
    return min(ok, key=lambda m: ts(m[0])) if ok else None


def schedule_matches(sched_qty_atomic, decimals, sched_exit_usdc, fill_qty, fill_notional):
    """True when the +600 s schedule describes the same bag and proceeds as the exit fill (0.5%)."""
    try:
        qty = int(sched_qty_atomic) / 10 ** int(decimals)
        return (abs(qty / float(fill_qty) - 1) <= 0.005
                and abs(float(sched_exit_usdc) / float(fill_notional) - 1) <= 0.005)
    except (TypeError, ValueError, ZeroDivisionError):
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-09-20T21:05:00")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    sums = {"h600": [], "h60": [], "h120": []}
    with contextlib.closing(sqlite3.connect("file:%s?mode=ro" % (REPO / "sniper_tracker.db"),
                                            uri=True, timeout=10)) as c:
        pos = c.execute(
            "SELECT id, source_handle, mint, exit_reason, closed_at, round(realized_pnl, 2) "
            "FROM memecoin_positions WHERE execution_mode='real' AND is_open=0 AND network_id='solana' "
            "AND exit_reason IN ('mirror', 'mirror_source_sell') AND closed_at >= ? ORDER BY id",
            (a.since,)).fetchall()
        print("mirror closes: n=%d since %s" % (len(pos), a.since))
        for pid, handle, mint, reason, closed_at, pnl in pos:
            fill = c.execute(
                "SELECT notional, qty FROM memecoin_fills WHERE position_id=? AND kind IN (%s) "
                "ORDER BY id DESC LIMIT 1" % ",".join("?" * len(EXIT_KINDS)),
                (pid, *EXIT_KINDS)).fetchone()
            if fill is None:
                print("%6d %-16s no exit fill" % (pid, handle))
                continue
            e_usd, qty = float(fill[0]), float(fill[1])
            sched = c.execute(
                "SELECT s.token_qty_atomic, s.token_decimals, s.exit_usdc, r.status, r.quote_out_usdc "
                "FROM memecoin_post_tp_schedules s LEFT JOIN memecoin_post_tp_results r "
                "ON r.followup_id = s.followup_id WHERE s.position_id=? AND s.horizon_s=600",
                (pid,)).fetchone()
            h600, note = None, "no +600 schedule"
            if sched is not None:
                if not schedule_matches(sched[0], sched[1], sched[2], qty, e_usd):
                    note = "schedule != fill"
                elif sched[3] != "healthy" or sched[4] is None:
                    note = "result %s" % sched[3]
                else:
                    h600, note = float(sched[4]), "exact"
            close = ts(closed_at)
            marks = c.execute(
                "SELECT sweep_ts, mid FROM memecoin_position_marks WHERE chain_id='solana' AND mint=? "
                "AND sweep_ts >= ? AND sweep_ts <= ?",
                (mint, iso(close + timedelta(seconds=60)),
                 iso(close + timedelta(seconds=120 + MARK_TOL_S)))).fetchall()
            m60 = mark_at(marks, close + timedelta(seconds=60))
            m120 = mark_at(marks, close + timedelta(seconds=120))
            h60 = m60[1] * qty if m60 else None
            h120 = m120[1] * qty if m120 else None
            for key, h in (("h600", h600), ("h60", h60), ("h120", h120)):
                if h is not None:
                    sums[key].append(e_usd - h)
            print("%6d %-16s %-18s pnl %+7.2f E %7.2f | H600 %s (%s) | H60 mid %s | H120 mid %s" % (
                pid, handle, reason, pnl or 0.0, e_usd,
                "%7.2f d%+7.2f" % (h600, e_usd - h600) if h600 is not None else "   -   ", note,
                "%7.2f d%+7.2f" % (h60, e_usd - h60) if h60 is not None else "-",
                "%7.2f d%+7.2f" % (h120, e_usd - h120) if h120 is not None else "-"))
    for key, d in sums.items():
        if d:
            print("%s: n=%d delta sum %+.2f, exit beat hold on %d/%d" % (
                key, len(d), sum(d), sum(1 for x in d if x > 0), len(d)))
        else:
            print("%s: n=0" % key)


def selftest():
    close = ts("2026-09-27T20:00:00+00:00")
    marks = [("2026-09-27T20:00:59+00:00", 1.0), ("2026-09-27T20:01:00+00:00", 0.0),
             ("2026-09-27T20:01:10+00:00", 0.8), ("2026-09-27T20:01:02+00:00", 0.9),
             ("2026-09-27T20:02:40+00:00", 0.5)]
    # 20:00:59 is before +60 s; the 0.0 mid at exactly +60 s is not a price, so the first priced
    # mark at/after +60 s is 20:01:02 (a filter that lets 0.0 through picks 20:01:00 and fails).
    assert mark_at(marks, close + timedelta(seconds=60))[1] == 0.9
    # +120 s = 20:02:00: 20:02:40 is 40 s late, beyond the 30 s tolerance.
    assert mark_at(marks, close + timedelta(seconds=120)) is None
    assert schedule_matches("1000000000", 6, 77.24, 1000.0, 77.24)
    assert not schedule_matches("2000000000", 6, 77.24, 1000.0, 77.24)       # a different bag
    assert not schedule_matches("1000000000", 6, 90.00, 1000.0, 77.24)       # different proceeds
    assert not schedule_matches(None, 6, 77.24, 1000.0, 77.24)
    print("selftest ok")



# Operator protocol: legacy hypothetical conclusions cannot publish by default.
from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports
_quarantine_exports(globals(), ('main',))

if __name__ == "__main__":
    main()
