#!/usr/bin/env python3
"""Find dispatch-table keys the producer has stopped emitting.

THE FAILURE THIS CATCHES (2026-09-08, Alpha's ask). A table of exact `(status, reason)` pairs is
matched against strings some other module produces. When the producer's vocabulary moves, an entry
goes dead silently: no exception, no log, and its unit test still passes because the test constructs
the input by hand instead of taking it from the producer. `_RETRY_TRANSIENT`'s `("failed", None)`
sat dead for two weeks that way, so no Solana revert re-quoted.

THE METHOD. A key is not checked against the source. It is checked against what the system actually
recorded, PUSHED THROUGH THE SAME TRANSFORM THE PERSIST PATH APPLIES. That last part is the whole
trick: an audit that reads the raw column instead reported 7 of 10 keys dead on 2026-09-08, which
was entirely an artefact of `_trace_terminal` mapping `unsafe`->`safety_failed`, refusals->`refused`
and `str(None)`->the status string. Through the mapping the true answer was 0 of 10 never-produced,
with exactly one key showing the dead signature: fired historically, silent recently.

READ-ONLY. Opens the funded ledger through a read-only URI and never writes.

    python3 scripts/research/dead_dispatch_keys.py [--days 14] [--db sniper_tracker.db]
"""
import argparse
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))


def audit_retry_transient(conn, days):
    """`_RETRY_TRANSIENT` against `memecoin_entry_attempts`, through `_trace_terminal`."""
    from sniper.services.memecoin_real_runner import _RETRY_TRANSIENT, _trace_terminal
    rows = []
    for status, reason in sorted(_RETRY_TRANSIENT, key=lambda p: (p[0], str(p[1]))):
        persisted_status, persisted_reason = _trace_terminal({"status": status, "reason": reason})
        ever = conn.execute(
            "SELECT COUNT(*) FROM memecoin_entry_attempts "
            "WHERE terminal_status=? AND terminal_reason=?",
            (persisted_status, persisted_reason)).fetchone()[0]
        recent = conn.execute(
            "SELECT COUNT(*) FROM memecoin_entry_attempts "
            "WHERE terminal_status=? AND terminal_reason=? "
            "AND decision_started_at >= datetime('now', ?)",
            (persisted_status, persisted_reason, f"-{int(days)} days")).fetchone()[0]
        rows.append({"key": (status, reason), "persisted": (persisted_status, persisted_reason),
                     "ever": ever, "recent": recent})
    return rows


def classify(row, days):
    """NEVER: the key may never have been producible. DEAD: it was, and has gone silent."""
    if row["ever"] == 0:
        return "NEVER"
    if row["recent"] == 0:
        return "DEAD"
    return "live"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="sniper_tracker.db")
    ap.add_argument("--days", type=int, default=14,
                    help="a key with no rows inside this window is DEAD (default 14)")
    args = ap.parse_args(argv)

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=10)
    try:
        rows = audit_retry_transient(conn, args.days)
    finally:
        conn.close()

    width = max(len(f"{k['key'][0]}, {k['key'][1]}") for k in rows) + 4
    print(f"{'_RETRY_TRANSIENT key':{width}s} {'ever':>6s} {f'{args.days}d':>6s}  verdict")
    print("-" * (width + 24))
    suspect = []
    for r in rows:
        verdict = classify(r, args.days)
        key = f"{r['key'][0]}, {r['key'][1]}"
        print(f"{key:{width}s} {r['ever']:6d} {r['recent']:6d}  {verdict}")
        if verdict != "live":
            suspect.append((key, verdict, r["persisted"]))

    if not suspect:
        print("\nEvery key still fires. No dead dispatch entries.")
        return 0
    print(f"\n{len(suspect)} suspect key(s):")
    for key, verdict, persisted in suspect:
        note = ("recorded before, silent since — the producer's vocabulary probably moved"
                if verdict == "DEAD" else
                "no row has ever carried it — it may never have been producible at all")
        print(f"  [{verdict}] ({key})")
        print(f"          persisted as {persisted}")
        print(f"          {note}")
    print("\nA suspect key is a QUESTION, not a defect. A genuinely rare failure mode reads the "
          "same way as a dead one; confirm against the producer before deleting anything.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
