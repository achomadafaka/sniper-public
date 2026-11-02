#!/usr/bin/env python3
"""Forward read and STOP line for the winner re-entry size ladder. Ledger only, read-only.

Spec: docs/superpowers/specs/2026-09-28-memecoin-winner-reentry-size-ladder-design.md (Beta; ladder
0.5x then 0.25x, at most two re-entries per coin inside the 24 h cooldown window, only after a
closed WIN). Alpha activates it with MEMECOIN_REENTRY_WINNER_SIZE_LADDER=0.5,0.25 and
MEMECOIN_REENTRY_WINNER_UNBLOCKED=1; rollback is MEMECOIN_REENTRY_WINNER_UNBLOCKED=0 plus a restart.

Population, derived from the ledger and independent of any stamp the build writes: every REAL,
non-manual position opened at or after --since whose coin (network_id, mint; EVM mint lower-cased)
had a REAL close in the 86,400 s before the position's opened_at. rank = the number of those closes.
The most recent of them decides the kind: realized_pnl > 0 = a winner re-entry; anything else
(0, negative, NULL) is a re-entry the ladder must never admit.

PRE-REGISTERED STOP LINE (Gamma, 2026-09-28, committed before activation; amend only before the
first re-entry fill, and say so). Returns are realized_pnl / entry_notional, so the ladder's
smaller clips do not shrink the rule (a dollar tail rule forbids every larger size by construction).
  DEFECT      any one of: a filled re-entry of rank > 2; a filled re-entry after a close that did
              not win; a classification risk_scale above the ladder factor for its rank (0.5 for
              rank 1, 0.25 for rank 2; the regime scale can only lower it). One row = revert now.
  MONEY CAP   the cumulative realized PnL of closed re-entries <= -$100 at any n: revert.
  STOP-EARLY  the first 10 closed re-entries have a mean return <= -10% AND >= 7 of them lost: revert.
  STOP-READ   at >= 30 closed re-entries: the 95% bootstrap CI of the mean return (resampling mints)
              entirely below 0: revert. Entirely above 0: keep. Otherwise continue and re-read at 60.
  Per chain the same STOP-EARLY rule is printed as an ALERT (the spec has no per-chain knob; 4663's
  pre-ladder history was -4.6% on n=26).

    python3 scripts/research/reentry_ladder_forward.py --since 2026-09-28T21:00:00+00:00
    python3 scripts/research/reentry_ladder_forward.py --selftest
"""
import argparse
import contextlib
import json
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
LEDGER = str(REPO / "sniper_tracker.db")
WINDOW_S = 86400.0
LADDER = (0.5, 0.25)
MONEY_CAP_USD = -100.0
EARLY_N, EARLY_MEAN, EARLY_LOSERS = 10, -0.10, 7
READ_N = 30


def utc(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def coin(network_id, mint):
    net = str(network_id or "solana")
    return net, str(mint) if net == "solana" else str(mint).lower()


def reentries(positions):
    """positions: dicts with id, network_id, mint, opened_at, closed_at, realized_pnl, entry_notional,
    signal_source, is_open, risk_scale. Returns the re-entry rows (rank >= 1), in opened order."""
    closes = {}
    for p in positions:
        if p["closed_at"] and not p["is_open"]:
            closes.setdefault(coin(p["network_id"], p["mint"]), []).append((utc(p["closed_at"]), p["realized_pnl"]))
    out = []
    for p in sorted(positions, key=lambda x: x["opened_at"]):
        if p.get("signal_source") == "manual" or not p.get("forward"):
            continue
        t = utc(p["opened_at"])
        prior = sorted(c for c in closes.get(coin(p["network_id"], p["mint"]), [])
                       if t - timedelta(seconds=WINDOW_S) <= c[0] < t)
        if not prior:
            continue
        last = prior[-1][1]
        out.append({**p, "rank": len(prior), "after_win": last is not None and last > 0})
    return out


def defects(rows):
    bad = []
    for r in rows:
        if r["rank"] > len(LADDER):
            bad.append((r["id"], "rank %d > %d" % (r["rank"], len(LADDER))))
        elif not r["after_win"]:
            bad.append((r["id"], "re-entry after a close that did not win"))
        elif r.get("risk_scale") is not None and r["risk_scale"] > LADDER[r["rank"] - 1] + 1e-6:
            bad.append((r["id"], "risk_scale %.3f > ladder %.2f at rank %d" % (r["risk_scale"], LADDER[r["rank"] - 1], r["rank"])))
    return bad


def verdict(closed, bad=()):
    """closed: [(pnl_usd, ret_frac, mint)] of closed re-entries in fill order."""
    if bad:
        return "DEFECT: revert (%s)" % "; ".join("%s %s" % b for b in bad[:3])
    if closed and sum(c[0] for c in closed) <= MONEY_CAP_USD:
        return "MONEY CAP: cumulative %.2f <= %.0f, revert" % (sum(c[0] for c in closed), MONEY_CAP_USD)
    first = closed[:EARLY_N]
    if len(first) == EARLY_N and sum(c[1] for c in first) / EARLY_N <= EARLY_MEAN \
            and sum(c[1] < 0 for c in first) >= EARLY_LOSERS:
        return "STOP-EARLY: first %d mean %.1f%%, %d lost, revert" % (
            EARLY_N, 100 * sum(c[1] for c in first) / EARLY_N, sum(c[1] < 0 for c in first))
    if len(closed) >= READ_N:
        lo, hi = mint_ci(closed)
        if hi < 0:
            return "STOP-READ: mean return CI [%.1f%%, %.1f%%] below 0, revert" % (100 * lo, 100 * hi)
        if lo > 0:
            return "KEEP: mean return CI [%+.1f%%, %+.1f%%] above 0" % (100 * lo, 100 * hi)
        return "CONTINUE: CI [%+.1f%%, %+.1f%%] spans 0; re-read at 60" % (100 * lo, 100 * hi)
    return "CONTINUE (closed %d; early check at %d, registered read at %d)" % (len(closed), EARLY_N, READ_N)


def mint_ci(closed, n=4000, seed=20260928):
    by = {}
    for c in closed:
        by.setdefault(c[2], []).append(c[1])
    keys, rnd, means = list(by), random.Random(seed), []
    for _ in range(n):
        xs = [x for k in (keys[rnd.randrange(len(keys))] for _ in keys) for x in by[k]]
        means.append(sum(xs) / len(xs))
    means.sort()
    return means[int(0.025 * n)], means[int(0.975 * n) - 1]


def load(since):
    from sniper.persistence.db_core import get_readonly_connection
    horizon = (utc(since) - timedelta(seconds=WINDOW_S)).isoformat()
    with contextlib.closing(get_readonly_connection(LEDGER)) as c:
        rows = [dict(r) for r in c.execute(
            "SELECT p.id, p.network_id, p.mint, p.opened_at, p.closed_at, p.realized_pnl, p.entry_notional, "
            "p.signal_source, p.is_open, a.classification_json FROM memecoin_positions p "
            "LEFT JOIN memecoin_entry_attempts a ON a.position_id=p.id "
            "WHERE p.execution_mode='real' AND (p.opened_at>=? OR p.closed_at>=?)", (horizon, horizon))]
    for r in rows:
        cj = json.loads(r.pop("classification_json") or "null") or {}
        r["risk_scale"] = cj.get("risk_scale")
        r["forward"] = r["opened_at"] >= since
    return rows


def report(since):
    rows = reentries(load(since))
    bad = defects(rows)
    closed = [(r["realized_pnl"], r["realized_pnl"] / r["entry_notional"], r["mint"]) for r in rows
              if not r["is_open"] and r["realized_pnl"] is not None and r["entry_notional"]]
    print("winner re-entry ladder forward read since %s" % since)
    print("  re-entries filled %d (open %d, closed priced %d); by rank %s; after a non-win %d" % (
        len(rows), sum(1 for r in rows if r["is_open"]), len(closed),
        {k: sum(1 for r in rows if r["rank"] == k) for k in sorted({r["rank"] for r in rows})},
        sum(1 for r in rows if not r["after_win"])))
    if closed:
        print("  closed: sum %+.2f  mean return %+.1f%%  losers %d of %d" % (
            sum(c[0] for c in closed), 100 * sum(c[1] for c in closed) / len(closed),
            sum(c[1] < 0 for c in closed), len(closed)))
    for net in sorted({str(r["network_id"]) for r in rows}):
        cc = [(r["realized_pnl"], r["realized_pnl"] / r["entry_notional"], r["mint"]) for r in rows
              if str(r["network_id"]) == net and not r["is_open"] and r["realized_pnl"] is not None and r["entry_notional"]]
        v = verdict(cc)
        print("  chain %-7s closed %d sum %+.2f  %s" % (net, len(cc), sum(c[0] for c in cc),
                                                        ("ALERT " + v) if v.startswith("STOP-EARLY") else v))
    unscaled = sum(1 for r in rows if r.get("risk_scale") is None)
    if unscaled:
        print("  risk_scale missing on %d re-entries (size check not possible on those)" % unscaled)
    print("  VERDICT: %s" % verdict(closed, bad))


def selftest():
    def pos(i, net, mint, op, cl=None, pnl=None, src="onchain_watch", fwd=True, rs=None):
        return {"id": i, "network_id": net, "mint": mint, "opened_at": op, "closed_at": cl, "realized_pnl": pnl,
                "entry_notional": 100.0, "signal_source": src, "is_open": cl is None, "risk_scale": rs, "forward": fwd}
    ps = [pos(1, "solana", "M", "2026-09-28T10:00:00+00:00", "2026-09-28T10:05:00+00:00", 20.0, fwd=False),
          pos(2, "solana", "M", "2026-09-28T11:00:00+00:00", "2026-09-28T11:05:00+00:00", 5.0, rs=0.5),
          pos(3, "solana", "M", "2026-09-28T12:00:00+00:00", rs=0.25),
          pos(4, "4663", "0xAB", "2026-09-28T10:00:00+00:00", "2026-09-28T10:01:00+00:00", -3.0, fwd=False),
          pos(5, "4663", "0xab", "2026-09-28T10:30:00+00:00", rs=0.5),                          # after a loss
          pos(6, "solana", "M", "2026-09-28T12:30:00+00:00", src="manual"),                     # manual: out
          pos(7, "solana", "Z", "2026-09-29T11:00:00+00:00", rs=1.0)]                           # first entry
    rows = reentries(ps)
    assert [(r["id"], r["rank"], r["after_win"]) for r in rows] == [(5, 1, False), (2, 1, True), (3, 2, True)], rows
    assert [b[0] for b in defects(rows)] == [5]                     # EVM mint case-folded; loss re-entry caught
    unpriced = reentries([pos(8, "solana", "N", "2026-09-28T10:00:00+00:00", "2026-09-28T10:05:00+00:00", None, fwd=False),
                          pos(9, "solana", "N", "2026-09-28T10:30:00+00:00", rs=0.5)])
    assert [r["after_win"] for r in unpriced] == [False]           # an unpriced close is not a win
    assert defects([{**rows[1], "risk_scale": 0.6}])[0][1].startswith("risk_scale")
    assert defects([{**rows[2], "rank": 3}])[0][1].startswith("rank 3")
    assert verdict([], [(5, "x")]).startswith("DEFECT")
    assert verdict([(-60.0, -0.4, "a"), (-45.0, -0.4, "b")]).startswith("MONEY CAP")
    early = [(-5.0, -0.2, "m%d" % i) for i in range(7)] + [(1.0, 0.1, "w%d" % i) for i in range(3)]
    assert verdict(early).startswith("STOP-EARLY")
    assert verdict(early[:9]).startswith("CONTINUE")
    assert verdict([(-0.5, -0.05, "m%d" % i) for i in range(30)]).startswith("STOP-READ")
    assert verdict([(2.0, 0.1, "m%d" % i) for i in range(30)]).startswith("KEEP")
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--since", help="the activation instant (the runner start that loaded the ladder)")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.since:
        raise SystemExit("--since is required: the runner start that loaded the ladder")
    report(a.since)


if __name__ == "__main__":
    main()
