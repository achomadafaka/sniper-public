#!/usr/bin/env python3
"""Decompose the b_stop overshoot into observation, debounce, quote and fill. READ ONLY.

Question: the stop trigger is -35%, the realised level is worse. Where does the difference go?

THE FOUR STAGES, all in percentage points of the entry price (unit space, so a partial exit cannot
distort them), each measured from a row that the runner itself wrote:

    (0) observe   first read of the CONFIRMING RUN          minus  -35%
    (a) debounce  the confirming read                      minus  the first read of that run
    (b) re-quote  the execution witness quote              minus  the confirming read
    (c) fill      the realised sell price                  minus  the execution witness quote

    overshoot = realised - (-35%) = (0) + (a) + (b) + (c)

THE CONFIRMING RUN, not the first dip. The runner resets its confirmation streak whenever a read
comes back above the trigger, so only the FINAL uninterrupted run below the trigger produces the
exit. Attributing from the earliest breach in the whole series folds a dip that recovered hours
earlier into (0) and (a); on the review's fixture that reports a 100 s debounce where the real one
is 1 s. Reads that recovered before the final run are counted separately as `recovered_dips`.
A read with no usable price is neither: it cannot confirm a stop and it cannot clear one, so it
breaks the run and is counted as `unknown_reads`.

SOURCES, and the writer of each one:

    memecoin_route_series (seam='exit_decision')   `_keep_route_quote` in memecoin_real_runner.
        One row per exact whole-bag quote the exit sweep took, with open_qty and unit_price.
        THIS IS THE ONLY ROUTE-SPACE PRICE SERIES IN THE SYSTEM. It is not a mid.
    memecoin_exit_trigger_audit reason='b_stop'    `executable_exit_allowed` -> record_exit_trigger_audit.
        detail.executable_unit_pnl_pct on code='threshold_confirmed' is the level the rail decided on.
    memecoin_exit_trigger_audit reason='b_stop_execution'  `confirm_execution_stop`, lane='execution'.
        The SECOND witness: the execution path's own re-quote of the whole bag before it signs.
    memecoin_fills kind='bracket_exit'             the realised sell.
    memecoin_positions.exit_timing_json            solana stage map.
    memecoin_exit_execution_attempts.stage_ms_json EVM stage map.

WHY NOT memecoin_position_marks. `executable_exit_value` there is an ALIAS FOR `mid` on every row
(memecoin_real_runner._keep_route_quote says so in its own docstring), so a mark cannot answer an
execution question. Comparing a fill to a mark measures mid-versus-executable, which is spread and
impact, not latency. This script reports that gap separately rather than mixing it in.

THREE CENSORING FACTS, stated as measurements rather than caveats:
  * memecoin_route_series starts 2026-08-26T13:08Z, so positions closed before it have no stage (0)/(a).
  * reason='b_stop_execution' was only written from 2026-09-11T21:55Z (Gamma's D6 fix), so stages
    (b) and (c) separate only after that date. Before it, (b)+(c) are reported as one number.
    The missingness is a CODE DATE, not our trading behaviour, so it does not enrich the sample the
    way a "coins we held" denominator would - but it does bias the split window toward recent policy.
  * the audit table upserts on (position_id, reason, checked_minute), so a `debounce_pending` row is
    OVERWRITTEN by the `threshold_confirmed` row from the same minute. The debounce is therefore
    measured from the route series, never from the audit's pending rows.

Usage:
    python3 scripts/research/stop_overshoot_decomposition.py --days 30
    python3 scripts/research/stop_overshoot_decomposition.py --selftest
"""
import argparse
import datetime as dt
import json
import os
import sqlite3
import statistics as st
import sys
from pathlib import Path

REPO = Path("/home/bigpapi/Desktop/Sniper")
sys.path.insert(0, str(REPO))

TRIGGER_PCT = -35.0          # MEMECOIN_FOLLOW3050_STOP_PCT=35; verified empirically below.
ROUTE_SERIES_START = "2026-08-26T13:08"
EXEC_WITNESS_START = "2026-09-11T21:55"


def iso(stamp):
    """ISO text -> aware datetime. Never compare these against a SQLite datetime() rendering:
    that writes a SPACE where our stamps carry 'T', and 'T' > ' ', so such a bound is always false."""
    try:
        return dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def pct_of(price, entry):
    if not price or not entry or price <= 0 or entry <= 0:
        return None
    return (price / entry - 1.0) * 100.0


def quantiles(values):
    """(n, p50, p90) where p90 is the 90th percentile of the MAGNITUDE-ordered list as given."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return 0, None, None
    p90 = vals[min(len(vals) - 1, int(round(0.9 * (len(vals) - 1))))]
    return len(vals), st.median(vals), p90


def _load_json(raw):
    try:
        return json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}


def stage_map(pos_row, attempt_row):
    """Normalise the two stage-map shapes into one dict. Returns (stages, source)."""
    if attempt_row and attempt_row["stage_ms_json"]:
        d = _load_json(attempt_row["stage_ms_json"])
        return ({"quote_ms": d.get("quote_ms"), "quote_n": d.get("quote_n"),
                 "approve_ms": d.get("approve_ms"), "sign_ms": d.get("sign_ms"),
                 "send_ms": d.get("send_ms"), "confirm_ms": d.get("confirm_ms"),
                 "total_ms": attempt_row["total_ms"]}, "exit_execution_attempts")
    d = _load_json(pos_row["exit_timing_json"])
    s = d.get("stages_ms") or {}
    if not s:
        return ({}, None)
    return ({"quote_ms": s.get("quote"), "quote_n": d.get("ladder_rungs"),
             "approve_ms": s.get("approve"), "sign_ms": s.get("sign"),
             "send_ms": s.get("submit") if s.get("submit") is not None else s.get("send"),
             "confirm_ms": s.get("confirm"), "total_ms": d.get("total_ms")}, "exit_timing_json")


def confirming_run_start(levels):
    """Index of the first read of the FINAL uninterrupted run at or below the trigger.

    `levels` are percentage points of entry price, None where the price was unusable. The runner
    resets its confirmation streak on any read that is not a confirmed breach, so a recovery AND
    an unusable read both start the run over. Returns None when nothing breached.
    """
    start = None
    for i, lv in enumerate(levels):
        if lv is not None and lv <= TRIGGER_PCT:
            if start is None or not (i and levels[i - 1] is not None
                                     and levels[i - 1] <= TRIGGER_PCT):
                start = i
    return start


def build(days):
    from dotenv import load_dotenv
    load_dotenv(REPO / ".env")
    from sniper.util.state_paths import state_path
    from sniper.util.clock import real_utc_now
    db = os.getenv("DB_PATH") or str(state_path("sniper_tracker.db"))
    conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    now = dt.datetime.fromtimestamp(real_utc_now(), dt.timezone.utc)
    cut = (now - dt.timedelta(days=days)).isoformat()

    positions = conn.execute(
        "SELECT id, ticker, network_id, entry_price, entry_notional, realized_pnl, "
        "opened_recorded_at, opened_at, closed_at, closed_recorded_at, exit_timing_json, "
        "exit_intent_json, source_handle "
        "FROM memecoin_positions WHERE execution_mode='real' AND is_open=0 "
        "AND exit_reason='b_stop' AND closed_at>=? ORDER BY closed_at", (cut,)).fetchall()
    if not positions:
        return [], now, cut
    ids = [r["id"] for r in positions]
    qm = ",".join("?" * len(ids))

    # One batched read per table, keyed on the position id. No correlated subquery touches the
    # 4.2M-row mark table or the tick stores.
    audit = {}
    for r in conn.execute(
            "SELECT position_id, reason, lane, allowed, preview_usdc, display_price, detail, "
            f"checked_at FROM memecoin_exit_trigger_audit WHERE position_id IN ({qm}) "
            "AND reason IN ('b_stop','b_stop_execution') ORDER BY checked_at", tuple(ids)):
        audit.setdefault(r["position_id"], []).append(r)
    series = {}
    for r in conn.execute(
            "SELECT position_id, id, observed_at, expected_usdc, open_qty, unit_price "
            f"FROM memecoin_route_series WHERE position_id IN ({qm}) AND seam='exit_decision' "
            # ORDER BY id, NEVER observed_at. On 2026-09-09 the corrected clock stepped BACKWARD
            # 562 times, so a stamp order reorders that day's series. `id` is the write order.
            # scripts/research/stop_overshoot_attribution.py found this first; see the doc's
            # section 11. The stamps are still used for the deltas, so that day's SECONDS stay
            # suspect: 3 of 198 gaps read below one quote round trip and all 3 are 2026-09-09.
            "ORDER BY id", tuple(ids)):
        series.setdefault(r["position_id"], []).append(r)
    fills = {}
    for r in conn.execute(
            "SELECT position_id, kind, price, qty, notional, created_at "
            f"FROM memecoin_fills WHERE position_id IN ({qm}) ORDER BY created_at", tuple(ids)):
        fills.setdefault(r["position_id"], []).append(r)
    attempts = {}
    for r in conn.execute(
            "SELECT position_id, status, tier_variant, depth_first_rung_pct, total_ms, "
            f"stage_ms_json, failure_type, attempted_at FROM memecoin_exit_execution_attempts "
            f"WHERE position_id IN ({qm}) AND exit_reason='b_stop' ORDER BY attempted_at", tuple(ids)):
        attempts.setdefault(r["position_id"], []).append(r)

    rows = []
    for p in positions:
        pid, entry = p["id"], p["entry_price"]
        aud = audit.get(pid, [])
        decision = exec_witness = None
        for a in aud:
            d = _load_json(a["detail"])
            if d.get("code") != "threshold_confirmed":
                continue
            if a["reason"] == "b_stop" and decision is None:
                decision = (a, d)
            elif a["reason"] == "b_stop_execution" and exec_witness is None:
                exec_witness = (a, d)
        if decision is None:
            continue
        dec_row, dec_detail = decision
        dec_pct = dec_detail.get("executable_unit_pnl_pct")
        dec_at = dec_row["checked_at"]
        mid_pct = pct_of(dec_row["display_price"], entry)

        sell = [f for f in fills.get(pid, []) if f["kind"] != "entry"]
        sell = sell[-1] if sell else None
        real_pct = pct_of(sell["price"], entry) if sell else None

        # (0) and (a): the first read of the run that actually confirmed, from the rail's series.
        upto = [r for r in series.get(pid, []) if r["observed_at"] <= dec_at]
        # Classify every read. `pct_of` returns None for a missing or zero price, and the previous
        # `or 0` scored that as a comfortable clean read; an unusable price is unknown, not 0%.
        levels = [pct_of(r["unit_price"], entry) for r in upto]
        breach_idx = [i for i, lv in enumerate(levels) if lv is not None and lv <= TRIGGER_PCT]
        unknown_reads = sum(1 for lv in levels if lv is None)
        first_i = confirming_run_start(levels)
        first_breach = upto[first_i] if first_i is not None else None
        fb_pct = levels[first_i] if first_i is not None else None
        # confirming reads before the decision: the rest of that run
        prior_breaches = 0 if first_i is None else sum(1 for i in breach_idx if i > first_i)
        # An earlier run counts as a RECOVERY only if a priced read above the trigger ended it.
        # A run that an unusable read cut short is not evidence that the coin came back.
        recovered_dips = 0
        for i in (breach_idx if first_i is None else [i for i in breach_idx if i < first_i]):
            nxt = levels[i + 1] if i + 1 < len(levels) else None
            if nxt is not None and nxt > TRIGGER_PCT:
                recovered_dips += 1
        # The last read before the confirming run, and the wall time to it. This is what decides
        # whether a faster read cadence could have caught the crossing nearer the trigger.
        prev_pct = read_gap_s = None
        if first_i:
            prev = upto[first_i - 1]
            prev_pct = levels[first_i - 1]
            a, b = iso(prev["observed_at"]), iso(first_breach["observed_at"])
            read_gap_s = (b - a).total_seconds() if (a and b) else None
        debounce_s = None
        if first_breach:
            a, b = iso(first_breach["observed_at"]), iso(dec_at)
            debounce_s = (b - a).total_seconds() if (a and b) else None

        exec_pct = exec_witness[1].get("executable_unit_pnl_pct") if exec_witness else None
        exec_at = exec_witness[0]["checked_at"] if exec_witness else None

        att = [a for a in attempts.get(pid, []) if a["status"] == "closed"]
        stages, stage_src = stage_map(p, att[-1] if att else None)

        rows.append({
            "position_id": pid, "ticker": p["ticker"], "chain": p["network_id"],
            "handle": p["source_handle"], "closed_at": p["closed_at"],
            "entry_price": entry, "entry_notional": p["entry_notional"],
            "realized_pnl": p["realized_pnl"],
            "decision_at": dec_at, "decision_pct": dec_pct, "mid_pct_at_decision": mid_pct,
            "first_breach_at": first_breach["observed_at"] if first_breach else None,
            "first_breach_pct": fb_pct, "prior_breach_reads": prior_breaches,
            "recovered_dips": recovered_dips, "unknown_reads": unknown_reads,
            "prev_read_pct": prev_pct, "read_gap_s": read_gap_s, "debounce_s": debounce_s,
            "series_reads": len(series.get(pid, [])),
            "exec_quote_at": exec_at, "exec_quote_pct": exec_pct,
            "fill_at": sell["created_at"] if sell else None, "realised_pct": real_pct,
            "g_observe": (fb_pct - TRIGGER_PCT) if fb_pct is not None else None,
            "g_debounce": (dec_pct - fb_pct) if (fb_pct is not None and dec_pct is not None) else None,
            "g_requote": (exec_pct - dec_pct) if (exec_pct is not None and dec_pct is not None) else None,
            "g_fill": (real_pct - exec_pct) if (exec_pct is not None and real_pct is not None) else None,
            "g_decision_to_fill": (real_pct - dec_pct) if (real_pct is not None and dec_pct is not None) else None,
            "overshoot": (real_pct - TRIGGER_PCT) if real_pct is not None else None,
            "mid_minus_executable": (mid_pct - dec_pct) if (mid_pct is not None and dec_pct is not None) else None,
            "stage_source": stage_src, "attempts_n": len(attempts.get(pid, [])),
            **{k: stages.get(k) for k in ("quote_ms", "quote_n", "approve_ms", "sign_ms",
                                          "send_ms", "confirm_ms", "total_ms")},
        })
    conn.close()
    return rows, now, cut


def report(rows, now, cut, days):
    chains = sorted({r["chain"] for r in rows})
    out = []
    w = out.append
    w("window %s .. %s (%d days), n=%d b_stop closes" % (cut[:19], now.isoformat()[:19], days, len(rows)))
    w("trigger %.1f%% -- least-negative confirmed level per chain (an empirical check on the knob):" % TRIGGER_PCT)
    for ch in chains:
        lv = [r["decision_pct"] for r in rows if r["chain"] == ch and r["decision_pct"] is not None]
        w("  %-8s n=%-4d max=%.2f%%  min=%.2f%%  above trigger=%d"
          % (ch, len(lv), max(lv), min(lv), sum(1 for x in lv if x > TRIGGER_PCT)))
    w("")
    w("%-8s %-22s %5s %9s %9s %12s" % ("chain", "stage", "n", "p50 pp", "p90 pp", "sum $"))
    for ch in chains + ["ALL"]:
        sub = rows if ch == "ALL" else [r for r in rows if r["chain"] == ch]
        for key, label in (("g_observe", "(0) observe"), ("g_debounce", "(a) debounce"),
                           ("g_requote", "(b) re-quote"), ("g_fill", "(c) fill"),
                           ("g_decision_to_fill", "(b+c) decision->fill"),
                           ("overshoot", "TOTAL overshoot"),
                           ("mid_minus_executable", "[mid - executable]")):
            vals = [r[key] for r in sub if r[key] is not None]
            n, p50, p90 = quantiles(vals)
            if not n:
                continue
            usd = sum(r[key] / 100.0 * (r["entry_notional"] or 0) for r in sub if r[key] is not None)
            w("%-8s %-22s %5d %9.2f %9.2f %12.2f" % (ch, label, n, p50, p90, usd))
        w("")
    w("stage maps (ms), p50 / p90:")
    for ch in chains:
        sub = [r for r in rows if r["chain"] == ch]
        line = ["  %-8s n=%-4d" % (ch, len(sub))]
        for k in ("quote_ms", "approve_ms", "sign_ms", "send_ms", "confirm_ms", "total_ms"):
            n, p50, p90 = quantiles([r[k] for r in sub])
            line.append("%s %s/%s(n=%d)" % (k.replace("_ms", ""),
                                            "-" if p50 is None else "%.0f" % p50,
                                            "-" if p90 is None else "%.0f" % p90, n))
        w(" ".join(line))
        qn = [r["quote_n"] for r in sub if r["quote_n"] is not None]
        w("           quote_n>1 on %d of %d" % (sum(1 for x in qn if x and x > 1), len(qn)))
    w("")
    w("debounce reads before the decision (0 = the confirming read was the first breach):")
    for ch in chains:
        sub = [r for r in rows if r["chain"] == ch and r["first_breach_pct"] is not None]
        zero = sum(1 for r in sub if r["prior_breach_reads"] == 0)
        w("  %-8s n=%-4d first-read decisions=%d (%.0f%%)"
          % (ch, len(sub), zero, 100.0 * zero / len(sub) if sub else 0))
    return "\n".join(out)


def selftest():
    """Guards the ways this script can silently lie."""
    # 1. pct_of must not invent a number from a zero or missing price.
    assert pct_of(None, 1.0) is None and pct_of(1.0, 0.0) is None and pct_of(0.0, 1.0) is None
    assert abs(pct_of(0.65, 1.0) - (-35.0)) < 1e-9
    # 2. the decomposition must be an identity: (0)+(a)+(b)+(c) == overshoot.
    entry, fb, dec, exq, real = 1.0, 0.64, 0.58, 0.575, 0.57
    g0 = pct_of(fb, entry) - TRIGGER_PCT
    ga = pct_of(dec, entry) - pct_of(fb, entry)
    gb = pct_of(exq, entry) - pct_of(dec, entry)
    gc = pct_of(real, entry) - pct_of(exq, entry)
    assert abs((g0 + ga + gb + gc) - (pct_of(real, entry) - TRIGGER_PCT)) < 1e-9
    # 3. the confirming run starts after the last recovery, and an unusable price is not a read.
    #    This calls the function build() calls, so the guard cannot drift away from the code.
    f = confirming_run_start
    assert f([-10.0, -36.0, -10.0, -40.0, -42.0]) == 3, "a recovered dip must not start the run"
    assert f([-10.0, -38.0, None, -40.0, -42.0]) == 3, "an unusable price breaks the run"
    assert f([-10.0, -38.0, -40.0, -42.0]) == 1, "an uninterrupted run starts at its own head"
    assert f([-10.0, -20.0]) is None and f([]) is None
    # 4. 'T' > ' ': an ISO stamp must never be compared against a SQLite datetime() rendering.
    assert "2026-09-23T09:03" > "2026-09-23 09:03"
    # 5. both stage-map shapes normalise to the same keys.
    class R(dict):
        def __getitem__(self, k):
            return dict.get(self, k)
    evm = stage_map(R(exit_timing_json=None),
                    R(stage_ms_json='{"quote_ms":5597.3,"quote_n":2,"sign_ms":1.0}', total_ms=5736.7))
    sol = stage_map(R(exit_timing_json='{"ladder_rungs":1,"total_ms":1057.2,'
                                       '"stages_ms":{"quote":145.0,"sign":216.3,"submit":41.3,"confirm":612.7}}'),
                    None)
    assert evm[1] == "exit_execution_attempts" and evm[0]["quote_ms"] == 5597.3
    assert sol[1] == "exit_timing_json" and sol[0]["send_ms"] == 41.3 and sol[0]["quote_n"] == 1
    print("selftest ok")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--csv")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest()
        raise SystemExit(0)
    rows, now, cut = build(a.days)
    print(report(rows, now, cut, a.days))
    if a.csv:
        import csv
        with open(a.csv, "w", newline="") as fh:
            wr = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            wr.writeheader()
            wr.writerows(rows)
        print("\ncsv -> %s (%d rows)" % (a.csv, len(rows)))
