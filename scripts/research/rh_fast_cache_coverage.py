"""Signals per UTC day by safety-cache state at the 4663 fast lane's safety step, live mode only
(spec 2026-10-14 rh-fast-cached-only §3.5). Read-only.

States:
- `cache`: a fresh passing verdict.
- `inline`: a miss read inline (before cached-only).
- `band_miss`: a miss the absolute source band refused before its inline read.
- `no_safety_verdict` / `safety_stale`: a miss refused (cached-only).

A refusal before the safety step (the snapshot, ETH price, build or state checks) is not counted,
although its row carries safety_path='cache'.

    python3 scripts/research/rh_fast_cache_coverage.py [lane_db_path]
"""
import sqlite3
import sys

_MISSES = ("no_safety_verdict", "safety_stale")
# LiveLane._precheck's refusals ahead of the safety reason (inputs_reason, the clip, state, build).
_BEFORE_SAFETY = ("no_eth_usd", "estimated_state", "build_refused", "eth_usd_stale", "float_short",
                  "generation_unknown", "no_snapshot", "snapshot_predates_execution", "snapshot_stale")


def state_of(path, reason):
    """The row's cache state at the safety step, or None when it never reached it."""
    reason = str(reason or "")
    if reason in _MISSES:
        return reason
    if path == "inline":
        return "inline"
    if path == "cache" and reason.startswith("source_band:"):
        return "band_miss"                     # set on the inline branch, before its read
    if path == "cache" and reason not in _BEFORE_SAFETY:
        return "cache"
    return None


def coverage(db):
    """{day: {state: n}} from rh_fast_lane_events (live rows only)."""
    out = {}
    for day, path, reason in db.execute(
            "SELECT substr(written_at, 1, 10), safety_path, reason FROM rh_fast_lane_events "
            "WHERE mode = 'live' AND (safety_path IS NOT NULL OR reason IN (?, ?))", _MISSES):
        state = state_of(path, reason)
        if state is not None:
            slot = out.setdefault(day, {})
            slot[state] = slot.get(state, 0) + 1
    return out


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "data/memecoin_rh_fast_lane.db"
    with sqlite3.connect("file:%s?mode=ro" % path, uri=True) as db:
        for day, counts in sorted(coverage(db).items()):
            print(day, " ".join("%s=%d" % kv for kv in sorted(counts.items())))
