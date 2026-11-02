#!/usr/bin/env python3
"""One-off honest close of STALE open PAPER memecoin positions (2026-07-23).

"Stale" = a paper position held past --min-age-days without any arm rail firing. These are not
dead coins (all open paper positions are still freshly quoted); they are positions their arm's
policy simply never closes, riding open and distorting the open-book count.

This does NOT delete anything and it does NOT prune (memecoin data is never pruned). It marks
each stale paper position CLOSED with an honest modeled exit, using the EXACT convention the
runner's own max-hold backstop uses for a position it can't price this sweep: close at the last
live mid we recorded (`last_quote_price`) with conservative unknown-liquidity slippage, tagged
`max_hold_cap_stale`; if a position never got a quote, close at entry (0 PnL, no fabrication),
tagged `max_hold_cap_noquote`. Rows stay; only is_open, the exit fill, and finalize bookkeeping
change.

REAL positions are never touched. Every close goes through `_close` -> `claim_close`, the same
atomic 1->0 flip the live runner uses, so this can run against the live DB without racing it
into a double-close: whoever flips is_open first wins, the loser no-ops.

Dry-run by default. Pass --apply to write.
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone

import sys as _sys, pathlib as _pathlib   # a bare `python3 scripts/close_stale_paper_positions.py` puts this file's
_R = str(_pathlib.Path(__file__).resolve().parents[1])  # own dir on sys.path,
if _R not in _sys.path:                    # never the repo root
    _sys.path.insert(0, _R)
from sniper.persistence import db_memecoin as m
from sniper.persistence.db_core import get_connection
from sniper.services import memecoin_exits as X
from sniper.util.clock import real_utc_now_iso


def _age_days(opened_at, now_iso):
    try:
        a = datetime.fromisoformat(str(opened_at))
        b = datetime.fromisoformat(str(now_iso))
    except (TypeError, ValueError):
        return None
    if a.tzinfo is None:
        a = a.replace(tzinfo=timezone.utc)
    if b.tzinfo is None:
        b = b.replace(tzinfo=timezone.utc)
    return (b - a).total_seconds() / 86400.0


def stale_paper_positions(conn, min_age_days, now_iso):
    """Open PAPER positions older than min_age_days (corrected-UTC age)."""
    out = []
    for pos in m.list_open_positions(conn, mode="paper"):
        if str(pos.get("execution_mode") or "paper") == "real":
            continue                                   # belt-and-braces; list already paper-only
        age = _age_days(pos.get("opened_at"), now_iso)
        if age is not None and age >= min_age_days:
            out.append((age, pos))
    return sorted(out, key=lambda t: -t[0])


def close_one(conn, pos, now_iso):
    """Honest close, mirroring memecoin_exits._timecap_no_quote's stale/noquote branches."""
    last = pos.get("last_quote_price")
    if last and last > 0:
        return X._close(conn, pos, last, None, now_iso, "max_hold_cap_stale",
                        source="dexscreener_stale")
    return X._close(conn, pos, None, None, now_iso, "max_hold_cap_noquote",
                    exit_price=pos.get("entry_price"), source="noquote")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-age-days", type=float, default=7.0)
    ap.add_argument("--apply", action="store_true", help="write; default is dry-run")
    args = ap.parse_args()

    conn = get_connection()
    conn.row_factory = sqlite3.Row
    now_iso = real_utc_now_iso()
    stale = stale_paper_positions(conn, args.min_age_days, now_iso)

    print(f"anchor (corrected-UTC now): {now_iso}")
    print(f"stale paper opens >= {args.min_age_days}d: {len(stale)}")
    by_arm: dict[str, int] = {}
    for age, pos in stale:
        by_arm[pos.get("strategy") or "?"] = by_arm.get(pos.get("strategy") or "?", 0) + 1
    print("by arm:", {k: by_arm[k] for k in sorted(by_arm)})

    if not args.apply:
        print("\nDRY RUN — nothing written. Sample (oldest first):")
        for age, pos in stale[:15]:
            print(f"  id={pos['id']:6d} {pos.get('strategy'):11s} {pos.get('network_id') or '?':7s} "
                  f"age={age:5.1f}d last_q={pos.get('last_quote_price')}")
        print("\nRe-run with --apply to close them.")
        return 0

    closed, skipped, pnl = 0, 0, 0.0
    for age, pos in stale:
        r = close_one(conn, pos, now_iso)
        if r is None:
            skipped += 1                               # claim lost to the live runner — fine
            continue
        row = conn.execute("SELECT realized_pnl, exit_reason FROM memecoin_positions "
                           "WHERE id=?", (pos["id"],)).fetchone()
        rp = float(row["realized_pnl"] or 0.0)
        pnl += rp
        closed += 1
        print(f"  closed id={pos['id']:6d} {pos.get('strategy'):11s} age={age:5.1f}d "
              f"reason={row['exit_reason']} pnl={rp:+.2f}")
    print(f"\nclosed={closed}  skipped(claim lost to runner)={skipped}  "
          f"realized_pnl_sum={pnl:+.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
