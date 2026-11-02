#!/usr/bin/env python3
"""Write data/office/probe/input.json for the T15 probe from ONE current desk claim. Read-only and bounded.

Takes the newest awaiting-judge hard-filter PASS ticket (newest first, at most --limit read) whose cohort runs the v3 sheet
(ps_inputs pumpswap-facts-v3), refreshes its snapshot at now exactly as the desk judge does (memecoin_desk_asof),
and builds the hard_filter_row the way the sheet parity test does. The desk DB is opened read-only; the only write is
the input file (atomic). allow_model_calls stays false unless --allow-model-calls (Alpha's go).
usage: nice -n 19 python3 scripts/office_probe_input.py [--allow-model-calls] [--out PATH]"""
import argparse
import json
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def pick(desk_conn, *, desk_dir, now, limit, refresh, store):
    """-> input doc (without allow_model_calls) from the newest usable ticket, or None. A ticket whose snapshot cannot
    be refreshed is skipped, never patched."""
    for ticket in store.list_awaiting_judge(desk_conn, limit=limit, newest_first=True):
        cohort = store.get_cohort(desk_conn, ticket["cohort_id"])
        policy = cohort["policy"]
        if policy.get("ps_inputs") != "pumpswap-facts-v3":
            continue
        try:
            candidate, meta = refresh(ticket["candidate"], desk_dir=desk_dir, now=now)
        except Exception:                                  # StaleSnapshot and friends: try the next ticket
            continue
        stage = next(s for s in ticket["stages"] if s["stage"] == "hard_filter")
        source = store.get_intake_source_receipt(desk_conn, ticket["opportunity_id"], cohort_id=ticket["cohort_id"],
                                                 hard_policy=policy["hard_filter"], candidate=ticket["candidate"],
                                                 as_of=now) or {}
        return {"decision_id": "probe-%s-%s" % (ticket["ticket_id"], uuid.uuid4().hex[:8]),
                "candidate": candidate, "desk_policy": policy, "strategy_epoch": ticket["cohort_id"],
                "hard_filter_row": {"verdict": "PASS", "ts": stage["ts"], "evidence": stage["inputs"]["evidence"],
                                    "market_cut": {"receipt_cut": meta["receipt_cut"], "cut_slot": meta["cut_slot"]},
                                    "source_metadata": source.get("source_metadata"),
                                    "emission_candidate": ticket["candidate"]},
                "bound_usd_per_fill": 0.0}
    return None


def main(argv=None):
    from sniper.persistence import db_memecoin_desk as store
    from sniper.services import memecoin_desk_asof as asof
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state-root", type=Path, default=Path(os.environ.get("SNIPER_STATE_ROOT", ROOT)))
    p.add_argument("--out", type=Path)
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--allow-model-calls", action="store_true")
    a = p.parse_args(argv)
    if not 1 <= a.limit <= 200:
        p.error("limit must be 1..200")
    out = a.out or a.state_root / "data/office/probe/input.json"
    conn = sqlite3.connect((a.state_root / "data/memecoin_desk.db").resolve().as_uri() + "?mode=ro", uri=True, timeout=1.)
    conn.row_factory = sqlite3.Row
    try:
        doc = pick(conn, desk_dir=a.state_root / "data/desk", now=time.time(), limit=a.limit, refresh=asof.refresh,
                   store=store)
    finally:
        conn.close()
    if doc is None:
        print(json.dumps({"state": "no_usable_ticket", "limit": a.limit}))
        return 2
    doc["allow_model_calls"] = bool(a.allow_model_calls)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, sort_keys=True))
    os.replace(tmp, out)
    print(json.dumps({"state": "written", "out": str(out), "decision_id": doc["decision_id"],
                      "allow_model_calls": doc["allow_model_calls"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
