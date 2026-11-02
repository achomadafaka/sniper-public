"""Office live floor: the desk candidate funnel (intake -> hard filter), read-only and bounded (Epsilon's funnel contract,
2026-10-29). One indexed all-stage rowid tail of desk_tickets, then Python hard_filter selection; no global scans, no JSON history walks.

Counts come from a capped tail, so they are OBSERVED lower-bound windows, never complete per-hour totals; `limited`
says when the all-stage tail was cut. The first failing check is the producer's own persisted reason (the hard filter stops at
its first refusal), never re-derived. An absent check is 'not_observed', never a pass. Office admission is a separate
universe (no receipt linkage here)."""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

LIMIT = 200          # all-stage tail rows (plus one sentinel)
TICKETS = 50         # ticket rows with per-check detail
CHECKS = ("market_data", "token_safety", "sell_probe", "holders", "cluster", "dump_group", "chart")


def _text(value, n=128):
    return value[:n] if isinstance(value, str) else None


def _first_failing(verdict, reason):
    """'<check>' from the producer's persisted reason: unknown:<check>:..., kill:<rule> (-> hard_kill), a bare
    dump_group_buyers, or None on PASS."""
    reason = _text(reason)
    if verdict == "PASS":
        return None
    if not isinstance(reason, str):
        return "unknown"
    if reason.startswith("unknown:"):
        return reason.split(":", 2)[1] or "unknown"
    if reason.startswith("kill:"):
        return "hard_kill"
    return reason.split(":", 1)[0]


def _stamp(ts):
    try:
        at = datetime.fromisoformat(ts)
        if at.tzinfo is None or at.utcoffset() is None:
            return None
        stamp = at.timestamp()
        return stamp if math.isfinite(stamp) and stamp >= 0 else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _hour(ts):
    stamp = _stamp(ts)
    return None if stamp is None else datetime.fromtimestamp(stamp, timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()


def _checks(checks_json):
    """{check: {status, reason}} as plain strings; a check the producer did not record is 'not_observed'."""
    try:
        checks = json.loads(checks_json)
        if not isinstance(checks, dict):
            raise TypeError
    except (TypeError, ValueError, AttributeError, RecursionError):
        return None
    out = {}
    for name in CHECKS:
        row = checks.get(name)
        out[name] = ({"status": _text(row.get("status"), 16), "reason": _text(row.get("reason"))}
                     if isinstance(row, dict) else {"status": "not_observed", "reason": None})
    return out


def read_funnel(state_root, *, now, host_now, open_ro, charge):
    """state_root = data/office; the desk store is its sibling data/memecoin_desk.db. open_ro(path) is the floor's
    budgeted read-only context manager, charge(n) its byte budget. Returns
    {state, items, sources, limited, coverage, tickets, blockers}; unknown counts are None, measured zero is 0."""
    root = Path(state_root)
    out = {"state": "unknown", "items": [], "sources": [], "limited": None, "tickets": [], "blockers": [],
           "coverage": "last %d tickets, all stages; hard_filter subset; observed lower bounds, not complete hourly totals" % LIMIT}
    source = {"source": "Desk hard filter", "state": "unknown", "limited": None,
              "clock_basis": "decision_as_of_utc", "covered_from": None, "covered_through": None}
    def unknown():
        out["sources"].append(source)
        out["items"].append({"source": "Desk hard filter", "stage": "hard_filter", "seat": None, "verdict": "Unknown",
                             "count": None, "hour_start": None, "reason": None})
        return out
    if type(now) not in (int, float) or not math.isfinite(now) or not 0 <= now <= 253402300799:
        return unknown()
    try:
        with open_ro(root.parent / "memecoin_desk.db") as conn:
            rows = conn.execute("SELECT id,ticket_id,stage,verdict,reason,ts FROM desk_tickets "
                                "ORDER BY rowid DESC LIMIT ?", (LIMIT + 1,)).fetchall()
            limited, rows = len(rows) > LIMIT, rows[:LIMIT]
            selected = [r for r in rows if r["stage"] == "hard_filter"]
            rows = [r for r in selected if (stamp := _stamp(r["ts"])) is not None and stamp <= now]
            omitted = len(selected) - len(rows)
            if omitted and not rows:
                out.update(limited=limited, omitted=omitted)
                source["limited"] = limited
                return unknown()
            detail = {}
            ids = [r["id"] for r in rows[:TICKETS]]
            if ids:
                # only the checks object crosses (json_extract), capped, so 50 rows stay inside the shared 1 MiB budget
                for r in conn.execute("SELECT id,substr(json_extract(inputs_json,'$.evidence.checks'),1,8192) AS checks "
                                      "FROM desk_tickets WHERE id IN (" + ",".join("?" * len(ids)) + ")", ids):
                    detail[r["id"]] = _checks(r["checks"])
    except Exception:             # missing db, budget exhausted, sqlite error: unknown, never empty
        return unknown()
    groups, blockers = {}, {}
    stamps = []
    for r in rows:
        verdict, reason = _text(r["verdict"], 16), _text(r["reason"])
        key = (verdict, reason, _hour(r["ts"]))
        groups[key] = groups.get(key, 0) + 1
        first = _first_failing(verdict, reason)
        if first is not None:
            blockers[first] = blockers.get(first, 0) + 1
        stamps.append(_stamp(r["ts"]))
    out["items"] = [{"source": "Desk hard filter", "stage": "hard_filter", "seat": None, "verdict": v, "count": n,
                     "hour_start": h, "reason": rs} for (v, rs, h), n in sorted(groups.items(), key=lambda kv: -kv[1])]
    out["blockers"] = [{"check": k, "count": n} for k, n in sorted(blockers.items(), key=lambda kv: -kv[1])]
    out["tickets"] = [{"ticket_id": _text(r["ticket_id"], 160), "at": _text(r["ts"], 40), "verdict": _text(r["verdict"], 16),
                       "first_failing_check": _first_failing(r["verdict"], r["reason"]), "reason": _text(r["reason"]),
                       "checks": detail.get(r["id"])} for r in rows[:TICKETS]]
    finite = [t for t in stamps if math.isfinite(t)]
    source.update(state="partial" if omitted else "known", limited=limited, covered_from=min(finite) if finite else None,
                  covered_through=max(finite) if finite else None)
    out.update(state="partial" if omitted else "known", limited=limited, omitted=omitted)
    out["sources"].append(source)
    return out
