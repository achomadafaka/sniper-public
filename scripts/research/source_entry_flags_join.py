#!/usr/bin/env python3
"""Add selected_wallet(v2) and pair_first to the Tier W forward rows. Reads NO outcome field (no swaps, labels or prices): only event_id -> memecoin_events identity columns.
    nice -n 19 ionice -c3 python3 scripts/research/source_entry_flags_join.py IN.jsonl OUT.jsonl SELECTED.json
pair_first = this row's event is the earliest side='buy' per (network_id, mint) across ALL stored history (created_at, then id; prereg sec. 1). selected_wallet = the event's
source_user_id is in the frozen v2 list. Absent event (id not in memecoin_events) gives NULL for both, never false."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.research.source_entry_live import load_selected  # noqa: E402
from sniper.persistence.db_core import get_readonly_connection  # noqa: E402

LEDGER = "/home/bigpapi/Desktop/Sniper/sniper_tracker.db"
EARLIER = ("select 1 from memecoin_events p where p.side='buy' and p.mint=e.mint and p.network_id is e.network_id and "
           "(p.created_at < e.created_at or (p.created_at = e.created_at and p.id < e.id))")


def main():
    src, dst, sel_path = sys.argv[1:4]
    selected, sha = load_selected(sel_path)
    con = get_readonly_connection(LEDGER)
    n = absent = 0
    stats = {"selected": 0, "pair_first": 0, "not_buy": 0}
    with open(src) as fi, open(dst, "w") as fo:
        for line in fi:
            r = json.loads(line)
            row = con.execute("select e.side, e.source_user_id, not exists(%s) from memecoin_events e where e.id=?" % EARLIER, (r["event_id"],)).fetchone()
            if row is None:
                r.update(selected_wallet=None, pair_first=None)
                absent += 1
            else:
                side, uid, pf = row
                r["selected_wallet"] = uid in selected
                r["pair_first"] = bool(pf) if side == "buy" else None
                stats["selected"] += bool(r["selected_wallet"])
                stats["pair_first"] += bool(r["pair_first"])
                stats["not_buy"] += side != "buy"
            r["selected_list_sha"] = sha
            fo.write(json.dumps(r, sort_keys=True) + "\n")
            n += 1
    print(json.dumps({"rows": n, "event_absent": absent, "list_sha": sha, **stats}))


if __name__ == "__main__":
    main()
