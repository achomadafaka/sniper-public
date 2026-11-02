#!/usr/bin/env python3
"""Read the BSC approval-pipelining canary: did the first N pipelined BSC entries behave?

Pre-registered with Alpha on 2026-09-28, before MEMECOIN_EVM_APPROVAL_PIPELINE_CHAINS=4663,56 is set.
A pipelined approval is one whose evm.approval substage says `broadcast` (the receipt-first path
says `confirmed`). Per pipelined BSC entry attempt since --since:

  b  allowance revert : the attempt's terminal reason names an allowance / transferFrom failure
  c  stuck            : its reservation is still unresolved after 60 s, or recovered as ambiguous
  d  nonce refusal    : no provider accepted the approval or the swap, and a reason names the nonce
                        or an underpriced replacement (one provider's "nonce too low" beside another
                        provider's acceptance is a duplicate, not a refusal)
  e  speed            : evm.approval p50 and decision -> confirmed fill p50 against the receipt-first
                        baseline, the same chain before --since

  STOP (rollback at once) on any b, c or d. After N entries: e must show approval p50 < 0.5 s and
  a fill p50 at least 2 s under the baseline.

Exits: `_approve` is shared, so BSC exit approvals pipeline too. Exit submissions on 56 since --since
are counted by aggregate outcome; an exit none of whose providers accepted is listed.

Read-only (ledger opened read-only). No chain reads: a swap that filled proves its exact approval
was mined first, and one that reverted on allowance says so in its terminal reason.

    python3 scripts/research/bsc_approval_pipeline_canary.py --since 2026-09-28T08:00:00
    python3 scripts/research/bsc_approval_pipeline_canary.py --selftest
"""
import argparse
import contextlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LEDGER = REPO / "sniper_tracker.db"
ALLOWANCE_RE = re.compile(r"allowance|transfer_?from_?failed|transferfrom|stf\b", re.I)
NONCE_RE = re.compile(r"nonce too (low|high)|replacement transaction underpriced|underpriced", re.I)
STUCK_S = 60.0


def _ts(value):
    """An ISO instant; a stamp without an offset is UTC (the ledger's own stamps carry +00:00)."""
    t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _q(xs, p):
    xs = sorted(x for x in xs if x is not None)
    return None if not xs else xs[min(len(xs) - 1, int(p * len(xs)))]


def classify(attempt, pending, submissions, now):
    """The failed criteria (subset of 'bcd') for one pipelined attempt. Pure."""
    failed = set()
    if ALLOWANCE_RE.search(str(attempt.get("terminal_reason") or "")):
        failed.add("b")
    if pending is not None:
        unresolved = pending.get("status") not in ("confirmed", "failed")
        old = (now - _ts(pending["reserved_at"])).total_seconds() > STUCK_S
        if (unresolved and old) or "ambiguous" in str(pending.get("recovery_status") or ""):
            failed.add("c")
    by_action = {}
    for s in submissions:
        by_action.setdefault(s["action"], []).append(s)
    for rows in by_action.values():
        if not any(r["state"] == "accepted" for r in rows) and any(
                NONCE_RE.search(str(r.get("reason_code") or "")) for r in rows):
            failed.add("d")
    return failed


def _approval(trace):
    for e in trace.get("substage_events") or []:
        if e.get("name") == "evm.approval":
            return e
    return None


def read(conn, since, now, chain="56"):
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT a.attempt_id, a.pending_id, a.decision_started_at, a.terminal_status, "
        "a.terminal_reason, a.trace_json, p.opened_recorded_at FROM memecoin_entry_attempts a "
        "LEFT JOIN memecoin_positions p ON p.attempt_id = a.attempt_id "
        "WHERE a.network_id = ? AND a.decision_started_at >= ? "
        "AND a.trace_json LIKE '%evm.approval%' ORDER BY a.decision_started_at",
        (chain, since)).fetchall()
    out = {"pipelined": [], "receipt_first": []}
    for r in rows:
        appr = _approval(json.loads(r["trace_json"] or "{}"))
        if appr is None:
            continue
        pending = conn.execute(
            "SELECT status, reserved_at, recovery_status FROM memecoin_pending_entries WHERE id = ?",
            (r["pending_id"],)).fetchone() if r["pending_id"] is not None else None
        subs = [dict(s) for s in conn.execute(
            "SELECT action, state, reason_code FROM memecoin_submission_attempts "
            "WHERE owner_kind = 'reservation' AND owner_id = ?", (str(r["pending_id"]),))]
        fill_ms = ((_ts(r["opened_recorded_at"]) - _ts(r["decision_started_at"])).total_seconds() * 1000
                   if r["opened_recorded_at"] else None)
        row = {"attempt_id": r["attempt_id"], "at": r["decision_started_at"],
               "status": r["terminal_status"], "reason": r["terminal_reason"],
               "approval_ms": appr.get("duration_ms"), "fill_ms": fill_ms,
               "failed": sorted(classify(dict(r), dict(pending) if pending else None, subs, now))}
        out["pipelined" if appr.get("outcome") == "broadcast" else "receipt_first"].append(row)
    exits = conn.execute(
        "SELECT owner_id, SUM(state = 'accepted') AS accepted, COUNT(*) AS n "
        "FROM memecoin_submission_attempts WHERE chain_id = ? AND action LIKE 'exit%' "
        "AND created_at >= ? GROUP BY owner_id", (chain, since)).fetchall()
    out["exits"] = [dict(e) for e in exits]
    return out


def baseline(conn, since, chain, days=7):
    lo = (_ts(since) - timedelta(days=days)).isoformat()
    return read(conn, lo, _ts(since), chain)["receipt_first"]


def report(args):
    now = _ts(args.now) if args.now else datetime.now(timezone.utc)
    with contextlib.closing(sqlite3.connect("file:%s?mode=ro" % args.db, uri=True, timeout=10)) as c:
        cur = read(c, args.since, now, args.chain)
        base = baseline(c, args.since, args.chain)
    pipe = cur["pipelined"][:args.n]
    print("pipelined entry attempts on %s since %s: %d (reading the first %d)"
          % (args.chain, args.since, len(cur["pipelined"]), len(pipe)))
    for r in pipe:
        print("  %s %-10s approval %6s ms  fill %6s ms  failed %s  %s" % (
            r["at"][:19], r["status"], "%.0f" % r["approval_ms"] if r["approval_ms"] else "-",
            "%.0f" % r["fill_ms"] if r["fill_ms"] else "-", ",".join(r["failed"]) or "-",
            (r["reason"] or "")[:60]))
    stop = sorted({f for r in pipe for f in r["failed"]})
    for name, rows in (("pipelined", pipe), ("baseline receipt-first 7 d", base)):
        print("  %-28s n=%-3d approval p50 %s ms  fill p50 %s ms" % (
            name, len(rows), _fmt(_q([r["approval_ms"] for r in rows], .5)),
            _fmt(_q([r["fill_ms"] for r in rows], .5))))
    bad_exits = [e for e in cur["exits"] if not e["accepted"]]
    print("  exits on %s since: %d, with no accepted provider: %d %s"
          % (args.chain, len(cur["exits"]), len(bad_exits), [e["owner_id"] for e in bad_exits]))
    if stop:
        print("STOP: criteria %s failed -> MEMECOIN_EVM_APPROVAL_PIPELINE_CHAINS=4663 and restart"
              % ",".join(stop))
    elif len(pipe) >= args.n:
        ap, fp = _q([r["approval_ms"] for r in pipe], .5), _q([r["fill_ms"] for r in pipe], .5)
        bf = _q([r["fill_ms"] for r in base], .5)
        ok = ap is not None and ap < 500 and fp is not None and bf is not None and fp <= bf - 2000
        print("VERDICT after %d: %s" % (len(pipe), "PASS" if ok else "REVIEW (speed criterion e)"))
    else:
        print("no stop criterion hit; %d of %d entries so far" % (len(pipe), args.n))


def _fmt(x):
    return "-" if x is None else "%.0f" % x


def selftest():
    now = _ts("2026-09-28T09:00:00+00:00")
    ok_pending = {"status": "confirmed", "reserved_at": "2026-09-28T08:59:00+00:00",
                  "recovery_status": None}
    assert classify({"terminal_reason": None}, ok_pending, [], now) == set()
    assert _ts("2026-09-28T09:00:00") == now, "a bare --since is UTC"
    assert classify({"terminal_reason": "chain_error:execution reverted: TRANSFER_FROM_FAILED"},
                    ok_pending, [], now) == {"b"}
    stuck = dict(ok_pending, status="reserved", reserved_at="2026-09-28T08:58:00+00:00")
    assert classify({}, stuck, [], now) == {"c"}
    assert classify({}, dict(stuck, reserved_at="2026-09-28T08:59:30+00:00"), [], now) == set()
    dup = [{"action": "entry_approval", "state": "accepted", "reason_code": None},
           {"action": "entry_approval", "state": "ambiguous", "reason_code": "nonce too low: x"}]
    assert classify({}, ok_pending, dup, now) == set(), "a duplicate beside an acceptance is fine"
    refused = [{"action": "entry", "state": "ambiguous", "reason_code": "nonce too low: x"}]
    assert classify({}, ok_pending, refused, now) == {"d"}
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--since")
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--chain", default="56", help="56 for the canary; 4663 replays the reader on "
                                                  "a chain that already pipelines")
    ap.add_argument("--now", help="evaluation instant (default: now, UTC)")
    ap.add_argument("--db", default=str(LEDGER))
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.since:
        ap.error("--since is required")
    report(a)


if __name__ == "__main__":
    main()
