#!/usr/bin/env python3
"""Per-armed-wallet CROWDING table, Solana: who has copiers between the source and us, and what it costs.

Operator order 2026-09-27 (lever 3, via Alpha). Descriptive, no recommendation: the roster is the
operator's. Offline: reads existing research outputs plus the ledger read-only. No network.

Per wallet armed on Solana now (memecoin_wallets real_approved=1, paused=0, excluded=0, 'solana' in
real_chains), window --since .. now:

  ahead      buys that landed strictly between the source's buy and OUR fill, per our funded fill
             (`ahead_n` from copier_crowd_and_fees.py fees outputs; status 'ok' only). This is the
             crowd in front of US, so it exists only where we filled.
  mkt crowd  other buys on the mint in slots S..S+2 after the source's buy, over the wallet's last
             ~20 on-chain buys, our own entries excluded (`crowd_n` in the wallet_crowd_dump_scoreboard
             jsonl). This one describes the market, not us.
  A          our price against the source's POST-trade price, (1+G)/(1+I)-1, from Beta's
             chase_source_split.py output (fills and chase refusals, status 'ok'): other buyers'
             move plus our own route cost.
  premium    memecoin_positions.fill_gap_pct on our funded Solana fills (our fill over the source).
  funded     funded Solana closes opened since --since: n and realized $.
  chase      first attempt per origin_event_id on Solana: chase-cap refusals ('above source price'
             or 'already ran ... past their entry') over chase refusals + fills.

Every metric carries its own n; n < 5 is flagged. Rank: ahead p50 (descending), then market crowd.

    python3 scripts/research/wallet_crowding_table.py --fees A.json [B.json ...] \
        --split chase_split.json --board logs/wallet_scoreboard_20260927_v3.jsonl
    python3 scripts/research/wallet_crowding_table.py --selftest
"""
import argparse
import contextlib
import json
import sqlite3
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SINCE = "2026-09-20T00:00:00"


def pct(xs, p):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def cell(xs, fmt="%.1f"):
    """'p50 (n)' with an n<5 flag, or '-' when empty."""
    xs = [x for x in xs if x is not None]
    if not xs:
        return "-"
    return (fmt % pct(xs, .5)) + " (n=%d%s)" % (len(xs), ", n<5" if len(xs) < 5 else "")


def is_armed_solana(real_approved, paused, excluded, real_chains):
    chains = {c.strip().lower() for c in str(real_chains or "").split(",") if c.strip()}
    return bool(real_approved) and not paused and not excluded and "solana" in chains


def chase_share(chase_n, fill_n):
    return chase_n / (chase_n + fill_n) if (chase_n + fill_n) else None


def load_fees(paths):
    fills = []
    for p in paths:
        d = json.load(open(p))
        fills.extend(d if isinstance(d, list) else d.get("fills", []))
    by_pos = {}
    for f in fills:
        if isinstance(f, dict) and f.get("status") == "ok" and f.get("position_id") is not None:
            by_pos[int(f["position_id"])] = f.get("ahead_n")       # a later file wins: reruns
    return by_pos


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fees", nargs="*", default=[])
    ap.add_argument("--split")
    ap.add_argument("--board")
    ap.add_argument("--since", default=SINCE)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    ahead_by_pos = load_fees(a.fees)
    split = json.load(open(a.split)) if a.split else []
    board = [json.loads(l) for l in open(a.board)] if a.board else []
    with contextlib.closing(sqlite3.connect("file:%s?mode=ro" % (REPO / "sniper_tracker.db"),
                                            uri=True, timeout=10)) as c:
        wallets = [(uid, h) for uid, h, ra, pa, ex, rc in c.execute(
            "SELECT source_user_id, handle, real_approved, paused, excluded, real_chains "
            "FROM memecoin_wallets") if is_armed_solana(ra, pa, ex, rc)]
        pos = c.execute(
            "SELECT id, source_user_id, fill_gap_pct, is_open, realized_pnl FROM memecoin_positions "
            "WHERE execution_mode='real' AND network_id='solana' AND opened_at >= ?",
            (a.since,)).fetchall()
        att = c.execute(
            "SELECT origin_event_id, attempt_id, source_user_id, terminal_status, terminal_reason, "
            "decision_started_at FROM memecoin_entry_attempts WHERE network_id='solana' "
            "AND decision_started_at >= ? ORDER BY decision_started_at", (a.since,)).fetchall()
    first = {}
    for origin, aid, uid, status, reason, _t in att:
        first.setdefault(origin or aid, (uid, status, reason or ""))
    rows = []
    for uid, handle in wallets:
        mine = [p for p in pos if p[1] == uid]
        closed = [p for p in mine if not p[3]]
        ahead = [ahead_by_pos[p[0]] for p in mine if p[0] in ahead_by_pos]
        crowd = [r.get("crowd_n") for b in board if b.get("uid") == uid
                 for r in b.get("rows", []) if r.get("crowd_complete")]
        a_pct = [s.get("a_pct") for s in split if s.get("handle") == handle and s.get("status") == "ok"]
        mine_att = [v for v in first.values() if v[0] == uid]
        chase_n = sum(1 for _u, st, rs in mine_att if st != "filled" and (
            "above source price" in rs or "past their entry" in rs))
        fill_n = sum(1 for _u, st, _r in mine_att if st == "filled")
        rows.append({
            "handle": handle, "ahead": ahead, "crowd": crowd, "a_pct": a_pct,
            "premium": [p[2] for p in mine if p[2] is not None],
            "n_closed": len(closed), "pnl": sum(float(p[4] or 0) for p in closed),
            "chase_n": chase_n, "fill_n": fill_n, "first_n": len(mine_att),
            "chase_share": chase_share(chase_n, fill_n)})
    rows.sort(key=lambda r: (-(pct(r["ahead"], .5) if r["ahead"] else -1),
                             -(pct(r["crowd"], .5) if r["crowd"] else -1)))
    print("armed Solana wallets: %d | our fills with a crowd read: %d | window %s..now" % (
        len(rows), sum(len(r["ahead"]) for r in rows), a.since))
    print("%-18s %-18s %-18s %-18s %-18s %-16s %s" % (
        "wallet", "ahead of us p50", "mkt crowd S..S+2", "A % p50", "premium % p50",
        "funded n / $", "chase share (chase/fills, first attempts)"))
    for r in rows:
        print("%-18s %-18s %-18s %-18s %-18s %-16s %s" % (
            str(r["handle"])[:18], cell(r["ahead"], "%.0f"), cell(r["crowd"], "%.0f"),
            cell(r["a_pct"]), cell(r["premium"]),
            "%d / %+.2f%s" % (r["n_closed"], r["pnl"], " n<5" if r["n_closed"] < 5 else ""),
            "%s (%d/%d, of %d)" % ("%.0f%%" % (100 * r["chase_share"]) if r["chase_share"] is not None
                                   else "-", r["chase_n"], r["fill_n"], r["first_n"])))


def selftest():
    assert is_armed_solana(1, 0, 0, "4663,solana") and not is_armed_solana(1, 1, 0, "solana")
    assert not is_armed_solana(1, 0, 0, "4663") and not is_armed_solana(0, 0, 0, "solana")
    assert not is_armed_solana(1, 0, 1, "solana")
    assert cell([]) == "-" and cell([1, 2, 3]) == "2.0 (n=3, n<5)"
    assert cell([1, 2, 3, 4, 5], "%.0f") == "3 (n=5)"
    assert chase_share(0, 0) is None and chase_share(1, 3) == 0.25 and chase_share(0, 4) == 0.0
    print("selftest ok")


if __name__ == "__main__":
    main()
