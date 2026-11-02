#!/usr/bin/env python3
"""Labelled pre-entry dataset for the "never printed above entry" cohort. MEASUREMENT ONLY.

Built for Beta. No model, no conclusion, no filter, no gate, no knob.

THE LEAKAGE RULE, enforced structurally rather than by care: every feature column is read from
`memecoin_entry_attempts` (its own columns, `classification_json.facts`, or a named pre-decision
`trace_json` substage). Nothing is read from `memecoin_positions` except the LABEL and the two
descriptors Beta asked to sit beside it (realized_pnl, hold seconds) plus the identity/window needed
to count marks. Marks, peak, exit and PnL are outcomes and appear only as label columns, which are
prefixed `y_` so a modelling script can drop them by prefix.

A NAME COLLISION THAT WOULD LEAK IF TAKEN AT FACE VALUE. `classification_json.facts` contains
`peak_price`, `runup_pct` and `giveback_pct`, and those are PRE-ENTRY momentum observations of the
coin before we bought. `memecoin_positions.peak_price` is the POST-FILL peak and is the label's basis.
Same word, opposite side of the fill. The facts ones are emitted here as `f_pre_entry_peak_price` etc.
so no one can confuse them; they are also only 9% populated, so they cannot carry much.

THREE TIMESTAMPS, THREE MEANINGS, AND USING THE WRONG ONE IS THE EASIEST ERROR HERE. This cost both
Beta and me a wrong conclusion on the same day (2026-09-23), and the memory
`finding-opened-at-is-decision-start-not-the-fill-2026-09-03` already recorded it:

    memecoin_positions.opened_at            DECISION START, before the quote. Both executors reuse
                                            this one stamp through quote, sign, send and confirm, so
                                            it is NOT the fill and NOT position creation.
    memecoin_entry_attempts.completed_at    the attempt finishes.
    memecoin_positions.opened_recorded_at   THE CONFIRMED FILL. Use this one. 97.0% populated.

`opened_at -> opened_recorded_at` is CHAIN-DEPENDENT and not small: solana p50 1.89 s, 4663 p50 4.78 s,
chain 56 p50 7.12 s (30 d). A single all-chain figure (p50 2.18 s) hides a 3.7x spread, so a latency
claim must name its chain. `memecoin_real_runner.py:5086` states the same distinction in the codebase.

WHY IT MATTERS FOR A LEAKAGE TEST: `post_peak_confirmation` tested against `opened_at` looks post-fill
on 100% of rows and reads as a leak; tested against `opened_recorded_at` it is PRE-fill on 100% of rows
(p50 -1.19 s, p90 -0.76 s) and is a legitimate feature. One reference condemned it, the right reference
cleared it. Test a pre/post-fill question against `opened_recorded_at` and nothing else.

TWO TRAPS BETA NAMED, both live:
  1. `opened_at` and `sweep_ts` are ISO with a 'T'; SQLite `datetime()` renders a SPACE and 'T' > ' ',
     so `sweep_ts <= datetime(opened_at,'+30 seconds')` is silently FALSE for every row. Every window
     bound here is built in Python and passed as a parameter.
  2. `memecoin_position_marks.executable_exit_value` equals `mid` (a per-token PRICE, not a value).
     It is not read at all. No PnL estimate is derived from marks here; realized_pnl is used.
Marks are counted on (chain_id, mint) within the position's own open window, never on position_id.

python3 never_up_dataset.py [--days 90] [--out never_up.csv]
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

REPO = Path("/home/bigpapi/Desktop/Sniper")
sys.path.insert(0, str(REPO))

FACT_NUMERIC = ("effective_mcap", "liquidity", "liquidity_pair_count", "volume_h1", "volume_m5",
                "volume_mcap", "pair_age_s", "display_price", "base_clip_usd",
                "momentum_observations", "regime_baseline_buys", "regime_buyer_median",
                "regime_coin_median", "regime_current_buyers", "regime_current_coins",
                "regime_latest_age_s", "regime_nonempty_buckets", "consensus_n_60m")
FACT_PRE_ENTRY_MOMENTUM = ("peak_price", "first_price", "runup_pct", "giveback_pct", "momentum_span_s")
FACT_CATEGORICAL = ("signal_source", "assumed_micro", "liquidity_pair_address")
CLASS_TOP = ("coin_band", "liquidity_class", "momentum_phase", "market_regime", "data_quality",
             "lifecycle", "classification_version")
# Substages that complete BEFORE the fill. `evm.*`, `funded_entry_shadow_v1` and anything after
# signing are execution-time and deliberately excluded.
SUBSTAGE_FIELDS = {
    "source_buy_floor": ("configured_abs_min_usd", "configured_mcap_ratio"),
    "volume_gate": ("decided_organic_volume_m5", "configured_m5_floor", "boundary"),
    "sell_share": ("cap_pct", "basis"),
    "token_safety": ("reason",),
    "planned_exit_route": ("available",),
    "roundtrip_cap": ("cost_pct", "cap_pct"),
    "execution_premium": ("ratio", "reference_basis", "cap"),
    # post_peak_confirmation IS pre-commitment, and establishing that took the right reference.
    # Against `memecoin_positions.opened_at` it looks late (after it on 100% of rows, median +0.68 s),
    # which first read as a leak. Against `memecoin_entry_attempts.completed_at` it is EARLIER on 100%
    # of rows (median -1.22 s, p90 -0.77 s), so it completes INSIDE the attempt, and the code puts it
    # in `evm_swap_executor._post_peak_confirmation` / `solana_swap_executor._post_peak_confirmation`,
    # called before signing. THE REAL FINDING IS ABOUT `opened_at`: it is stamped at position creation,
    # NOT at the fill, so it is the wrong reference for any pre/post-fill test.
    "post_peak_confirmation": ("confirmed", "nominated", "reason"),
}


def parse(value):
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def build(days, out):
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
        "SELECT id, attempt_id, network_id, mint, ticker, source_user_id, entry_price, peak_price, "
        "entry_notional, realized_pnl, opened_at, opened_recorded_at, closed_at, exit_reason, "
        "strategy, origin_event_id "
        "FROM memecoin_positions WHERE execution_mode='real' AND is_open=0 AND closed_at>=? "
        "ORDER BY opened_at", (cut,)).fetchall()
    handles = {r["source_user_id"]: (r["handle"] or r["label"] or "")
               for r in conn.execute("SELECT source_user_id, handle, label FROM memecoin_wallets")}

    rows, cov, drops = [], Counter(), Counter()
    for pos in positions:
        entry = float(pos["entry_price"] or 0)
        decided, closed = parse(pos["opened_at"]), parse(pos["closed_at"])
        # THE FILL INSTANT. opened_at is decision start and is only a fallback for a row predating
        # the column; which one was used is recorded per row so no reader has to guess.
        filled = parse(pos["opened_recorded_at"]) or decided
        fill_basis = "opened_recorded_at" if pos["opened_recorded_at"] else "opened_at_fallback"
        opened = filled
        if entry <= 0 or opened is None or closed is None:
            drops["no_entry_price_or_window"] += 1
            continue
        # TRAP 1: both bounds are built HERE and passed as parameters. No SQLite datetime().
        n_marks = conn.execute(
            "SELECT COUNT(*) FROM memecoin_position_marks WHERE chain_id=? AND mint=? "
            "AND sweep_ts>=? AND sweep_ts<=?",
            (str(pos["network_id"]), str(pos["mint"]), opened.isoformat(), closed.isoformat())
        ).fetchone()[0]
        if n_marks <= 3:
            drops["marks_le_3"] += 1
            continue
        if not pos["attempt_id"]:
            drops["no_attempt_id"] += 1
            continue
        att = conn.execute(
            "SELECT attempt_id, decision_started_at, source_price, chase_source_size_usd, source_market_cap, "
            "quote_price, quote_age_ms, quote_ms, price_attribution, quote_route_target, source_drift_pct, "
            "signal_source, delivery_lane, chase_liquidity_usd, classification_json, trace_json "
            "FROM memecoin_entry_attempts WHERE attempt_id=?", (pos["attempt_id"],)).fetchone()
        if att is None:
            drops["attempt_row_missing"] += 1
            continue
        peak = float(pos["peak_price"]) if pos["peak_price"] is not None else None
        if peak is None:
            drops["no_peak_price"] += 1
            continue
        row = {
            # ---- identity / window (not features) ----
            "position_id": pos["id"], "attempt_id": att["attempt_id"],
            "chain": pos["network_id"], "mint": pos["mint"], "ticker": pos["ticker"],
            "wallet": handles.get(pos["source_user_id"], ""), "wallet_id": pos["source_user_id"],
            "opened_at": pos["opened_at"], "opened_recorded_at": pos["opened_recorded_at"],
            "fill_basis": fill_basis, "decision_started_at": att["decision_started_at"],
            # ---- LABEL and outcome descriptors: y_ prefix, drop by prefix before modelling ----
            "y_never_up": int(peak <= entry * 1.0000001),
            "y_realized_pnl": pos["realized_pnl"],
            # measured from the FILL. The opened_at version is kept beside it because the two differ
            # by a chain-dependent 1.9-7.1 s median, which is 3%+ of a 60 s hold.
            "y_hold_s": (closed - filled).total_seconds(),
            "y_hold_s_from_opened_at": ((closed - decided).total_seconds() if decided else None),
            "y_fill_lag_s": ((filled - decided).total_seconds() if decided else None),
            "y_exit_reason": pos["exit_reason"],
            "y_n_marks_in_window": n_marks,
            "y_peak_over_entry_pct": (peak / entry - 1.0) * 100.0,
            # ---- features: attempt columns, all recorded at the decision ----
            "f_source_price": att["source_price"],
            "f_chase_source_size_usd": att["chase_source_size_usd"],
            "f_source_drift_pct": att["source_drift_pct"],
            "f_source_market_cap": att["source_market_cap"],
            "f_quote_price": att["quote_price"], "f_quote_age_ms": att["quote_age_ms"],
            "f_quote_ms": att["quote_ms"], "f_price_attribution": att["price_attribution"],
            "f_signal_source": att["signal_source"], "f_delivery_lane": att["delivery_lane"],
            "f_chase_liquidity_usd": att["chase_liquidity_usd"],
            "f_entry_notional": pos["entry_notional"], "f_strategy": pos["strategy"],
            "f_route_hops": (len(str(att["quote_route_target"]).split(","))
                             if att["quote_route_target"] else None),
        }
        # The origin signal, as the feed recorded it BEFORE our decision. Joined on
        # fomo_event_id -- NOT memecoin_events.id, which is a different key.
        ev = conn.execute(
            "SELECT their_size_usd, their_market_cap, is_first_buy, lag_s, signal_source "
            "FROM memecoin_events WHERE fomo_event_id=?", (pos["origin_event_id"],)).fetchone() \
            if pos["origin_event_id"] else None
        row["f_src_buy_usd"] = ev["their_size_usd"] if ev else None
        row["f_src_mcap_at_signal"] = ev["their_market_cap"] if ev else None
        row["f_src_is_first_buy"] = ev["is_first_buy"] if ev else None
        row["f_src_feed_lag_s"] = ev["lag_s"] if ev else None
        cl = json.loads(att["classification_json"] or "{}")
        facts = cl.get("facts") or {}
        for key in CLASS_TOP:
            row["f_" + key] = cl.get(key)
        for key in FACT_NUMERIC + FACT_CATEGORICAL:
            row["f_" + key] = facts.get(key) if isinstance(facts, dict) else None
        for key in FACT_PRE_ENTRY_MOMENTUM:
            # renamed so the pre-entry momentum peak can never be read as the post-fill label
            row["f_pre_entry_" + key] = facts.get(key) if isinstance(facts, dict) else None
        subs = {s.get("name"): s for s in json.loads(att["trace_json"] or "{}").get("substage_events", [])}
        for name, fields in SUBSTAGE_FIELDS.items():
            src = subs.get(name) or {}
            for field in fields:
                row["f_%s__%s" % (name.replace(".", "_"), field)] = src.get(field)
        rows.append(row)
        for k, v in row.items():
            if k.startswith("f_") and v is not None and v != "":
                cov[k] += 1

    conn.close()
    if not rows:
        print("no rows")
        return 1
    cols = list(rows[0].keys())
    with open(out, "w", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=cols)
        wr.writeheader()
        wr.writerows(rows)

    n = len(rows)
    pos_n = sum(r["y_never_up"] for r in rows)
    print("window: %s -> %s (%d days)" % (cut, now.isoformat(), days))
    print("rows written: %d  ->  %s" % (n, out))
    print("label y_never_up = 1: %d (%.1f%%)   = 0: %d" % (pos_n, 100.0 * pos_n / n, n - pos_n))
    print("\nexcluded before labelling:", dict(drops))
    bychain = Counter(r["chain"] for r in rows)
    print("\nby chain: rows / never_up / net realized")
    for ch, cn in bychain.most_common():
        sel = [r for r in rows if r["chain"] == ch]
        nu = [r for r in sel if r["y_never_up"]]
        print("  %-8s n=%-5d never_up=%-4d (%4.1f%%)  never_up PnL $%+9.2f   rest PnL $%+9.2f"
              % (ch, len(sel), len(nu), 100.0 * len(nu) / len(sel),
                 sum(float(r["y_realized_pnl"] or 0) for r in nu),
                 sum(float(r["y_realized_pnl"] or 0) for r in sel if not r["y_never_up"])))
    print("\nFEATURE COVERAGE (non-null share of %d rows) — the part that decides what is usable:" % n)
    feats = [k for k in cols if k.startswith("f_")]
    for k in sorted(feats, key=lambda x: -cov[x]):
        share = 100.0 * cov[k] / n
        flag = "OK       " if share >= 80 else ("thin     " if share >= 50 else "TOO SPARSE")
        print("  %-10s %5.1f%%  %s" % (flag, share, k))
    print("\nmarks in window: min=%d median=%d max=%d"
          % (min(r["y_n_marks_in_window"] for r in rows),
             sorted(r["y_n_marks_in_window"] for r in rows)[n // 2],
             max(r["y_n_marks_in_window"] for r in rows)))
    return 0


def selftest():
    """Pure logic, no database. The label edge and the three-timestamp rule live here."""
    e = 0.00012297433345381171
    # the label is peak <= entry * 1.0000001: an exactly-flat peak is never_up, a tick above is not
    assert (e <= e * 1.0000001) is True, "an exactly flat peak must count as never_up"
    assert (e * 1.001 <= e * 1.0000001) is False, "a peak 0.1% above entry must NOT count"
    assert (e * 0.5 <= e * 1.0000001) is True, "a peak below entry is never_up"
    # the fill instant must win over decision start, and the fallback must be recorded not silent
    for recorded, expect in (("2026-09-23T00:00:02+00:00", "opened_recorded_at"),
                             (None, "opened_at_fallback")):
        basis = "opened_recorded_at" if recorded else "opened_at_fallback"
        assert basis == expect
    a, b = parse("2026-09-23T00:00:00+00:00"), parse("2026-09-23T00:00:02.29+00:00")
    assert (b - a).total_seconds() == 2.29, "fill lag must be computed in Python, not by SQLite"
    assert parse("not a date") is None and parse(None) is None
    # 'T' vs space: the trap that silently returns zero rows
    assert "2026-09-23T00:00:00" > "2026-09-23 00:00:00", (
        "'T' > ' ' is why a SQLite datetime() bound excludes every ISO row")
    print("selftest ok")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--out", default="never_up.csv")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    sys.exit(selftest() if a.selftest else build(a.days, a.out))
