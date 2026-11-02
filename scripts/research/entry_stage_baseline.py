#!/usr/bin/env python3
"""Entry-stage baseline per chain and per lane, with every opportunity in the denominator (Gamma,
2026-09-26, operator acceptance metric). Extended 2026-09-27 for the overnight scoreboard: the
routed lane split from the watcher, fills by execution route, a JSON summary and a delta mode, the
writer of every stamp, and the stages with no stamp as the next build items.

An OPPORTUNITY is one source buy a lane saw:
  j7        memecoin_events side='buy', delivery_lane j7_wallets/j7_pump, by received_at
  watcher   onchain_wallet_detections, by detected_at (solana only; its own attempts carry
            origin_event_id 'onchain:<signature>')
  routed    onchain_wallet_detections whose source_valuation_json basis is 'slot_lane_routed'
            (written by the slot lane's RoutedHandoff, live since 2026-09-27T20:18:05Z); same
            'onchain:<signature>' origins as the watcher, reported apart
  slot_lane every handled signal in logs/memecoin_runner.log ("slot lane <handle> <mint> S=..."),
            the lane writes no ledger row for a refusal; its attempts carry 'slotlane:<sig>'.
            The line may carry "head-S=.. recv-S=.. land-S=.." after ms=: a pattern anchored at
            head-S (this script until 2026-09-27) silently dropped every send, fills included
Each opportunity gets ONE outcome from its attempts (joined on origin_event_id): filled, roster
(not_approved / unroutable_chain / quiet_hours / kill_switch_tripped: our own state, not the
market), refused (every other refused, aborted or safety_failed), failed, unknown (no terminal
status or any other value), or missed (no attempt row at all). Filled rate = filled/opportunities.

Stage stamps, all ours unless stated (table.column):
  source evidence  j7: memecoin_events.created_at (the SOURCE'S CLAIM, untrusted; negative gaps
                   are clock skew); watcher: onchain_wallet_detections.block_time (chain, 1 s
                   resolution); slot lane: NO STAMP in the ledger (S only from a chain read)
  detection        j7: memecoin_events.received_at; watcher: onchain_wallet_detections.detected_at;
                   slot lane: NO USABLE STAMP (attempts.observed_at is written ~50-110 ms after
                   decision_started_at, so it is not a receive time)
  execution start  memecoin_entry_attempts.decision_started_at
  signed           memecoin_entry_attempts.signing_completed_at
  first accept     memecoin_submission_attempts: min(created_at + latency_ms) over state='accepted',
                   action='entry', owner_kind='reservation', owner_id = attempts.pending_id
  inclusion        NO STAMP (the block/slot time of our own tx is not in the ledger)
  confirmed seen   memecoin_entry_attempts.chain_confirmation_completed_at
  position ready   memecoin_positions.opened_recorded_at (joined on attempt_id)
  exit ready       memecoin_route_series first row (lowest id) with seam='exit_decision'
Stage percentiles use every attempt that REACHED both stamps (n printed), refusals included where
they reach a stage; the outcome table beside them carries the full denominator.

WRITERS (code that writes each stamp):
  memecoin_events.received_at                  db_memecoin.insert_event (J7 socket ingest)
  onchain_wallet_detections.detected_at        onchain_wallet_watch._store_detection (watcher);
                                               memecoin_slot_lane.RoutedHandoff.write (routed lane,
                                               = the lane socket's receipt, host clock)
  onchain_wallet_detections.block_time         the watcher, from getTransaction blockTime (1 s)
  attempts.decision_started_at / signing_completed_at / chain_confirmation_completed_at
                                               memecoin_entry_trace.EntryTrace.mark, via
                                               entry_attribution.trace_mark_submission
  memecoin_submission_attempts created_at, latency_ms
                                               db_memecoin.record_submission_attempts (submission
                                               transport, one row per leg)
  memecoin_positions.opened_recorded_at        db_memecoin.open_position (real-UTC at the fill record)
  memecoin_route_series.observed_at            memecoin_real_runner._keep_route_quote

LEGACY STAMP GAPS (historical rows; inspect current fill timing coverage):
  1. inclusion: the slot/block time of OUR entry tx, all chains (only our receipt observation exists)
  2. slot lane source evidence: the source tx's slot S / block time (only a chain read gives it)
  3. slot lane detection: the lane's receive instant (attempts.observed_at is written after
     decision_started_at, so it is not one)
  4. J7 source evidence is the source's CLAIM (memecoin_events.created_at), never verified
  5. EVM: signed->accept reads negative on approval-pipelined 4663/56 entries, because the accepted
     'entry' leg can precede the swap's signing stamp; the stage needs a per-leg signing stamp

CLOCKS (2026-09-27, Lambda): compute every latency on ONE clock. J7 received_at and every
attempt stamp share the corrected clock; watcher / routed detected_at and the runner log's _p
lines are HOST clock. Watcher/routed detect->* stages are therefore replaced by detect->lane_result,
host at both ends (the lane's 'onchain lane: <handle> <mint8> -> ...' line).

ROUTE: for a fill, the Solana builder (trace timing_swap_build.builder), else the EVM executed-pool
source (trace evm.executed_pool.source) plus the router prefix (attempts.quote_route_target).

--bsc-reads (optional, research host bsc-dataseed1.defibit.io, >= 1 s apart) splits chain 56's
approval -> swap gap: approval inclusion (block milliTimestamp) vs our receipt observation
(evm.approval substage completed_at), and the swap receipt observation lag the same way.

    python3 scripts/research/entry_stage_baseline.py --days 7
    python3 scripts/research/entry_stage_baseline.py --hours 2 --json out.json
    python3 scripts/research/entry_stage_baseline.py --delta before.json after.json
    python3 scripts/research/entry_stage_baseline.py --days 7 --bsc-reads
    python3 scripts/research/entry_stage_baseline.py --selftest
"""
import argparse
import collections
import contextlib
import glob
import gzip
import json
import os
import re
import sqlite3
import time
import urllib.request
from datetime import datetime, timedelta, timezone

REPO = "/home/bigpapi/Desktop/Sniper"
DB = os.path.join(REPO, "sniper_tracker.db")
LOG = os.path.join(REPO, "logs", "memecoin_runner.log")
BSC = "https://bsc-dataseed1.defibit.io"
ROSTER = ("not_approved", "unroutable_chain", "quiet_hours", "kill_switch_tripped")
LANE_LINE = re.compile(r"^(\S+) \[memecoin\] slot lane \S+ \S+ S=\S+ -> (\S+) ?(.*?) ms=\S+ head-S=\S+(?: .*)?$")
STAGES = [("detect->start(first)", "detect_first", "start"), ("start->signed", "start", "signed"),
          ("signed->accept", "signed", "accept"), ("accept->confirm_seen", "accept", "confirm"),
          ("confirm_seen->position", "confirm", "position"), ("position->exit_ready", "position", "exit"),
          ("detect->position", "detect", "position")]


def log_lines():
    """Current and rotated lane results; copytruncate overlap is counted once."""
    lines = set()
    for path in glob.glob(LOG + "*"):
        opener = gzip.open if path.endswith(".gz") else open
        try:
            with opener(path, "rt", errors="replace") as fh:
                lines.update(line for line in fh if "slot lane " in line or "onchain lane: " in line)
        except FileNotFoundError:  # rotation during a read
            continue
    yield from sorted(lines)


def ts(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")) if s else None


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else None


def route_label(trace_json, route_target):
    """Execution route of a fill: Solana builder, else EVM executed-pool source + router prefix."""
    try:
        subs = (json.loads(trace_json or "{}") or {}).get("substage_events") or []
    except (TypeError, ValueError):
        subs = []
    builder = next((e.get("builder") for e in subs if e.get("name") == "timing_swap_build"
                    and e.get("builder")), None)
    if builder:
        return str(builder)
    pool_src = next((e.get("source") for e in subs if e.get("name") == "evm.executed_pool"
                     and e.get("source")), None)
    target = str(route_target or "")
    router = target[:10] if target.startswith("0x") else (target if target in ("raydium", "jupiter") else "")
    label = "/".join(x for x in (pool_src, router) if x)
    return label or "unknown"


NO_STAMP = ("J7 source evidence is the source's claim, unverified",
            "EVM per-leg signing stamp (signed->accept reads negative on approval-pipelined entries)")


def slot_coverage(attempts):
    from sniper.services.memecoin_fill_timing import facts
    rows = [facts(a) for a in attempts]
    return {"attempts": len(rows), **{key + "_missing": sum(r[key] is None for r in rows)
                                     for key in ("source_slot", "landing_slot", "recv_slot")}}


def outcome(attempts, rows=()):
    """One outcome per opportunity from all its attempts (a retry can follow a refusal). A position
    on any attempt is a fill even when the trace never finished (37 Solana fills 09-25/26 lost it)."""
    if not attempts:
        return "missed"
    if any(r.get("position") for r in rows):
        return "filled"
    statuses = [(a["terminal_status"], a["terminal_reason"] or "") for a in attempts]
    if any(s == "filled" for s, _ in statuses):
        return "filled"
    s, r = statuses[-1]
    if s in ("refused", "aborted", "safety_failed"):
        return "roster" if r in ROSTER else "refused"
    if s == "failed":
        return "failed"
    return "unknown"


LANE_RESULT = re.compile(r"^(\S+) \[memecoin\] onchain lane: (\S+) (\S+) -> (\S+)")


def lane_result_lag(lane_lines, detected_at, window_s=60.0):
    """ONE-CLOCK detect->lane result: the host-clock seconds from a detection (host detected_at) to
    the on-chain lane's first 'onchain lane: <handle> <mint8> -> ...' line (host-stamped by _p) at
    or after it, within window_s. None when the lane wrote no line for it.

    WHY (2026-09-27, Lambda): detected_at is HOST clock and decision_started_at is the CORRECTED
    clock (host - offset_s), and offset_s is rewritten on every J7 message as host_now minus J7's
    received_at, so it carries relay transit (86.5 ms at 23:53Z) and moves message to message.
    decision_started_at minus a host stamp is therefore not a latency; this script's earlier fixed
    offset (0.052 s) read the routed lane +35 ms slow that never was.

    lane_lines are (stamp, result word); returns (seconds, result) or None. A "filled" line is written
    AFTER the fill (2026-09-28: LingardSol 66271, 2.45 s), so callers keep fills apart from decisions."""
    t0 = ts(detected_at)
    for stamp, result in lane_lines:
        s = ts(stamp)
        if s >= t0 - timedelta(milliseconds=1):
            d = (s - t0).total_seconds()
            return (d, result) if d <= window_s else None
    return None


def stamps(conn, a, detect, first=True, route_default="unknown"):
    pending = a["pending_id"]
    if pending is None:   # the slot lane never writes attempts.pending_id; its reservation names it
        r = conn.execute("SELECT id FROM memecoin_pending_entries WHERE attempt_id=?", (a["attempt_id"],)).fetchone()
        pending = r[0] if r else None
    acc = conn.execute(
        "SELECT created_at, latency_ms FROM memecoin_submission_attempts WHERE owner_kind='reservation' "
        "AND owner_id=? AND action='entry' AND state='accepted'", (str(pending),)).fetchall() \
        if pending is not None else []
    accept = min((ts(r[0]) + timedelta(milliseconds=r[1] or 0) for r in acc), default=None)
    pos = conn.execute("SELECT id, opened_recorded_at FROM memecoin_positions WHERE attempt_id=?",
                       (a["attempt_id"],)).fetchone()
    exit_ready = route = None
    if pos:
        t = conn.execute("SELECT trace_json, quote_route_target FROM memecoin_entry_attempts "
                         "WHERE attempt_id=?", (a["attempt_id"],)).fetchone()
        route = route_label(t[0], t[1]) if t else "unknown"
        route = route_default if route == "unknown" else route
        r = conn.execute("SELECT observed_at FROM memecoin_route_series WHERE position_id=? AND "
                         "seam='exit_decision' ORDER BY id LIMIT 1", (pos[0],)).fetchone()
        exit_ready = ts(r[0]) if r else None
    det = ts(detect) if detect else None
    return {"detect": det, "detect_first": det if first else None, "start": ts(a["decision_started_at"]), "signed": ts(a["signing_completed_at"]),
            "accept": accept, "confirm": ts(a["chain_confirmation_completed_at"]),
            "position": ts(pos[1]) if pos else None, "exit": exit_ready, "route": route}


SUMMARY = {}


def report(title, opp_outcomes, stamp_rows, source_gaps=None, source_label="", key=None):
    n = len(opp_outcomes)
    c = collections.Counter(opp_outcomes)
    summ = SUMMARY.setdefault(key or title, {"opportunities": n, "outcomes": dict(c), "stages": {},
                                             "routes": {}})
    print("\n### %s  opportunities=%d" % (title, n))
    if n:
        print("  outcomes: " + ", ".join("%s %d (%.1f%%)" % (k, c[k], 100 * c[k] / n) for k in
                                          ("filled", "refused", "roster", "failed", "unknown", "missed")))
    if source_gaps is not None:
        neg = sum(1 for g in source_gaps if g < 0)
        print("  %s: n=%d p50=%s p90=%s negative=%d" % (source_label, len(source_gaps), fmt(pct(source_gaps, .5)),
                                                       fmt(pct(source_gaps, .9)), neg))
    for name, a, b in STAGES:
        d = [(r[b] - r[a]).total_seconds() for r in stamp_rows if r.get(a) and r.get(b)]
        summ["stages"][name] = [len(d), pct(d, .5), pct(d, .9)]
        print("  %-24s n=%-5d p50=%-8s p90=%s" % (name, len(d), fmt(pct(d, .5)), fmt(pct(d, .9))))
    by_route = collections.defaultdict(list)
    for r in stamp_rows:
        if r.get("position") and r.get("start"):
            by_route[r.get("route") or "unknown"].append((r["position"] - r["start"]).total_seconds())
    for route in sorted(by_route, key=lambda k: -len(by_route[k])):
        d = by_route[route]
        summ["routes"][route] = [len(d), pct(d, .5), pct(d, .9)]
        print("  fills by route %-28s n=%-4d start->position p50=%-8s p90=%s" % (
            route, len(d), fmt(pct(d, .5)), fmt(pct(d, .9))))


def fmt(x):
    return "-" if x is None else "%.3f" % x


def attempts_by_origin(conn, lo, hi, like=None):
    q = ("SELECT attempt_id, origin_event_id, network_id, pending_id, decision_started_at, observed_at, "
         "signing_completed_at, chain_confirmation_completed_at, terminal_status, terminal_reason "
         "FROM memecoin_entry_attempts WHERE decision_started_at>=? AND decision_started_at<=?")
    args = [lo.isoformat(), (hi + timedelta(minutes=10)).isoformat()]
    if like:
        q += " AND origin_event_id LIKE ?"
        args.append(like)
    out = collections.defaultdict(list)
    for a in conn.execute(q + " ORDER BY decision_started_at", args):
        out[a["origin_event_id"]].append(a)
    return out


def j7(conn, lo, hi):
    by_origin = attempts_by_origin(conn, lo, hi)
    events = conn.execute(
        "SELECT fomo_event_id, network_id, created_at, received_at FROM memecoin_events WHERE side='buy' "
        "AND delivery_lane IN ('j7_wallets','j7_pump') AND received_at>=? AND received_at<=?",
        (lo.isoformat(), hi.isoformat())).fetchall()
    per_chain = collections.defaultdict(lambda: ([], [], []))
    for e in events:
        outs, rows, gaps = per_chain[e["network_id"]]
        atts = by_origin.get(e["fomo_event_id"], [])
        mine = [stamps(conn, a, e["received_at"], i == 0) for i, a in enumerate(atts)]
        outs.append(outcome(atts, mine))
        rows.extend(mine)
        if e["created_at"] and e["received_at"]:
            gaps.append((ts(e["received_at"]) - ts(e["created_at"])).total_seconds())
    for chain in sorted(per_chain, key=lambda k: -len(per_chain[k][0])):
        outs, rows, gaps = per_chain[chain]
        report("j7 / chain %s" % chain, outs, rows, gaps, "source claim->received (UNTRUSTED)",
               key="j7/%s" % chain)


def watcher(conn, lo, hi):
    by_origin = attempts_by_origin(conn, lo, hi, "onchain:%")
    dets = conn.execute("SELECT signature, source_user_id, handle, mint, detected_at, detect_latency_s, "
                        "json_extract(source_valuation_json, '$.basis') AS basis "
                        "FROM onchain_wallet_detections "
                        "WHERE network_id='solana' AND detected_at>=? AND detected_at<=?",
                        (lo.isoformat(), hi.isoformat())).fetchall()
    import sys
    sys.path.insert(0, REPO)
    sys.path.insert(0, os.path.join(REPO, "scripts", "research"))
    from sniper.persistence.db_memecoin import event_transaction_signature
    from solana_onchain_lane_funnel import armed_at
    j7_sigs = {event_transaction_signature(r[0]) for r in conn.execute(
        "SELECT fomo_event_id FROM memecoin_events WHERE network_id='solana' AND received_at>=? AND received_at<=?",
        (lo.isoformat(), (hi + timedelta(minutes=10)).isoformat()))}
    groups = {"watcher": ([], [], [], collections.Counter()), "routed": ([], [], [], collections.Counter())}
    lags = {"watcher": [], "routed": []}
    fill_lags = {"watcher": [], "routed": []}
    lane_lines = collections.defaultdict(list)
    with contextlib.nullcontext(log_lines()) as fh:
        for line in fh:
            if "onchain lane: " in line:
                m = LANE_RESULT.match(line)
                if m:
                    lane_lines[(m.group(2), m.group(3))].append((m.group(1), m.group(4)))
    print("\n(watcher/routed detections are HOST clock; decision_started_at is the CORRECTED clock, "
          "so their detect->* stages are NOT computed. detect->lane_result is host at both ends.)")
    for d in dets:
        name = "routed" if d["basis"] == "slot_lane_routed" else "watcher"
        outs, rows, gaps, missed = groups[name]
        atts = by_origin.get("onchain:" + d["signature"], [])
        # detect=None: a host detection against a corrected-clock start is not a latency.
        mine = [stamps(conn, a, None, i == 0) for i, a in enumerate(atts)]
        lag = lane_result_lag(sorted(lane_lines.get((d["handle"], d["mint"][:8]), [])), d["detected_at"])
        if lag is not None:
            (fill_lags if lag[1] == "filled" else lags)[name].append(lag[0])
        o = outcome(atts, mine)
        outs.append(o)
        rows.extend(mine)
        if d["detect_latency_s"] is not None:
            gaps.append(d["detect_latency_s"])
        if o == "missed":
            arm = armed_at(conn, d["source_user_id"], d["detected_at"])
            missed["j7 saw the same tx" if d["signature"] in j7_sigs else
                   {True: "armed, no attempt", False: "not armed", None: "arm unknown"}[arm]] += 1
    for name, title in (("watcher", "watcher / solana (lane-labelled attempts only; the J7 lane may act on the same swap first)"),
                        ("routed", "routed lane / solana (slot lane RoutedHandoff rows; detection = the lane socket's receipt)")):
        outs, rows, gaps, missed = groups[name]
        report(title, outs, rows, gaps, "block_time->detected (chain, 1 s res)", key="%s/solana" % name)
        SUMMARY["%s/solana" % name]["missed_split"] = dict(missed)
        d = lags[name]
        SUMMARY["%s/solana" % name]["stages"]["detect->lane_result(host)"] = [len(d), pct(d, .5), pct(d, .9)]
        print("  %-24s n=%-5d p50=%-8s p90=%s   (ONE clock: host detected_at -> host lane line; fills excluded)" % (
            "detect->lane_result", len(d), fmt(pct(d, .5)), fmt(pct(d, .9))))
        f = fill_lags[name]
        SUMMARY["%s/solana" % name]["stages"]["detect->filled_line(host)"] = [len(f), pct(f, .5), pct(f, .9)]
        print("  %-24s n=%-5d p50=%-8s p90=%s   (host; the lane writes 'filled' after the fill)" % (
            "detect->filled_line", len(f), fmt(pct(f, .5)), fmt(pct(f, .9))))
        print("  missed split (approvals ledger at the detection instant, chain='solana' actions only): %s" % dict(missed))


def slot_lane(conn, lo, hi):
    outs = []
    with contextlib.nullcontext(log_lines()) as fh:
        for line in fh:
            if "slot lane" not in line:
                continue
            m = LANE_LINE.match(line.rstrip("\n"))
            if not m or not (lo <= ts(m.group(1)) <= hi):
                continue
            status, reason = m.group(2), m.group(3)
            outs.append({"filled": "filled", "failed": "failed", "unknown": "unknown", "error": "unknown"}.get(
                status, "refused" if status in ("rejected", "aborted") else "unknown"))
    by_origin = attempts_by_origin(conn, lo, hi, "slotlane:%")
    # attempts.observed_at is written ~50-110 ms AFTER decision_started_at on every lane attempt, so it
    # is not a receive stamp: detection has NO usable stamp on this lane and detect->* is left out.
    rows = [stamps(conn, a, None, route_default="slot_lane_prebuilt") for atts in by_origin.values() for a in atts]
    report("slot_lane / solana (outcomes from the runner log; roster refusals are not separable there; no detection stamp)",
           outs, rows, key="slot_lane/solana")
    durable = [outcome(atts, [stamps(conn, a, None) for a in atts]) for atts in by_origin.values()]
    SUMMARY["slot_lane/solana"]["durable_attempt_outcomes"] = dict(collections.Counter(durable))
    SUMMARY["slot_lane/solana"]["log_coverage"] = "retained rotations only; pre-attempt losses unknown"
    coverage = slot_coverage(dict(a) for a in conn.execute(
        "SELECT * FROM memecoin_entry_attempts WHERE decision_started_at>=? "
        "AND decision_started_at<=? AND origin_event_id LIKE 'slotlane:%'", (lo.isoformat(), hi.isoformat())))
    SUMMARY["slot_lane/solana"]["attempt_trace_slot_coverage"] = coverage
    print("  attempt/trace slot coverage: %s (detection JSON and resolver backfill excluded)" % coverage)
    print("  durable attempted origins: %s (separate from retained log denominator)" %
          dict(collections.Counter(durable)))


def bsc_split(conn, lo, hi):
    """Chain 56: approval inclusion vs our receipt observation, and the swap's observation lag."""
    def rpc(method, params):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        r = json.loads(urllib.request.urlopen(urllib.request.Request(
            BSC, body, {"Content-Type": "application/json", "User-Agent": "curl/8.5.0"}), timeout=15).read())
        time.sleep(1.05)
        if r.get("error"):
            raise RuntimeError(r["error"])
        return r["result"]

    def included_ms(tx_hash):
        rc = rpc("eth_getTransactionReceipt", [tx_hash])
        if not rc:
            return None, None
        blk = rpc("eth_getBlockByNumber", [rc["blockNumber"], False])
        return int(blk["milliTimestamp"], 16) / 1000.0, rc.get("status")

    rows = conn.execute(
        "SELECT a.attempt_id, a.pending_id, a.trace_json, a.chain_confirmation_completed_at FROM memecoin_entry_attempts a "
        "WHERE a.network_id='56' AND a.terminal_status='filled' AND a.decision_started_at>=? AND a.decision_started_at<=?",
        (lo.isoformat(), hi.isoformat())).fetchall()
    out = collections.defaultdict(list)
    for a in rows:
        sub = {e["name"]: e for e in json.loads(a["trace_json"] or "{}").get("substage_events") or []
               if e.get("name") in ("evm.approval", "evm.confirmation")}
        legs = {r[0]: r for r in conn.execute(
            "SELECT action, tx_identity, created_at, latency_ms FROM memecoin_submission_attempts WHERE owner_kind="
            "'reservation' AND owner_id=? AND state='accepted' ORDER BY id", (str(a["pending_id"]),))}
        for action, seen_at in (("entry_approval", (sub.get("evm.approval") or {}).get("completed_at")),
                                ("entry", a["chain_confirmation_completed_at"])):
            leg = legs.get(action)
            if not leg or not leg[1] or not seen_at:
                continue
            inc, status = included_ms(leg[1])
            if inc is None:
                continue
            sent = ts(leg[2]).timestamp()
            out[action].append((inc - sent, ts(seen_at).timestamp() - inc, status))
    for action, xs in out.items():
        inc = [x[0] for x in xs]
        lag = [x[1] for x in xs]
        print("\n### chain 56 %s (n=%d): send->inclusion p50=%s p90=%s | inclusion->we saw receipt p50=%s p90=%s "
              "min=%s max=%s | receipt status %s" % (
                  action, len(xs), fmt(pct(inc, .5)), fmt(pct(inc, .9)), fmt(pct(lag, .5)), fmt(pct(lag, .9)),
                  fmt(min(lag)), fmt(max(lag)), dict(collections.Counter(x[2] for x in xs))))


def delta(before, after):
    """Lines describing what moved between two --json summaries (same keys only)."""
    out = []
    for key in sorted(set(before) | set(after)):
        a, b = before.get(key), after.get(key)
        if a is None or b is None:
            out.append("%s: %s" % (key, "new" if a is None else "gone"))
            continue
        oa, ob = a.get("outcomes", {}), b.get("outcomes", {})
        moved = {k: (oa.get(k, 0), ob.get(k, 0)) for k in set(oa) | set(ob) if oa.get(k, 0) != ob.get(k, 0)}
        out.append("%s: opportunities %d -> %d%s" % (key, a.get("opportunities", 0), b.get("opportunities", 0),
                                                     (" outcomes %s" % moved) if moved else ""))
        for stage, (n1, p501, p901) in sorted(a.get("stages", {}).items()):
            n2, p502, p902 = b.get("stages", {}).get(stage, [0, None, None])
            if (n1, p501) != (n2, p502):
                out.append("  %-24s n %d -> %d  p50 %s -> %s  p90 %s -> %s" % (
                    stage, n1, n2, fmt(p501), fmt(p502), fmt(p901), fmt(p902)))
    return out


def selftest():
    A = lambda s, r="": {"terminal_status": s, "terminal_reason": r}
    assert outcome([]) == "missed"
    assert outcome([A("refused", "not_approved")]) == "roster"
    assert outcome([A("refused", "exact quote is +40% above source price (cap 30%)"), A("filled", "filled")]) == "filled"
    assert outcome([A("aborted", "no quote")]) == "refused"
    assert outcome([A("failed", "chain_error")]) == "failed"
    assert outcome([A(None, "")]) == "unknown"
    assert outcome([A(None, "")], [{"position": 1}]) == "filled"   # a lost trace still filled
    assert pct([3, 1, 2], .5) == 2 and pct([], .5) is None
    m = LANE_LINE.match("2026-09-26T21:34:07.415299+00:00 [memecoin] slot lane notdecu Gm9gqiHu S=450796749 -> "
                        "rejected source buy $30 (0.094%) under mcap floor 0.500% ($158) ms=0.0 head-S=None")
    assert m and m.group(2) == "rejected"
    # A SEND line carries recv-S/land-S after head-S (real frame, 2026-09-27T18:23:06Z). The pattern
    # anchored at head-S dropped every one of these, fills included.
    m = LANE_LINE.match("2026-09-27T18:23:06.263296+00:00 [memecoin] slot lane onmycheck BT7xGGQF S=451076278 "
                        "-> filled  ms=669.7 head-S=0 recv-S=0 land-S=1")
    assert m and m.group(2) == "filled", m
    assert not LANE_LINE.match("2026-09-27T20:24:22.280231+00:00 [memecoin] slot lane routed live DexGemsReal "
                               "F9SAHZVQ S=451103421 sig=3ydS usdc=1495.00 tokens=1 recv=x pair_ms=0.5 -> inserted")
    trace = json.dumps({"substage_events": [{"name": "timing_swap_build", "builder": "jupiter_swap"}]})
    assert route_label(trace, None) == "jupiter_swap"
    evm = json.dumps({"substage_events": [{"name": "evm.executed_pool", "source": "receipt_v4_swap"}]})
    assert route_label(evm, "0xccc88a9d1b4ed6b0eaba") == "receipt_v4_swap/0xccc88a9d"
    assert route_label(None, None) == "unknown" and route_label("{x", "raydium") == "raydium"
    # "t" keeps its n and moves its p50: a delta keyed on n alone would hide it.
    d = delta({"j7/56": {"opportunities": 3, "outcomes": {"filled": 1},
                         "stages": {"s": [1, 2.0, 3.0], "t": [3, 1.0, 2.0]}}},
              {"j7/56": {"opportunities": 5, "outcomes": {"filled": 2},
                         "stages": {"s": [2, 1.0, 3.0], "t": [3, 1.5, 2.0]}},
               "routed/solana": {}})
    assert d[0].startswith("j7/56: opportunities 3 -> 5") and "filled" in d[0] and "p50 2.000 -> 1.000" in d[1], d
    assert "p50 1.000 -> 1.500" in d[2] and d[3] == "routed/solana: new", d
    lines = [("2026-09-27T23:00:00.010000+00:00", "skipped"), ("2026-09-27T23:00:00.050000+00:00", "filled")]
    lag, res = lane_result_lag(lines, "2026-09-27T23:00:00.030000+00:00")
    assert abs(lag - 0.020) < 1e-9 and res == "filled", (lag, res)
    assert lane_result_lag(lines, "2026-09-27T23:00:01+00:00") is None          # no line after it
    assert lane_result_lag([("2026-09-27T23:02:00+00:00", "x")], "2026-09-27T23:00:00+00:00") is None  # > 60 s
    m = LANE_RESULT.match("2026-09-27T23:53:48.018979+00:00 [memecoin] onchain lane: OmakaseOnly CvUX4G2t "
                          "-> skipped duplicate_of_feed")
    assert m and m.group(2) == "OmakaseOnly" and m.group(3) == "CvUX4G2t" and m.group(4) == "skipped"
    print("selftest ok")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=float, default=7.0)
    p.add_argument("--hours", type=float)
    p.add_argument("--json", help="write the per-section summary here")
    p.add_argument("--delta", nargs=2, metavar=("BEFORE", "AFTER"), help="print what moved between two --json files")
    p.add_argument("--bsc-reads", action="store_true")
    p.add_argument("--selftest", action="store_true")
    args = p.parse_args()
    if args.selftest:
        selftest()
        raise SystemExit
    if args.delta:
        load = lambda path: (lambda j: j.get("sections", j))(json.load(open(path)))
        for line in delta(load(args.delta[0]), load(args.delta[1])):
            print(line)
        raise SystemExit
    hi = datetime.now(timezone.utc)
    lo = hi - (timedelta(hours=args.hours) if args.hours else timedelta(days=args.days))
    print("window %s .. %s" % (lo.isoformat(timespec="seconds"), hi.isoformat(timespec="seconds")))
    with contextlib.closing(sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=30)) as conn:
        conn.row_factory = sqlite3.Row
        j7(conn, lo, hi)
        watcher(conn, lo, hi)
        slot_lane(conn, lo, hi)
        if args.bsc_reads:
            bsc_split(conn, lo, hi)
    print("\nClock limitations:")
    for i, item in enumerate(NO_STAMP, 1):
        print("  %d. %s" % (i, item))
    if args.json:
        with open(args.json, "w") as fh:
            json.dump({"window": [lo.isoformat(), hi.isoformat()], "sections": SUMMARY}, fh, indent=1)
