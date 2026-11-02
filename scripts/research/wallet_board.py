#!/usr/bin/env python3
"""Per-wallet funded Solana scoreboard from the ledger alone (read-only): closed funded positions
opened in [--since, --until), split by whether the wallet is armed on Solana NOW.

Alpha's order 2026-09-27: per armed wallet n fills, net realized $, win rate, median hold, lane mix,
median paid-over-source, exit-reason mix; flag n<5; no recommendation.

Columns and their writers:
  realized_pnl  db_memecoin.finalize_close / book_full_exit_and_finalize (derisk proceeds + final
                received - entry_notional), or reconcile_unpriced_full_exit for a fill priced later.
  fill_gap_pct  (our fill / their_price - 1) x 100, frozen at open (memecoin_copy_trader provenance).
  hold          closed_at - opened_recorded_at (the confirmed fill), opened_at when that is missing.

The armed/not-armed split is SELECTION: a wallet disarmed after losing in the window lands in the
not-armed group, so the armed total overstates any forward edge.

    python3 scripts/research/wallet_board.py --since 2026-09-20T16:46:00+00:00 --until 2026-09-27T16:46:00+00:00
    python3 scripts/research/wallet_board.py --selftest
"""
import argparse
import collections
import contextlib
import sqlite3
import statistics as st
from datetime import datetime, timedelta, timezone
from pathlib import Path

LEDGER = Path(__file__).resolve().parents[2] / "sniper_tracker.db"
COLS = ("wallet", "n", "open", "net", "win", "hold_p50_s", "paid_p50_pct", "lanes", "exits")


def ts(s):
    if not s:
        return None
    dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def armed_on_solana(wallets):
    """wallets: (source_user_id, handle, real_approved, paused, excluded, real_chains) rows."""
    out = {}
    for uid, handle, approved, paused, excluded, chains in wallets:
        if approved == 1 and not paused and not excluded and \
                "solana" in [x.strip().lower() for x in str(chains or "").split(",")]:
            out[uid] = handle
    return out


def in_window(rows, since, until):
    # Window bounds are compared as parsed instants: SQLite text order puts ' ' before 'T'.
    return [r for r in rows if since <= ts(r[3]) < until]


def line(label, rs):
    closed = [r for r in rs if r[9] == 0]
    pnl = [r[6] for r in closed if r[6] is not None]
    wins = sum(1 for p in pnl if p > 0)
    holds = [(ts(r[5]) - ts(r[4] or r[3])).total_seconds() for r in closed if r[5]]
    gaps = [r[8] for r in rs if r[8] is not None]
    return {"wallet": label, "n": len(closed), "open": sum(1 for r in rs if r[9] == 1),
            "net": round(sum(pnl), 2), "null_pnl": sum(1 for r in closed if r[6] is None),
            "wins": wins, "win": f"{wins}/{len(pnl)}" if pnl else "-",
            "hold_p50_s": round(st.median(holds)) if holds else None,
            "paid_p50_pct": round(st.median(gaps), 1) if gaps else None,
            "lanes": dict(collections.Counter(r[2] for r in rs).most_common()),
            "exits": dict(collections.Counter(r[7] for r in closed).most_common(4)),
            "low_n": len(closed) < 5}


def board(wallets, rows, since, until):
    armed = armed_on_solana(wallets)
    by = collections.defaultdict(list)
    for r in in_window(rows, since, until):
        by[r[0]].append(r)
    rows = sorted((line(h or uid, by.get(uid, [])) for uid, h in armed.items()),
                  key=lambda d: (-d["n"], -d["net"], str(d["wallet"])))
    other = {uid: rs for uid, rs in by.items() if uid not in armed}
    return {"armed_n": len(armed), "armed": rows,
            "armed_total": line("ARMED TOTAL", [r for uid in armed for r in by.get(uid, [])]),
            "not_armed_total": line("NOT ARMED NOW", [r for rs in other.values() for r in rs]),
            "not_armed": sorted((line(rs[0][1] or uid, rs) for uid, rs in other.items()),
                                key=lambda d: (-d["n"], d["net"]))}


def selftest():
    wallets = [("a", "A", 1, 0, 0, "4663,solana"), ("b", "B", 1, 1, 0, "solana"), ("c", "C", 1, 0, 0, "56"),
               ("d", "D", 0, 0, 0, "solana")]
    base = "2026-09-25T10:00:0"
    rows = [
        ("a", "A", "slot_lane", base + "0+00:00", base + "1+00:00", base + "9+00:00", 5.0, "real_tp", 2.0, 0),
        ("a", "A", "follow_activity", base + "0+00:00", base + "2+00:00", base + "6+00:00", -3.0, "b_stop", 10.0, 0),
        # Same dates as the bounds, outside them: only the instant comparison drops these.
        ("a", "A", "slot_lane", "2026-09-20T06:00:00+00:00", None, "2026-09-20T06:01:00+00:00", 50.0, "real_tp", 0.0, 0),
        ("a", "A", "slot_lane", "2026-09-27T05:00:00+00:00", None, "2026-09-27T05:01:00+00:00", 50.0, "real_tp", 0.0, 0),
        ("b", "B", "follow_activity", base + "0+00:00", base + "1+00:00", base + "5+00:00", -7.0, "b_stop", 4.0, 0),
    ]
    out = board(wallets, rows, datetime(2026, 9, 20, 12, tzinfo=timezone.utc),
                datetime(2026, 9, 27, tzinfo=timezone.utc))
    a = out["armed"][0]
    # A: two closes in the window; the rows before `since` and after `until` are out.
    assert out["armed_n"] == 1 and (a["wallet"], a["n"], a["net"], a["wins"]) == ("A", 2, 2.0, 1), out
    assert a["hold_p50_s"] == 6 and a["paid_p50_pct"] == 6.0 and a["low_n"], a   # holds 8 s and 4 s
    # B is paused and D not approved, so neither is armed; C is armed on another chain only.
    nat = out["not_armed_total"]
    assert (nat["n"], nat["net"]) == (1, -7.0) and out["not_armed"][0]["wallet"] == "B", out
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None)
    ap.add_argument("--until", default=None)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest()
        return
    until = ts(a.until) if a.until else datetime.now(timezone.utc)
    since = ts(a.since) if a.since else until - timedelta(days=7)
    with contextlib.closing(sqlite3.connect(f"file:{LEDGER}?mode=ro", uri=True, timeout=10)) as c:
        wallets = c.execute("SELECT source_user_id, handle, real_approved, paused, excluded, real_chains "
                            "FROM memecoin_wallets").fetchall()
        rows = c.execute(
            "SELECT source_user_id, source_handle, signal_source, opened_at, opened_recorded_at, closed_at, "
            "realized_pnl, exit_reason, fill_gap_pct, is_open FROM memecoin_positions "
            "WHERE execution_mode='real' AND network_id='solana' AND opened_at >= ?",
            (since.date().isoformat(),)).fetchall()
    out = board(wallets, rows, since, until)
    print(f"window opened_at [{since.isoformat()}, {until.isoformat()}); armed on solana now: {out['armed_n']}")
    for d in out["armed"]:
        print(("LOW_N " if d["low_n"] else "      ") + " | ".join(str(d[k]) for k in COLS)
              + (f" | null_pnl={d['null_pnl']}" if d["null_pnl"] else ""))
    for key in ("armed_total", "not_armed_total"):
        print("TOTAL " + " | ".join(str(out[key][k]) for k in COLS))
    for d in out["not_armed"]:
        print("   not armed: " + " | ".join(str(d[k]) for k in ("wallet", "n", "net", "win", "lanes")))


if __name__ == "__main__":
    main()
