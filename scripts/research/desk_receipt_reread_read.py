#!/usr/bin/env python3
"""entry_receipt_recovery reads/wait_ms and outcome per entry since a cutover (read-only). usage: ... <since_iso> [signal_source]"""
import json
import sqlite3
import sys

since = sys.argv[1]
src = sys.argv[2] if len(sys.argv) > 2 else None
c = sqlite3.connect("file:sniper_tracker.db?mode=ro", uri=True, timeout=10)
q = ("SELECT decision_started_at,signal_source,terminal_reason,trace_json FROM memecoin_entry_attempts "
     "WHERE decision_started_at>=? AND trace_json LIKE '%entry_receipt_recovery%'" + (" AND signal_source=?" if src else "") + " ORDER BY rowid")
rows = []


def find(o):
    if isinstance(o, dict):
        if o.get("name") == "entry_receipt_recovery":
            return o
        for v in o.values():
            r = find(v)
            if r:
                return r
    elif isinstance(o, list):
        for v in o:
            r = find(v)
            if r:
                return r


for at, s, tr, tj in c.execute(q, (since, src) if src else (since,)):
    sub = find(json.loads(tj))
    if sub:
        rows.append((at, s, tr, sub.get("outcome"), sub.get("reads"), sub.get("wait_ms")))
n = len(rows)
ok = [r for r in rows if r[3] == "receipt"]
w = sorted(r[5] for r in ok if r[5] is not None)
pc = lambda p: round(w[min(len(w) - 1, int(p * len(w)))], 1) if w else None
print("entries", n, "receipt", len(ok), "unreadable", sum(r[3] == "receipt_unreadable" for r in rows), "other", n - len(ok) - sum(r[3] == "receipt_unreadable" for r in rows))
print("reads dist (receipt):", {k: sum(r[4] == k for r in ok) for k in (1, 2, 3, 4)}, "wait_ms p50/p90 (receipt):", pc(.5), pc(.9))
for r in rows[-25:]:
    print(*r)
