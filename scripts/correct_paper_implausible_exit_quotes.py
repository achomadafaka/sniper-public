#!/usr/bin/env python3
"""Correct six STANDARD paper closes that booked an implausible exit quote. Dry run by default.

2026-09-15, chain 4663, STANDARD 0x88ad8ddf: six $100 paper `real_tp` closes booked whole-bag route
quotes of 2.5137 and 3.4442-3.4467 USD per token against DexScreener mids of 0.3549 and 0.3142 (7.08x and
10.96x), $6,925.60 of paper profit. Tracked trades within two minutes stayed near the mid. The forward
fix (memecoin_exits._quote_plausible) books such a close as `<reason>_noquote` at the entry price, and
this script brings the six historical rows to that same state:

  exit_reason  real_tp -> real_tp_noquote   (censored by every reader that honours the suffix)
  realized_pnl booked  -> 0.0               (readers that sum it without the censor: closed_position_totals,
                                             wallet_stats)

The original values go into the append-only `memecoin_paper_pnl_corrections` table FIRST, in the same
transaction. Each row must still match its booked values; one mismatch aborts the whole run. A row with an
audit record is skipped, so a second run changes nothing. UBER (53308/53309) and GGEZ (58104/58105) are
unverifiable, not proven, and are deliberately not here.

    python3 scripts/correct_paper_implausible_exit_quotes.py            # dry run, read-only
    python3 scripts/correct_paper_implausible_exit_quotes.py --apply    # operator-approved write
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

MINT = "0x88ad8ddf1e3898412146a534538d418c6f8a9062"
REASON = "implausible_exit_quote"
CORRECTED = {"exit_reason": "real_tp_noquote", "realized_pnl": 0.0}
EVIDENCE = ("exit quote 7.08x/10.96x the live DexScreener mid (0.3549/0.3142); tracked trades within "
            "120 s near the mid; next sweep 01:57:26 quoted 0.98x; source unprovable (no historical "
            "state on the 4663 node). Report: Gamma 2026-09-17.")
TARGETS = (
    {"position_id": 57844, "booked_realized_pnl": 803.9502818259565, "exit_fill_id": 116766, "exit_fill_price": 2.513677097817185},
    {"position_id": 57845, "booked_realized_pnl": 803.9502818259565, "exit_fill_id": 116767, "exit_fill_price": 2.513677097817185},
    {"position_id": 57846, "booked_realized_pnl": 1329.6132536830667, "exit_fill_id": 116760, "exit_fill_price": 3.444469923602038},
    {"position_id": 57847, "booked_realized_pnl": 1329.4826829523452, "exit_fill_id": 116762, "exit_fill_price": 3.4441553301595706},
    {"position_id": 57850, "booked_realized_pnl": 1329.2746267324605, "exit_fill_id": 116761, "exit_fill_price": 3.4465405646377114},
    {"position_id": 57851, "booked_realized_pnl": 1329.342743922991, "exit_fill_id": 116763, "exit_fill_price": 3.4467048218461733},
)
TABLE_SQL = """CREATE TABLE IF NOT EXISTS memecoin_paper_pnl_corrections (
    position_id INTEGER PRIMARY KEY,
    corrected_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    booked_exit_reason TEXT NOT NULL,
    booked_realized_pnl REAL NOT NULL,
    booked_exit_fill_id INTEGER NOT NULL,
    booked_exit_fill_price REAL NOT NULL,
    corrected_exit_reason TEXT NOT NULL,
    corrected_realized_pnl REAL NOT NULL,
    evidence TEXT NOT NULL)"""


class GuardMismatch(RuntimeError):
    """A target row no longer holds the values it was proven with."""


def _corrected_ids(conn):
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                          "AND name='memecoin_paper_pnl_corrections'").fetchone()
    if not exists:
        return set()
    return {r[0] for r in conn.execute("SELECT position_id FROM memecoin_paper_pnl_corrections")}


def _guard(conn, target):
    """None when the row still matches its booked values, else the reason it does not."""
    row = conn.execute("SELECT execution_mode, network_id, mint, is_open, exit_reason, realized_pnl "
                       "FROM memecoin_positions WHERE id=?", (target["position_id"],)).fetchone()
    if row is None:
        return "position missing"
    mode, chain, mint, is_open, reason, pnl = tuple(row)
    if (mode, str(chain), str(mint).lower(), int(is_open)) != ("paper", "4663", MINT, 0):
        return "not a closed 4663 STANDARD paper row"
    if reason != "real_tp":
        return "exit_reason is %r" % reason
    if pnl is None or not math.isclose(float(pnl), target["booked_realized_pnl"], abs_tol=0.005):
        return "realized_pnl is %r" % pnl
    fills = conn.execute("SELECT id, price FROM memecoin_fills WHERE position_id=? AND kind='bracket_exit'",
                         (target["position_id"],)).fetchall()
    if [(f[0], round(float(f[1]), 12)) for f in fills] != [
            (target["exit_fill_id"], round(target["exit_fill_price"], 12))]:
        return "exit fill differs"
    return None


def plan(conn, targets=TARGETS):
    """One entry per target: status will_correct / already_corrected / guard_mismatch, before and after."""
    done = _corrected_ids(conn)
    out = []
    for target in targets:
        pid = target["position_id"]
        row = conn.execute("SELECT strategy, source_handle, exit_reason, realized_pnl FROM memecoin_positions "
                           "WHERE id=?", (pid,)).fetchone()
        before = {"exit_reason": row[2], "realized_pnl": row[3]} if row else None
        if pid in done:
            status, why = "already_corrected", None
        else:
            why = _guard(conn, target)
            status = "guard_mismatch" if why else "will_correct"
        out.append({"position_id": pid, "strategy": row[0] if row else None,
                    "source_handle": row[1] if row else None, "status": status, "detail": why,
                    "before": before, "after": dict(CORRECTED),
                    "exit_fill_price": target["exit_fill_price"]})
    return out


def apply(conn, *, now_iso, targets=TARGETS):
    """Audit, then correct, in one transaction. Returns the number of rows corrected."""
    steps = plan(conn, targets)
    bad = [s for s in steps if s["status"] == "guard_mismatch"]
    if bad:
        raise GuardMismatch("; ".join("%s: %s" % (s["position_id"], s["detail"]) for s in bad))
    todo = [t for t, s in zip(targets, steps) if s["status"] == "will_correct"]
    if not todo:
        return 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(TABLE_SQL)
        for target in todo:
            why = _guard(conn, target)                      # re-checked inside the write lock
            if why:
                raise GuardMismatch("%s: %s" % (target["position_id"], why))
            conn.execute(
                "INSERT INTO memecoin_paper_pnl_corrections VALUES (?,?,?,?,?,?,?,?,?,?)",
                (target["position_id"], now_iso, REASON, "real_tp", target["booked_realized_pnl"],
                 target["exit_fill_id"], target["exit_fill_price"], CORRECTED["exit_reason"],
                 CORRECTED["realized_pnl"], EVIDENCE))
            cur = conn.execute(
                "UPDATE memecoin_positions SET exit_reason=?, realized_pnl=? "
                "WHERE id=? AND execution_mode='paper' AND exit_reason='real_tp'",
                (CORRECTED["exit_reason"], CORRECTED["realized_pnl"], target["position_id"]))
            if cur.rowcount != 1:
                raise GuardMismatch("%s: update matched %d rows" % (target["position_id"], cur.rowcount))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return len(todo)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the correction (operator approval)")
    args = parser.parse_args(argv)
    from sniper.persistence.db_core import get_connection, get_readonly_connection
    from sniper.util.clock import real_utc_now_iso
    conn = get_connection() if args.apply else get_readonly_connection()
    try:
        steps = plan(conn)
        print(json.dumps(steps, indent=1))
        booked = sum(float(s["before"]["realized_pnl"] or 0.0) for s in steps
                     if s["before"] and s["status"] == "will_correct")
        print("rows to correct: %d, booked realized_pnl removed: %.2f"
              % (sum(s["status"] == "will_correct" for s in steps), booked))
        if not args.apply:
            print("DRY RUN: nothing written. Re-run with --apply to write.")
            return 0
        print("corrected %d row(s)" % apply(conn, now_iso=real_utc_now_iso()))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
