"""Where the time goes from source buy to accepted send, per lane and chain, on funded fills.

Alpha, 2026-09-28 (operator target: 0.2 s from the source's buy to our quote). Stages, each on ONE
clock, method named per row:
  (a) source execution -> our receipt   NOT here: needs a slot clock (see --help of the caller).
  (b) receipt -> decision start          attempts.observed_at -> decision_started_at. Both corrected
      clock ("observed_real_utc"); only where observed_at precedes the decision (J7). The watcher and
      slot lane write observed_at AFTER the decision, so their (b) is not measured here.
  (c) decision -> quote in hand          decision_started_at -> quote_completed_at (trace: anchor +
      monotonic elapsed, one clock). The slot lane has no quote stage (prebuilt), reported as '-'.
  (d) quote -> signed                    quote_completed_at -> signing_completed_at (trace).
  (e) signed -> accepted                 signing_completed_at -> transaction_submitted_at (trace; the
      mark is written when the send call RETURNS accepted, solana_swap_executor.py:1203,
      solana_slot_executor.py:135, evm_swap_executor.py:1161).
  plus decision -> signed, and inside (c) for trace-marked lanes: the time to the first gate mark
  (the market-data read and pair selection before any gate), quote_fallback and timing_reverse_quote.

Lanes: J7 = signal_source follow_activity; watcher = onchain_watch whose detection basis is not the
slot lane's; routed = onchain_watch whose detection basis is slot_lane_routed; slot lane =
signal_source slot_lane. Fills only (terminal_status 'filled'), so every stage exists.

    python3 scripts/research/stage_split.py [--since ISO]
    python3 scripts/research/stage_split.py --selftest
"""
import argparse
import collections
import contextlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

LEDGER = Path(__file__).resolve().parents[2] / "sniper_tracker.db"
STAGES = ["(b) receipt->decision", "(c) decision->quote", "(d) quote->signed", "(e) signed->accepted",
          "decision->signed", "  c: to first gate", "  c: quote_fallback", "  c: reverse_quote"]


def ts(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")) if s else None


def secs(a, b):
    a, b = ts(a), ts(b)
    return (b - a).total_seconds() if a and b else None


def lane_of(signal_source, basis):
    if signal_source == "follow_activity":
        return "J7"
    if signal_source == "slot_lane":
        return "slot lane"
    if signal_source == "onchain_watch":
        return "routed" if basis == "slot_lane_routed" else "watcher"
    return signal_source or "?"


def substage(trace, name, field):
    for e in (trace.get("substage_events") or []):
        if e.get("name") == name:
            v = e.get(field)
            return v / 1000.0 if isinstance(v, (int, float)) else None
    return None


def first_gate(trace):
    subs = [e for e in (trace.get("substage_events") or []) if isinstance(e.get("elapsed_ms"), (int, float))]
    return subs[0]["elapsed_ms"] / 1000.0 if subs else None


def row_stages(r, trace):
    return {
        "(b) receipt->decision": (secs(r["observed_at"], r["decision_started_at"])
                                  if r["observed_at"] and r["observed_at"] <= r["decision_started_at"] else None),
        "(c) decision->quote": secs(r["decision_started_at"], r["quote_completed_at"]),
        "(d) quote->signed": secs(r["quote_completed_at"], r["signing_completed_at"]),
        "(e) signed->accepted": secs(r["signing_completed_at"], r["transaction_submitted_at"]),
        "decision->signed": secs(r["decision_started_at"], r["signing_completed_at"]),
        "  c: to first gate": first_gate(trace),
        "  c: quote_fallback": substage(trace, "quote_fallback", "duration_ms"),
        "  c: reverse_quote": substage(trace, "timing_reverse_quote", "duration_ms"),
    }


def pct(xs, p):
    xs = sorted(x for x in xs if x is not None)
    return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    since = a.since or (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    groups = collections.defaultdict(lambda: collections.defaultdict(list))
    n = collections.Counter()
    with contextlib.closing(sqlite3.connect("file:%s?mode=ro" % LEDGER, uri=True, timeout=20)) as c:
        c.row_factory = sqlite3.Row
        basis = {}
        for sig, v in c.execute("SELECT signature, source_valuation_json FROM onchain_wallet_detections "
                                "WHERE detected_at >= ?", (since,)):
            try:
                basis["onchain:" + sig] = (json.loads(v or "{}") or {}).get("basis")
            except ValueError:
                pass
        for r in c.execute("SELECT network_id, signal_source, origin_event_id, observed_at, decision_started_at, "
                           "quote_completed_at, signing_completed_at, transaction_submitted_at, trace_json "
                           "FROM memecoin_entry_attempts WHERE terminal_status='filled' AND decision_started_at >= ?",
                           (since,)):
            lane = lane_of(r["signal_source"], basis.get(r["origin_event_id"] or ""))
            key = (lane, r["network_id"])
            n[key] += 1
            for k, v in row_stages(r, json.loads(r["trace_json"] or "{}")).items():
                groups[key][k].append(v)
    print("funded fills since %s, seconds p50 / p90 (n with the stage)" % since[:19])
    for key in sorted(groups, key=lambda k: -n[k]):
        print("\n%s / %s  fills=%d" % (key[0], key[1], n[key]))
        for s in STAGES:
            xs = [x for x in groups[key][s] if x is not None]
            if xs:
                print("  %-24s %7.3f / %7.3f  (n=%d)" % (s, pct(xs, .5), pct(xs, .9), len(xs)))
            else:
                print("  %-24s       - (no stamp)" % s)


def selftest():
    assert lane_of("follow_activity", None) == "J7" and lane_of("onchain_watch", "slot_lane_routed") == "routed"
    assert lane_of("onchain_watch", "route_owner_quote_out") == "watcher" and lane_of("slot_lane", None) == "slot lane"
    r = {"observed_at": "2026-09-28T02:02:46.510703+00:00", "decision_started_at": "2026-09-28T02:02:46.514739+00:00",
         "quote_completed_at": "2026-09-28T02:02:47.036655+00:00", "signing_completed_at": "2026-09-28T02:02:47.420537+00:00",
         "transaction_submitted_at": "2026-09-28T02:02:47.455257+00:00"}
    tr = {"substage_events": [{"name": "source_buy_floor", "elapsed_ms": 427, "duration_ms": 427},
                              {"name": "quote_fallback", "elapsed_ms": 522, "duration_ms": 91},
                              {"name": "timing_reverse_quote", "elapsed_ms": 873, "duration_ms": 351}]}
    s = row_stages(r, tr)
    assert abs(s["(b) receipt->decision"] - 0.004036) < 1e-6 and abs(s["(c) decision->quote"] - 0.521916) < 1e-6
    assert abs(s["(e) signed->accepted"] - 0.03472) < 1e-6 and s["  c: to first gate"] == 0.427
    assert s["  c: reverse_quote"] == 0.351 and s["  c: quote_fallback"] == 0.091
    late = dict(r, observed_at="2026-09-28T02:02:46.600000+00:00")          # written after the decision
    assert row_stages(late, tr)["(b) receipt->decision"] is None
    assert pct([3, 1, 2, None], .5) == 2
    print("selftest ok")


if __name__ == "__main__":
    main()
