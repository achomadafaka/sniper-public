#!/usr/bin/env python3
"""Solana general-path entry: the critical-path ORDER of the substages, and which ones overlap.

Asked 2026-09-27 (Alpha): are dex_pair_selection, volume_gate, token_safety, quote_fallback and
timing_token_read sequential or overlapped on the critical path?

HOW A SUBSTAGE'S INTERVAL IS KNOWN (read from sniper/services/memecoin_entry_trace.py
mark_substage before any number here was trusted). Every event carries `elapsed_ms` = the instant
the mark was WRITTEN, measured from the attempt's start. `duration_ms` is either
  gap       the time since the PREVIOUS mark (the default), so start = the previous mark by
            construction. A gap mark can never show an overlap: its interval is the thread's
            wall time between two marks, whatever ran in it. Only the code says what that was.
  explicit  a caller-measured duration. start = elapsed - duration is right only if the mark is
            written when the work ends.
  join      timing_usdc_read / timing_token_read / timing_balance_reads / timing_balance_wait are
            written at the JOIN of the pre-balance reads (solana_swap_executor._PreBalanceReads,
            _read_pre_balances), so elapsed - duration is NOT their start. Since 0df2e7ee (runner
            2026-09-27T17:00:05Z) the reads start beside the forward quote and timing_balance_wait
            is what the entry actually blocked on.
A mark counts as gap when |duration - (elapsed - previous elapsed)| < 0.5 ms.

Code order on the decision thread (memecoin_real_runner / memecoin_real_leg / solana executor):
  market data fetch  -> dex_pair_selection (gap; ~0 when classification_context carried the stats)
  volume inputs      -> [onchain_volume_m5] [solana_hourly_floor_evidence] -> volume_gate (gap)
  reserve_entry      -> timing_gate_balance_read (explicit) -> reservation
  token_safety       (gap from the last mark: rest of reservation + the safety call)
  execute_fn         wallet lock, pre-reads started (L2) -> forward quote -> quote_fallback (gap;
                     only when the quote carries a fallback tag) -> timing_reverse_quote ...
                     -> join -> timing_*_read marks

DENOMINATOR: the FIRST attempt per origin_event_id on network 'solana' since --since, excluding the
slot lane (its own builder) and manual entries. A substage's n counts only attempts that reached it.

Ledger read-only (read-only URI). No network.
    python3 scripts/research/entry_critical_path.py [--since 2026-09-24T00:00:00]
    python3 scripts/research/entry_critical_path.py --selftest
"""
import argparse
import contextlib
import json
import sqlite3
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
JOIN_MARKS = {"timing_usdc_read", "timing_token_read", "timing_balance_reads", "timing_balance_wait"}
# Deploys that changed the pre-balance reads (runner start, UTC).
PAIR_CONCURRENT_AT = "2026-09-27T12:27:51"   # 827b78e7: usdc || token, still after the gates
OVERLAP_QUOTE_AT = "2026-09-27T17:00:05"     # 0df2e7ee: reads start beside the forward quote
ASKED = ("dex_pair_selection", "volume_gate", "token_safety", "quote_fallback", "timing_token_read")


def pct(xs, p):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def timeline(events):
    """[(name, start_ms, end_ms, duration_ms, kind, previous_name)], FIRST occurrence per name.
    start_ms is None for a join mark: elapsed - duration is not when that read began."""
    out, seen, prev_elapsed, prev_name = [], set(), 0.0, None
    for e in events:
        name, end = e.get("name"), e.get("elapsed_ms")
        dur = e.get("duration_ms")
        if name is None or end is None or dur is None:
            continue
        end, dur = float(end), float(dur)
        kind = ("join" if name in JOIN_MARKS
                else "gap" if abs(dur - (end - prev_elapsed)) < 0.5 else "explicit")
        if name not in seen:
            seen.add(name)
            out.append((name, None if kind == "join" else end - dur, end, dur, kind, prev_name))
        prev_elapsed, prev_name = end, name
    return out


def regime(decision_started_at):
    s = str(decision_started_at)
    return ("overlap_quote" if s >= OVERLAP_QUOTE_AT
            else "pair_concurrent" if s >= PAIR_CONCURRENT_AT else "serial")


def load(since):
    with contextlib.closing(sqlite3.connect("file:%s?mode=ro" % (REPO / "sniper_tracker.db"),
                                            uri=True, timeout=10)) as c:
        rows = c.execute(
            "SELECT attempt_id, origin_event_id, delivery_lane, signal_source, decision_started_at, "
            "trace_json FROM memecoin_entry_attempts WHERE decision_started_at >= ? "
            "AND network_id='solana' ORDER BY decision_started_at", (since,)).fetchall()
    firsts = {}
    for r in rows:
        firsts.setdefault(r[1] or r[0], r)
    out = []
    for r in firsts.values():
        if r[2] == "slot_lane_processed" or r[3] in ("slot_lane", "manual") or not r[5]:
            continue
        tr = json.loads(r[5])
        out.append({"lane": r[2] or r[3] or "?", "started": r[4], "regime": regime(r[4]),
                    "tl": timeline(tr.get("substage_events") or [])})
    return out


def report(facts):
    by = {}
    for f in facts:
        for name, start, end, dur, kind, prev in f["tl"]:
            by.setdefault(name, []).append((start, end, kind, prev, dur))
    order = sorted(by, key=lambda n: pct([x[1] for x in by[n]], 0.5))
    print("first attempts: n=%d (general path, solana)" % len(facts))
    print("%-34s %5s %9s %9s %9s %9s %s" % ("substage (median mark order)", "n", "dur p50", "dur p90",
                                            "end p50", "start p50", "kind / top previous mark"))
    for name in order:
        xs = by[name]
        durs = [x[4] for x in xs]
        kinds = {}
        prevs = {}
        for x in xs:
            kinds[x[2]] = kinds.get(x[2], 0) + 1
            prevs[x[3]] = prevs.get(x[3], 0) + 1
        top_prev = max(prevs.items(), key=lambda kv: kv[1])
        print("%-34s %5d %9s %9s %9.1f %9s %s / %s (%d)" % (
            ("*" if name in ASKED else " ") + name, len(xs),
            "%.1f" % pct(durs, 0.5) if durs else "-", "%.1f" % pct(durs, 0.9) if durs else "-",
            pct([x[1] for x in xs], 0.5),
            "%.1f" % pct([x[0] for x in xs if x[0] is not None], 0.5)
            if any(x[0] is not None for x in xs) else "join",
            ",".join("%s=%d" % kv for kv in sorted(kinds.items())), top_prev[0], top_prev[1]))
    return by


def join_reads(facts):
    """The balance reads by regime: each read's own wall, the pair's wall, and the entry's block."""
    print("\npre-balance reads by regime (join marks; the durations are the reads' own walls):")
    for reg in ("serial", "pair_concurrent", "overlap_quote"):
        fs = [f for f in facts if f["regime"] == reg]
        vals = {}
        for f in fs:
            d = {e[0]: e for e in f["tl"]}
            if "timing_token_read" not in d:
                continue
            for name in ("timing_usdc_read", "timing_token_read", "timing_balance_reads",
                         "timing_balance_wait"):
                if name in d:
                    vals.setdefault(name, []).append(d[name][3])
        print("  %-16s n=%d  %s" % (reg, len(vals.get("timing_token_read", [])), "  ".join(
            "%s p50 %.1f p90 %.1f" % (k.replace("timing_", ""), pct(v, 0.5), pct(v, 0.9))
            for k, v in vals.items())))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-09-24T00:00:00")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    facts = load(a.since)
    report(facts)
    join_reads(facts)


def selftest():
    events = [
        {"name": "source_buy_floor", "elapsed_ms": 18.0, "duration_ms": 18.0},
        {"name": "dex_pair_selection", "elapsed_ms": 262.0, "duration_ms": 244.0},
        {"name": "volume_gate", "elapsed_ms": 265.0, "duration_ms": 3.0},
        {"name": "timing_gate_balance_read", "elapsed_ms": 361.0, "duration_ms": 95.0},
        {"name": "token_safety", "elapsed_ms": 398.0, "duration_ms": 37.0},
        {"name": "quote_fallback", "elapsed_ms": 853.0, "duration_ms": 455.0},
        {"name": "timing_token_read", "elapsed_ms": 875.0, "duration_ms": 107.0},
        {"name": "token_safety", "elapsed_ms": 900.0, "duration_ms": 25.0},     # a retry: ignored
    ]
    tl = {t[0]: t for t in timeline(events)}
    assert tl["dex_pair_selection"][1:5] == (18.0, 262.0, 244.0, "gap"), tl["dex_pair_selection"]
    assert tl["timing_gate_balance_read"][1:5] == (266.0, 361.0, 95.0, "explicit")
    assert tl["token_safety"][1:6] == (361.0, 398.0, 37.0, "gap", "timing_gate_balance_read")
    # A join mark has no start: elapsed - duration (768) is not when the read began.
    assert tl["timing_token_read"][1] is None and tl["timing_token_read"][3:5] == (107.0, "join")
    assert regime("2026-09-27T17:00:05.1+00:00") == "overlap_quote"
    assert regime("2026-09-27T12:30:00+00:00") == "pair_concurrent"
    assert regime("2026-09-26T12:30:00+00:00") == "serial"
    print("selftest ok")


if __name__ == "__main__":
    main()
