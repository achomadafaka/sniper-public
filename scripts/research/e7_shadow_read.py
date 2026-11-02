#!/usr/bin/env python3
"""E7 activation read: our own tape's sell-share split against DexScreener's, per chain.

Alpha's D-E7-3 (2026-09-29, spec docs/superpowers/specs/2026-09-29-memecoin-dexscreener-out-e6-e7-design.md):
switch E7 off on a chain only after 20 shadow attempts there with DexScreener at >= 8 m5 txns, and
only when no disagreement would have refused a coin DexScreener admitted that later made money.

Read-only: the ledger and data/memecoin_refusal_captures.db. The shadow is the `sell_share` substage
that E7 (d9c085f87) records on every attempt: while E7 is on for the chain (`basis` m5_txn_counts),
`share_pct` / `txns` are DexScreener's and `own_share_pct` / `own_txns` / `own_pool` are ours. One
attempt per origin. A flip is a different verdict against the gate's own cap and floor:
  new_refusal    ours refuses, DexScreener admitted  (the dangerous direction: graded)
  new_admission  ours admits, DexScreener refused    (counted)
A new refusal is graded from the funded position the attempt opened (realized PnL > 0 = made money),
else from the refusal captures of the same origin (TP +25% / stop -35% / the +300 s mark, as
scripts/research/cap_refusal_outcomes.py), else it is unknown and listed.

READY needs n >= 20 and no new refusal that made money. Unknown outcomes do not block READY by the
rule as ordered; they are counted on the READY line so a reader sees them.

    python3 scripts/research/e7_shadow_read.py [--since 2026-09-29T00:00:00] [--list 10]
    python3 scripts/research/e7_shadow_read.py --selftest
"""
import argparse
import collections
import contextlib
import json
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "scripts" / "research")]

LEDGER = REPO / "sniper_tracker.db"
CAPTURES = REPO / "data" / "memecoin_refusal_captures.db"
CHAINS = ("4663", "8453")
MIN_DEX_TXNS = 8
READY_N = 20


def sell_share_substage(trace_json):
    """The attempt's `sell_share` substage dict, or None."""
    try:
        events = (json.loads(trace_json or "{}") or {}).get("substage_events") or []
    except (TypeError, ValueError):
        return None
    return next((s for s in events if isinstance(s, dict) and s.get("name") == "sell_share"), None)


def pairs(rows):
    """Plain attempt rows (dicts: network_id, origin_event_id, mint, attempt_id, position_id,
    trace_json) -> one comparable pair per origin on the tape chains, oldest first."""
    out, seen = [], set()
    for r in rows:
        chain = str(r.get("network_id") or "")
        key = (chain, r.get("origin_event_id") or r.get("attempt_id"))
        if chain not in CHAINS or key in seen:
            continue
        ss = sell_share_substage(r.get("trace_json"))
        if not ss or ss.get("basis") != "m5_txn_counts" or ss.get("own_share_pct") is None:
            continue
        if ss.get("share_pct") is None or int(ss.get("txns") or 0) < MIN_DEX_TXNS:
            continue
        seen.add(key)
        out.append({"chain": chain, "origin": r.get("origin_event_id"), "mint": r.get("mint"),
                    "attempt_id": r.get("attempt_id"), "position_id": r.get("position_id"),
                    "dex_share": float(ss["share_pct"]), "dex_txns": int(ss["txns"]),
                    "own_share": float(ss["own_share_pct"]), "own_txns": int(ss.get("own_txns") or 0),
                    "cap": float(ss.get("cap_pct") or 0), "min_txns": int(ss.get("min_txns") or MIN_DEX_TXNS),
                    "pool": ss.get("own_pool")})
    return out


def refuses(share, txns, cap, min_txns):
    """The gate's own verdict: refuse only a confident one-sided count over the cap."""
    return bool(cap) and share is not None and txns >= min_txns and share > cap


def flip(p):
    dex = refuses(p["dex_share"], p["dex_txns"], p["cap"], p["min_txns"])
    own = refuses(p["own_share"], p["own_txns"], p["cap"], p["min_txns"])
    return "new_refusal" if own and not dex else "new_admission" if dex and not own else None


def summarize(ps, outcomes, *, ready_n=READY_N):
    """{chain: summary}. `outcomes`: {attempt_id: 'profit' | 'loss' | 'unknown'} for new refusals."""
    out = {}
    for chain in CHAINS:
        cp = [p for p in ps if p["chain"] == chain]
        diffs = sorted(abs(p["own_share"] - p["dex_share"]) for p in cp)
        flips = collections.Counter(flip(p) for p in cp)
        graded = collections.Counter(outcomes.get(p["attempt_id"], "unknown")
                                     for p in cp if flip(p) == "new_refusal")
        out[chain] = {
            "n": len(cp),
            "diff_p50": statistics.median(diffs) if diffs else None,
            "diff_p90": diffs[int(0.9 * (len(diffs) - 1))] if diffs else None,
            "new_refusal": flips["new_refusal"], "new_admission": flips["new_admission"],
            "new_refusal_profit": graded["profit"], "new_refusal_loss": graded["loss"],
            "new_refusal_unknown": graded["unknown"],
            "ready": len(cp) >= ready_n and graded["profit"] == 0,
        }
    return out


def outcome(ledger, captures, p):
    """'profit' | 'loss' | 'unknown' for one new-refusal attempt."""
    from cap_refusal_outcomes import grade
    from first_seen_refusal_outcomes import path_returns
    if p.get("position_id"):
        row = ledger.execute("SELECT realized_pnl, is_open FROM memecoin_positions WHERE id=?",
                             (p["position_id"],)).fetchone()
        if row and not row["is_open"] and row["realized_pnl"] is not None:
            return "profit" if row["realized_pnl"] > 0 else "loss"
    if captures is not None and p.get("origin"):
        obs = [(float(a), float(px)) for a, px in captures.execute(
            "SELECT age_s, quote_price FROM memecoin_refusal_captures WHERE origin_event_id=? "
            "AND capture_id NOT LIKE '%#exact' AND quality='ok' AND quote_price IS NOT NULL "
            "AND age_s IS NOT NULL ORDER BY age_s", (p["origin"],))]
        rets = path_returns(obs, None) if obs else None
        ret, how = grade(rets) if rets else (None, "censored")
        if ret is not None:
            return "profit" if ret > 0 else "loss"
    return "unknown"


def report(args):
    from cap_refusal_outcomes import ro
    since = args.since or (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    with contextlib.closing(ro(LEDGER)) as ledger:
        rows = [dict(r) for r in ledger.execute(
            "SELECT network_id, origin_event_id, mint, attempt_id, position_id, trace_json "
            "FROM memecoin_entry_attempts WHERE decision_started_at>=? AND network_id IN (?,?) "
            "AND trace_json LIKE '%\"own_share_pct\"%' ORDER BY decision_started_at",
            (since, *CHAINS))]
        ps = pairs(rows)
        captures = ro(CAPTURES) if CAPTURES.exists() else None
        try:
            outcomes = {p["attempt_id"]: outcome(ledger, captures, p)
                        for p in ps if flip(p) == "new_refusal"}
        finally:
            if captures is not None:
                captures.close()
    print("E7 shadow since %s: attempts with our split AND DexScreener >= %d m5 txns, one per origin"
          % (since, MIN_DEX_TXNS))
    for chain, s in summarize(ps, outcomes).items():
        fmt = lambda v: "-" if v is None else "%.1f" % v          # noqa: E731
        print("%-5s n %3d  |own - DexScreener| share pp p50 %s p90 %s  flips: new refusals %d "
              "(made money %d, lost %d, unknown %d), new admissions %d  %s" % (
                  chain, s["n"], fmt(s["diff_p50"]), fmt(s["diff_p90"]), s["new_refusal"],
                  s["new_refusal_profit"], s["new_refusal_loss"], s["new_refusal_unknown"],
                  s["new_admission"],
                  "READY (unknown outcomes: %d)" % s["new_refusal_unknown"] if s["ready"]
                  else "not ready (need n >= %d and no new refusal that made money)" % READY_N))
    for p in [p for p in ps if flip(p)][:args.list]:
        print("  %-13s %s %s dex %.0f%% of %d, ours %.0f%% of %d on %s -> %s" % (
            flip(p), p["chain"], str(p["mint"])[:12], p["dex_share"], p["dex_txns"], p["own_share"],
            p["own_txns"], str(p["pool"])[:14], outcomes.get(p["attempt_id"], "-")))
    return 0


def selftest():
    def row(chain, origin, dex, dex_n, own, own_n, basis="m5_txn_counts", aid=None):
        ss = {"name": "sell_share", "share_pct": dex, "txns": dex_n, "own_share_pct": own,
              "own_txns": own_n, "own_pool": "0xp", "cap_pct": 80.0, "min_txns": 8, "basis": basis}
        return {"network_id": chain, "origin_event_id": origin, "mint": "0xm" + origin,
                "attempt_id": aid or "a" + origin, "position_id": None,
                "trace_json": json.dumps({"substage_events": [ss]})}
    rows = [row("4663", "1", 50.0, 10, 90.0, 10),            # new refusal
            row("4663", "1", 50.0, 10, 90.0, 10, aid="dup"),  # same origin: counted once
            row("4663", "2", 90.0, 10, 50.0, 10),            # new admission
            row("4663", "3", 50.0, 10, 55.0, 10),            # agree
            row("4663", "4", 50.0, 5, 90.0, 10),             # DexScreener under 8: not comparable
            row("4663", "5", 50.0, 10, 90.0, 10, basis="onchain_pool_counts"),  # E7 already off
            row("8453", "6", 40.0, 9, None, 0),              # no split of ours
            row("56", "7", 50.0, 10, 90.0, 10)]              # not a tape chain
    ps = pairs(rows)
    assert [p["origin"] for p in ps] == ["1", "2", "3"], ps
    assert [flip(p) for p in ps] == ["new_refusal", "new_admission", None]
    s = summarize(ps, {"a1": "loss"}, ready_n=3)["4663"]
    assert (s["n"], s["new_refusal"], s["new_admission"], s["new_refusal_loss"]) == (3, 1, 1, 1)
    assert s["diff_p50"] == 40.0 and s["ready"] is True
    assert summarize(ps, {"a1": "profit"}, ready_n=3)["4663"]["ready"] is False, "a winner blocks"
    assert summarize(ps, {}, ready_n=3)["4663"]["new_refusal_unknown"] == 1
    assert summarize(ps, {}, ready_n=4)["4663"]["ready"] is False, "n below the bar"
    assert summarize(ps, {})["8453"]["n"] == 0
    assert refuses(80.0, 10, 80.0, 8) is False and refuses(81.0, 7, 80.0, 8) is False
    print("selftest ok")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--since")
    ap.add_argument("--list", type=int, default=10)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    return selftest() if args.selftest else report(args)


if __name__ == "__main__":
    raise SystemExit(main())
