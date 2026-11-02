#!/usr/bin/env python3
"""What did the vol_death VETO cost? Exit at the first veto versus what we actually realized.

Pre-registered with Alpha on 2026-09-27 before any outcome was read:

  Population  funded Solana positions (execution_mode='real', network_id='solana', closed) with at
              least one memecoin_exit_trigger_audit row reason='vol_death', allowed=0, detail
              starting 'exact_quote_moving' or 'state_moving'. Window: 2026-08-26T13:08:38Z (the
              first exit_decision route-series row) to the read time.
  First veto  the earliest SURVIVING such row (ORDER BY checked_at, id). The audit upserts per
              (position, reason, minute), so a veto overwritten in its minute is lost and a
              surviving row carries the LAST check of its minute.
  V_veto      expected_usdc of the memecoin_route_series row seam='exit_decision' for the same
              position with the highest id among rows observed at or before checked_at. Age above
              15 s is STALE: reported separately, excluded from the primary.
  R           sum of exit-fill notional (bracket_exit, mirror_exit, partial_exit, derisk,
              manual_close) created after checked_at. Earlier exits are common to both arms.
  Delta       V_veto - R. Positive = exiting at the first veto would have paid more.
  Primary     exit_reason != 'manual', age <= 15 s. Claim "the veto costs money" is supported only
              if the bootstrap 95% CI lower bound on the mean delta is > 0 (10k draws, seed 1).
  Secondary   V_veto scaled by the position's own notional-weighted exit execution_slippage_pct;
              splits by veto detail and by final exit_reason; manual and stale rows listed.

Ledger reads only (read-only URI). No network.
    python3 scripts/research/vetoed_voldeath.py
    python3 scripts/research/vetoed_voldeath.py --selftest
"""

import sys
import pathlib as _research_pathlib
sys.path.insert(0, str(_research_pathlib.Path(__file__).resolve().parents[2]))
from sniper.research.wallet_execution.publication import legacy_launch as _legacy_launch
_legacy_launch(__name__)
import argparse
import contextlib
import random
import sqlite3
import statistics
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WINDOW_START = "2026-08-26T13:08:38"
VETO_DETAILS = ("exact_quote_moving", "state_moving")
EXIT_KINDS = ("bracket_exit", "mirror_exit", "partial_exit", "derisk", "manual_close")
STALE_S = 15.0


def ts(text):
    return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()


def veto_kind(detail):
    """'exact_quote_moving' | 'state_moving' | None, from an audit detail like 'state_moving silence=31.2s'."""
    word = str(detail or "").split(" ", 1)[0]
    return word if word in VETO_DETAILS else None


def first_veto(rows):
    """rows: (id, checked_at, detail). The earliest surviving veto row, or None."""
    vetoes = [r for r in rows if veto_kind(r[2])]
    return min(vetoes, key=lambda r: (r[1], r[0])) if vetoes else None


def quote_at(rows, checked_at):
    """rows: (id, observed_at, expected_usdc). Highest id observed at or before checked_at."""
    before = [r for r in rows if ts(r[1]) <= ts(checked_at)]
    return max(before, key=lambda r: r[0]) if before else None


def realized_after(fills, checked_at):
    """fills: (kind, notional, created_at, slippage_pct). (sum notional after, weighted slip %)."""
    after = [f for f in fills if f[0] in EXIT_KINDS and f[2] and ts(f[2]) > ts(checked_at)]
    total = sum(float(f[1] or 0) for f in after)
    weighted = [(float(f[1] or 0), f[3]) for f in after if f[3] is not None and f[1]]
    w = sum(n for n, _ in weighted)
    slip = sum(n * s for n, s in weighted) / w if w else None
    return total, slip, len(after)


def boot_ci(values, draws=10_000, seed=1):
    if len(values) < 2:
        return None, None
    rng = random.Random(seed)
    n = len(values)
    means = sorted(sum(rng.choice(values) for _ in range(n)) / n for _ in range(draws))
    return means[int(0.025 * draws)], means[int(0.975 * draws) - 1]


def summarize(label, deltas):
    if not deltas:
        return "%s: n=0" % label
    lo, hi = boot_ci(deltas)
    return ("%s: n=%d sum=%+.2f mean=%+.2f median=%+.2f positive=%d/%d CI95_mean=[%s, %s]" % (
        label, len(deltas), sum(deltas), statistics.mean(deltas), statistics.median(deltas),
        sum(1 for d in deltas if d > 0), len(deltas),
        "%+.2f" % lo if lo is not None else "na", "%+.2f" % hi if hi is not None else "na"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--rows", action="store_true", help="print one line per position")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    out = []
    with contextlib.closing(sqlite3.connect("file:%s?mode=ro" % (REPO / "sniper_tracker.db"),
                                            uri=True, timeout=10)) as c:
        audit = {}
        for pid, rid, checked, detail in c.execute(
                "SELECT position_id, id, checked_at, detail FROM memecoin_exit_trigger_audit "
                "WHERE reason='vol_death' AND allowed=0 AND checked_at >= ?", (WINDOW_START,)):
            audit.setdefault(pid, []).append((rid, checked, detail))
        for pid, rows in sorted(audit.items()):
            veto = first_veto(rows)
            if veto is None:
                continue
            pos = c.execute(
                "SELECT id, source_handle, entry_notional, realized_pnl, exit_reason, closed_at "
                "FROM memecoin_positions WHERE id=? AND execution_mode='real' AND network_id='solana' "
                "AND is_open=0", (pid,)).fetchone()
            if pos is None:
                continue
            quotes = c.execute(
                "SELECT id, observed_at, expected_usdc FROM memecoin_route_series "
                "WHERE position_id=? AND seam='exit_decision' AND observed_at <= ?",
                (pid, veto[1])).fetchall()
            q = quote_at(quotes, veto[1])
            fills = c.execute("SELECT kind, notional, created_at, execution_slippage_pct "
                              "FROM memecoin_fills WHERE position_id=?", (pid,)).fetchall()
            r_after, slip, n_after = realized_after(fills, veto[1])
            age = ts(veto[1]) - ts(q[1]) if q else None
            out.append({"id": pid, "handle": pos[1], "entry": pos[2], "pnl": pos[3],
                        "exit": pos[4], "veto_at": veto[1], "kind": veto_kind(veto[2]),
                        "n_vetoes": sum(1 for r in rows if veto_kind(r[2])),
                        "v": q[2] if q else None, "age": age, "r": r_after, "n_exits": n_after,
                        "slip": slip})
    primary, stale, manual, noquote, noexit = [], [], [], [], []
    for r in out:
        if r["v"] is None:
            noquote.append(r)
        elif r["n_exits"] == 0:
            noexit.append(r)
        elif r["exit"] == "manual":
            manual.append(r)
        elif r["age"] > STALE_S:
            stale.append(r)
        else:
            primary.append(r)
        if a.rows:
            print("%6d %-16s veto %s %-18s n_veto=%d V=%s age=%s R=%.2f exits=%d slip=%s exit=%s pnl=%s" % (
                r["id"], r["handle"], r["veto_at"][:19], r["kind"], r["n_vetoes"],
                "%.2f" % r["v"] if r["v"] is not None else None,
                "%.1f" % r["age"] if r["age"] is not None else None, r["r"], r["n_exits"],
                "%.2f" % r["slip"] if r["slip"] is not None else None, r["exit"],
                "%.2f" % r["pnl"] if r["pnl"] is not None else None))
    print("positions with a surviving veto: %d | primary %d, stale %d, manual %d, no quote %d, "
          "no exit fill after veto %d" % (len(out), len(primary), len(stale), len(manual),
                                          len(noquote), len(noexit)))
    if out:
        print("window: first veto %s .. last veto %s" % (min(r["veto_at"] for r in out)[:19],
                                                         max(r["veto_at"] for r in out)[:19]))
    print(summarize("PRIMARY delta = V_veto - R", [r["v"] - r["r"] for r in primary]))
    print(summarize("SECONDARY slippage-adjusted",
                    [r["v"] * (1 + r["slip"] / 100.0) - r["r"] for r in primary if r["slip"] is not None]))
    for kind in VETO_DETAILS:
        print(summarize("  veto=%s" % kind, [r["v"] - r["r"] for r in primary if r["kind"] == kind]))
    for reason in sorted({r["exit"] or "None" for r in primary}):
        print(summarize("  exit=%s" % reason,
                        [r["v"] - r["r"] for r in primary if (r["exit"] or "None") == reason]))
    print(summarize("STALE (age > %.0f s)" % STALE_S, [r["v"] - r["r"] for r in stale]))
    print(summarize("MANUAL", [r["v"] - r["r"] for r in manual]))
    if primary:
        print("quote age at veto (s): p50 %.1f p90 %.1f" % (
            statistics.median([r["age"] for r in primary]),
            sorted(r["age"] for r in primary)[int(0.9 * (len(primary) - 1))]))


def selftest():
    # Ids deliberately out of time order: the earliest veto (19:10:59) has the HIGHER id, so a
    # first_veto keyed on id instead of checked_at picks the wrong row and fails here.
    rows = [(3, "2026-09-27T19:12:59+00:00", "state_moving silence=31.0s"),
            (5, "2026-09-27T19:10:59+00:00", "exact_quote_moving silence=29.8s"),
            (4, "2026-09-27T19:11:30+00:00", "stream_unvouched silence=40.0s")]
    assert first_veto(rows)[0] == 5
    assert first_veto([(1, "2026-09-27T19:10:00+00:00", "measured_state_still silence=31s")]) is None
    quotes = [(10, "2026-09-27T19:10:50+00:00", 150.0), (12, "2026-09-27T19:10:58+00:00", 152.0),
              (13, "2026-09-27T19:11:05+00:00", 140.0)]
    assert quote_at(quotes, "2026-09-27T19:10:59+00:00")[0] == 12   # 13 is after the veto
    fills = [("entry", 145.0, "2026-09-27T19:08:15+00:00", 1.0),
             ("partial_exit", 50.0, "2026-09-27T19:09:00+00:00", -1.0),   # before: common to both
             ("bracket_exit", 100.0, "2026-09-27T19:14:22+00:00", -2.0)]
    total, slip, n = realized_after(fills, "2026-09-27T19:10:59+00:00")
    assert (total, slip, n) == (100.0, -2.0, 1), (total, slip, n)
    lo, hi = boot_ci([1.0, 2.0, 3.0, 4.0])
    assert 1.0 <= lo <= hi <= 4.0
    print("selftest ok")



# Operator protocol: legacy hypothetical conclusions cannot publish by default.
from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports
_quarantine_exports(globals(), ('summarize', 'main'))

if __name__ == "__main__":
    main()
