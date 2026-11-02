#!/usr/bin/env python3
"""Solana general-path entry: where the time goes, per substage, from the funded traces (read-only).

Asked 2026-09-27 (Alpha, after docs/audits/2026-09-27-lemon-dougherty-s7.md): measured BEFORE
numbers for the three proposed changes on the J7 / on-chain -> Jupiter path: the oversize rebuild,
the serial pre-balance reads, and the market-data stage.

DENOMINATOR: the FIRST attempt per origin_event_id (a retry starts seconds later and would measure
the retry schedule), on network 'solana', since the timing substages exist (first
`timing_usdc_read` 2026-09-24T16:25:46Z), that reached the pre-balance reads, i.e. passed every
gate up to the build. Refusals before that point are not in it: this describes the path an entry
that is going to be sent takes, not every signal.

Stamps (memecoin_entry_attempts, all ours): observed_at (the lane's receive; NOT a receive stamp for
the slot lane, which is excluded here anyway), decision_started_at, the stage columns, and trace_json
submission_events (per build attempt) and substage_events (first occurrence unless stated).

    python3 scripts/research/entry_substage_baseline.py [--since ISO] [--json OUT]
    python3 scripts/research/entry_substage_baseline.py --selftest
"""
import argparse
import contextlib
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

SINCE = "2026-09-24T16:25:46"
SUBS = ("source_buy_floor", "dex_pair_selection", "token_safety", "timing_reverse_quote",
        "timing_usdc_read", "timing_token_read", "timing_swap_build", "timing_local_sign")


def ts(s):
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else None


def ms(a, b):
    a, b = ts(a), ts(b)
    return (b - a).total_seconds() * 1000.0 if a and b else None


def pct(xs, p):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def row_facts(r):
    tr = json.loads(r["trace_json"] or "{}")
    subs = tr.get("substage_events") or []
    first = {}
    for e in subs:
        first.setdefault(e.get("name"), e)
    sub_ev = tr.get("submission_events") or []
    quote1 = next((e for e in sub_ev if e.get("stage") == "quote" and e.get("attempt_no") == 1), {})
    sign = {e.get("attempt_no"): e.get("completed_at") for e in sub_ev if e.get("stage") == "signing"}
    over = first.get("oversize_retry")
    rebuild = first.get("oversize_retry_rebuild")
    rt = first.get("roundtrip_cap") or {}
    labels = [((leg or {}).get("swapInfo") or {}).get("label") for leg in (rt.get("buy_route_plan") or [])]
    f = {
        "lane": r["delivery_lane"] or r["signal_source"] or "?",
        "status": r["terminal_status"],
        "detect_to_start": ms(r["observed_at"], r["decision_started_at"]),
        "market_data": r["market_data_ms"],
        "reservation": r["reservation_ms"],
        "safety_checks": r["safety_checks_ms"],
        "forward_quote_1": quote1.get("duration_ms"),
        "balance_reads_1": None,
        "balance_reads_all": sum(float(e.get("duration_ms") or 0) for e in subs
                                 if e.get("name") in ("timing_usdc_read", "timing_token_read")),
        "start_to_submitted": ms(r["decision_started_at"], r["transaction_submitted_at"]),
        "detect_to_submitted": ms(r["observed_at"], r["transaction_submitted_at"]),
        "oversize": over is not None,
        "oversize_pre_bytes": (over or {}).get("pre_bytes"),
        "oversize_excluded": (over or {}).get("exclude_dexes"),
        "oversize_post_bytes": (rebuild or {}).get("post_bytes"),
        "oversize_added_ms": ms(sign.get(1), sign.get(2)) if over is not None else None,
        "route_labels": labels,
        "hops": len(labels) or None,
    }
    for name in SUBS:
        f[name] = (first.get(name) or {}).get("duration_ms")
    if f["timing_usdc_read"] is not None and f["timing_token_read"] is not None:
        # Serial reads (before 827b78e7) cost the sum. Concurrent reads (from 827b78e7, deployed
        # 2026-09-27T12:27:51Z) complete together and cost the longer one: summing them there
        # reported ~170 ms for a ~112 ms wall.
        gap = ms((first.get("timing_usdc_read") or {}).get("completed_at"),
                 (first.get("timing_token_read") or {}).get("completed_at"))
        together = gap is not None and abs(gap) < min(f["timing_usdc_read"], f["timing_token_read"])
        f["balance_reads_1"] = (max if together else sum)((f["timing_usdc_read"],
                                                           f["timing_token_read"]))
    return f


def load(since):
    from sniper.persistence.db_core import get_readonly_connection
    with contextlib.closing(get_readonly_connection()) as c:
        c.row_factory = __import__("sqlite3").Row
        rows = c.execute(
            "SELECT attempt_id, origin_event_id, delivery_lane, signal_source, observed_at, "
            "decision_started_at, market_data_ms, reservation_ms, safety_checks_ms, "
            "transaction_submitted_at, terminal_status, trace_json FROM memecoin_entry_attempts "
            "WHERE network_id='solana' AND decision_started_at >= ? "
            "ORDER BY decision_started_at", (since,)).fetchall()
    firsts = {}
    for r in rows:
        firsts.setdefault(r["origin_event_id"] or r["attempt_id"], r)
    return [row_facts(r) for r in firsts.values()
            if r["trace_json"] and "timing_usdc_read" in r["trace_json"]]


KEYS = ("detect_to_start", "market_data", "dex_pair_selection", "source_buy_floor", "reservation",
        "safety_checks", "token_safety", "forward_quote_1", "timing_reverse_quote",
        "timing_usdc_read", "timing_token_read", "balance_reads_1", "balance_reads_all",
        "timing_swap_build", "timing_local_sign", "start_to_submitted", "detect_to_submitted")


def summarize(facts):
    out = {"n_first_attempts": len(facts), "lanes": {}}
    groups = {"ALL": facts}
    for f in facts:
        groups.setdefault(f["lane"], []).append(f)
    for lane, fs in groups.items():
        g = {"n": len(fs)}
        for k in KEYS:
            xs = [f[k] for f in fs if f[k] is not None]
            g[k] = {"n": len(xs), "p50": pct(xs, 0.5), "p90": pct(xs, 0.9)}
        over = [f for f in fs if f["oversize"]]
        g["oversize"] = {"n": len(over), "share": len(over) / len(fs) if fs else None,
                         "added_ms_p50": pct([f["oversize_added_ms"] for f in over], 0.5),
                         "added_ms_p90": pct([f["oversize_added_ms"] for f in over], 0.9),
                         "cases": [{k: f[k] for k in ("oversize_pre_bytes", "oversize_post_bytes",
                                                      "oversize_excluded", "route_labels",
                                                      "oversize_added_ms")} for f in over]}
        routed = [f for f in fs if f["route_labels"]]
        hum = [f for f in routed if any("humidifi" in str(x).lower() for x in f["route_labels"])]
        g["routes"] = {"with_plan": len(routed), "humidifi_any_hop": len(hum),
                       "humidifi_oversize": sum(f["oversize"] for f in hum),
                       "hops": {h: sum(f["hops"] == h for f in routed)
                                for h in sorted({f["hops"] for f in routed})}}
        out["lanes"][lane] = g
    return out


def selftest():
    assert pct([1, 2, 3, 4], 0.5) == 2.5 and pct([], 0.5) is None
    tr = {"submission_events": [
        {"stage": "quote", "attempt_no": 1, "duration_ms": 100.0},
        {"stage": "signing", "attempt_no": 1, "completed_at": "2026-09-27T00:00:01+00:00"},
        {"stage": "signing", "attempt_no": 2, "completed_at": "2026-09-27T00:00:01.334+00:00"}],
        "substage_events": [{"name": "timing_usdc_read", "duration_ms": 20.0},
                            {"name": "timing_token_read", "duration_ms": 150.0},
                            {"name": "oversize_retry", "pre_bytes": 1365, "exclude_dexes": ["HumidiFi"]},
                            {"name": "timing_usdc_read", "duration_ms": 25.0},
                            {"name": "timing_token_read", "duration_ms": 160.0}]}
    r = {"trace_json": json.dumps(tr), "delivery_lane": "j7_wallets", "signal_source": None,
         "terminal_status": "filled", "observed_at": "2026-09-27T00:00:00+00:00",
         "decision_started_at": "2026-09-27T00:00:00.004+00:00", "market_data_ms": 500.0,
         "reservation_ms": 50.0, "safety_checks_ms": 20.0,
         "transaction_submitted_at": "2026-09-27T00:00:01.400+00:00"}
    f = row_facts(r)
    assert f["balance_reads_1"] == 170.0 and f["balance_reads_all"] == 355.0, f
    both = [{"name": "timing_usdc_read", "duration_ms": 60.0, "completed_at": "2026-09-27T00:00:00.1+00:00"},
            {"name": "timing_token_read", "duration_ms": 110.0,
             "completed_at": "2026-09-27T00:00:00.100040+00:00"}]
    r2 = dict(r, trace_json=json.dumps({"substage_events": both}))
    assert row_facts(r2)["balance_reads_1"] == 110.0, row_facts(r2)
    assert f["oversize"] and round(f["oversize_added_ms"]) == 334 and f["forward_quote_1"] == 100.0
    assert round(f["detect_to_submitted"]) == 1400
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=SINCE)
    ap.add_argument("--json")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    s = summarize(load(a.since))
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(s, fh, indent=1, default=str)
    print("first attempts reaching the pre-balance reads: n=%d since %s" % (s["n_first_attempts"], a.since))
    for lane, g in s["lanes"].items():
        print("\n== %s (n=%d)" % (lane, g["n"]))
        for k in KEYS:
            v = g[k]
            if v["n"]:
                print("  %-22s n=%-4d p50=%8.1f  p90=%8.1f ms" % (k, v["n"], v["p50"], v["p90"]))
        o = g["oversize"]
        print("  oversize: %d (%.1f%%) added p50=%s p90=%s" % (
            o["n"], 100 * (o["share"] or 0), o["added_ms_p50"], o["added_ms_p90"]))
        print("  routes: %s" % g["routes"])


if __name__ == "__main__":
    main()
