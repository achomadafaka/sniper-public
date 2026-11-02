#!/usr/bin/env python3
"""Forward read and STOP line for the Solana round-trip cap raised from 10% to 15%. Ledger only.

Alpha, 2026-09-28 21:07Z (operator order "fix, turn off or raise"): MEMECOIN_ENTRY_ROUNDTRIP_CAP_TP_FRAC=0.6
x TP 25 = a 15% cap, written 21:07:04Z, live from the runner restart that followed. Evidence for the
raise (scripts/research/cap_refusal_outcomes.py, 5af3fc27 / 967beb66): refused 10-15% band +4.6%
[-3.8, +13.8] n=76 on exact-route captures, direction only.

FLIPPED = a filled Solana attempt whose own roundtrip_cap substage recorded cap_pct > 10 (the raised
cap was in force) and cost_pct in (10, 15]: a fill the old cap would have refused. The population
anchors on the substage's own cap_pct, not on a typed time, so a restart delay cannot leak old rows.

PRE-REGISTERED STOP LINE (Gamma, committed before the first flipped fill):
  STOP-EARLY  the first 10 closed flipped fills sum <= -$120 AND >= 7 of them lost: revert to 10%.
  STOP-READ   at >= 30 closed flipped fills, the 95% bootstrap CI of the mean realized return
              (realized_pnl / entry_notional, resampling mints) entirely below 0: revert.
              Entirely above 0: keep. Otherwise continue and re-read at 60.
Revert = unset MEMECOIN_ENTRY_ROUNDTRIP_CAP_TP_FRAC (or set the old value) plus a runner restart.

    python3 scripts/research/roundtrip_cap_forward.py
    python3 scripts/research/roundtrip_cap_forward.py --selftest
"""
import argparse
import contextlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "scripts" / "research")]
from cap_refusal_outcomes import mint_ci, ro  # noqa: E402

LEDGER = REPO / "sniper_tracker.db"
OLD_CAP, NEW_CAP = 10.0, 15.0
EARLY_N, EARLY_SUM, EARLY_LOSERS, READ_N = 10, -120.0, 7, 30


def flipped(cap_pct, cost_pct):
    return cap_pct is not None and cost_pct is not None and cap_pct > OLD_CAP and OLD_CAP < cost_pct <= NEW_CAP


def verdict(closed):
    """closed: [(pnl_usd, ret_frac, mint)] of closed flipped fills in fill order."""
    first = closed[:EARLY_N]
    if len(first) == EARLY_N and sum(c[0] for c in first) <= EARLY_SUM and sum(c[0] < 0 for c in first) >= EARLY_LOSERS:
        return "STOP-EARLY: first %d sum %+.2f, %d lost: revert to the 10%% cap" % (
            EARLY_N, sum(c[0] for c in first), sum(c[0] < 0 for c in first))
    if len(closed) >= READ_N:
        lo, hi = mint_ci([(m, r) for _p, r, m in closed])
        if hi < 0:
            return "STOP-READ: mean return CI [%.1f%%, %.1f%%] below 0: revert" % (100 * lo, 100 * hi)
        if lo > 0:
            return "KEEP: mean return CI [%+.1f%%, %+.1f%%] above 0" % (100 * lo, 100 * hi)
        return "CONTINUE: CI [%+.1f%%, %+.1f%%] spans 0; re-read at 60" % (100 * lo, 100 * hi)
    return "CONTINUE (closed flipped %d; early check at %d, registered read at %d)" % (len(closed), EARLY_N, READ_N)


def load(since):
    rows = []
    with contextlib.closing(ro(LEDGER)) as c:
        for at, tj, pid, open_, pnl, notional, mint in c.execute(
                "SELECT a.decision_started_at, a.trace_json, p.id, p.is_open, p.realized_pnl, p.entry_notional, p.mint "
                "FROM memecoin_entry_attempts a JOIN memecoin_positions p ON p.id=a.position_id "
                "WHERE a.network_id='solana' AND a.decision_started_at>=? AND p.execution_mode='real' "
                "AND a.trace_json LIKE '%\"roundtrip_cap\"%' ORDER BY a.decision_started_at", (since,)):
            ev = next((s for s in json.loads(tj).get("substage_events") or [] if s.get("name") == "roundtrip_cap"), {})
            rows.append({"at": at, "pid": pid, "open": open_, "pnl": pnl, "notional": notional, "mint": mint,
                         "cap": ev.get("cap_pct"), "cost": ev.get("cost_pct")})
    return rows


def report(since):
    rows = load(since)
    raised = [r for r in rows if r["cap"] is not None and r["cap"] > OLD_CAP]
    flips = [r for r in raised if flipped(r["cap"], r["cost"])]
    closed = [(r["pnl"], r["pnl"] / r["notional"], r["mint"]) for r in flips
              if not r["open"] and r["pnl"] is not None and r["notional"]]
    print("round-trip cap forward read since %s" % since)
    print("  Solana fills with a roundtrip_cap substage %d; under the raised cap %d (cap_pct seen %s)" % (
        len(rows), len(raised), sorted({r["cap"] for r in raised})))
    print("  FLIPPED (cost in (10, 15]) %d: open %d, closed priced %d" % (
        len(flips), sum(1 for r in flips if r["open"]), len(closed)))
    if closed:
        print("  closed flipped: sum %+.2f  mean %+.1f%%  losers %d of %d" % (
            sum(c[0] for c in closed), 100 * sum(c[1] for c in closed) / len(closed),
            sum(c[0] < 0 for c in closed), len(closed)))
    print("  VERDICT: %s" % verdict(closed))


def selftest():
    assert flipped(15.0, 12.0) and flipped(15.0, 15.0) and not flipped(15.0, 10.0)
    assert not flipped(10.0, 12.0) and not flipped(15.0, 15.1) and not flipped(None, 12.0)
    lose = [(-20.0, -0.2, "m%d" % i) for i in range(7)] + [(5.0, 0.05, "w%d" % i) for i in range(3)]
    assert verdict(lose).startswith("STOP-EARLY")                 # -140 + 15 = -125, 7 losers
    assert verdict(lose[:9]).startswith("CONTINUE")
    mild = [(-10.0, -0.1, "m%d" % i) for i in range(7)] + [(5.0, 0.05, "w%d" % i) for i in range(3)]
    assert verdict(mild).startswith("CONTINUE")                   # -55 is above -120
    six = [(-25.0, -0.25, "m%d" % i) for i in range(6)] + [(5.0, 0.05, "w%d" % i) for i in range(4)]
    assert verdict(six).startswith("CONTINUE")                    # -130 but only 6 losers
    assert verdict([(-1.0, -0.05, "m%d" % i) for i in range(30)]).startswith("STOP-READ")
    assert verdict([(1.0, 0.05, "m%d" % i) for i in range(30)]).startswith("KEEP")
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--since", default="2026-09-28T21:07:04")
    a = ap.parse_args()
    return selftest() if a.selftest else report(a.since)


if __name__ == "__main__":
    main()
