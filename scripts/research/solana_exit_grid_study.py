#!/usr/bin/env python3
"""Solana funded exit grid: would another TP, stop or trail have beaten the policy that ran?

Pre-registration: docs/superpowers/specs/2026-10-16-solana-exit-grid-preregistration.md. Every
threshold below is fixed there and does not change after Gamma's PASS.

Read-only on the ledger. The only network reads are the entry and exit receipts (fees and tips, for
absolute PnL only), on the research Helius key through funded_exit_truth.rpc's shared pacer, never a
funded lane, and only after the validity check has passed.

    python3 scripts/research/solana_exit_grid_study.py --cohort
    python3 scripts/research/solana_exit_grid_study.py --read --out <new file>.json

`--cohort` reads identity and coverage only, never an exit value. `--read` is the one read: it
refuses to overwrite an existing output file.
"""
import argparse
import bisect
import json
import os
import sqlite3
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "research"))

START = "2026-08-26T13:08:00+00:00"     # first exit_decision row on the route series
SEAL_AT = "2026-10-16T06:00:00+00:00"   # cohort = opened in [START, SEAL_AT)
# The as-run stop mechanics are known and constant only from here: the instant bypass (fda39f3a8,
# 09-03) and its 20 s grace (557c421e4, 09-04) are in, and the elapsed-time confirm windows are off
# (last breach_pending audit row 09-03, last tp_confirm_pending 08-30). Earlier positions are counted
# and reported apart, never read.
RULES_FROM = "2026-09-05T00:00:00+00:00"
INSTANT_PCT = 50.0                      # MEMECOIN_REAL_STOP_INSTANT_PCT
INSTANT_GRACE_S = 20.0                  # MEMECOIN_REAL_STOP_INSTANT_GRACE_S
HIT_SPACING_S = 0.75                    # 0.75 x MEMECOIN_FASTLANE_TICK_S (1 s): stop_hit_counts
# stop_hit_counts arrived with the rail wake (5bc3ad3f2), live from the runner restart on release
# 068dd3e7 at 2026-09-27T04:38:38Z (HANDOFF). Before it the debounce counted every sweep.
SPACING_FROM = "2026-09-27T04:38:38+00:00"
# MEMECOIN_REAL_NO_LOSS_SELL_S: 30 from the 2026-09-04 evening deploy of 9d866c348 (the last loss
# close inside 30 s of its fill was 17:10Z; none from then until the switch-off), set to 0 at
# 2026-09-15T16:58Z (HANDOFF, operator "Remove the 30 sec no bad wick sell"). A loss read inside
# the window is no exit, as live.
NO_LOSS_SELL_S = 30.0
NO_LOSS_OFF_AT = "2026-09-15T16:58:00+00:00"
FAMILY = "follow-30-50"
COVER_WINDOW_S = 5.0
MIN_COVERAGE = 0.80
MAX_GAP_S = 60.0
MAX_QUOTE_AGE_S = 5.0
MIN_WALLET_N = 20
MIN_INCLUDED_SHARE = 0.50
MAX_INHERITED = 0.50
MAX_VALIDITY_REL_ERR = 0.05
BOOT_N = 10_000
SEED = 20261016
ALPHA = 0.025                           # one-sided: the lower bound of a two-sided 95% CI above 0
MAX_HOLD_S = 14400.0

TPS = (10, 15, 25, 35, 50, None)
SLS = (20, 35, 50, None)
TRAILS = (None, 10, 20)
ARMS = tuple((tp, sl, tr) for tp in TPS for sl in SLS for tr in TRAILS)
CONTROL = (25, 35, None)
CONTROL_CATASTROPHE = 80.0
CONTROL_CONTRACT = {"tp_pct": 25.0, "stop_pct": 35.0, "trail_pct": 0.0, "catastrophe_pct": 80.0,
                    "max_hold_s": 14400.0}

# realized exit_reason -> the replay's label, for the rails this replay simulates
RAIL_REASONS = {"real_tp": "tp", "b_stop": "stop", "catastrophe": "catastrophe",
                "b_timecap": "max_hold", "real_trail": "trail"}
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def arm_label(arm):
    tp, sl, tr = arm
    off = lambda v: "off" if v is None else str(v)
    return "tp%s/sl%s/trail%s" % (off(tp), off(sl), off(tr))


# --- the pure parts (pinned by tests/test_solana_exit_grid_study.py) ----------------------------

def coverage(times, life, window=COVER_WINDOW_S):
    """Share of [0, life] lying within `window` s after a usable row, and every gap between rows,
    counting both ends (open -> first row, last row -> close). `times` are seconds from open."""
    if life <= 0:
        return 0.0, [(0.0, 0.0)]
    pts = sorted(t for t in times if 0.0 <= t <= life)
    covered = sum(min(window, nxt - t) for t, nxt in zip(pts, pts[1:] + [life]))
    edges = [0.0] + pts + [life]
    return covered / life, list(zip(edges, edges[1:]))


def first_bad_gap(gaps, max_gap=MAX_GAP_S):
    return next(((a, b) for a, b in gaps if b - a > max_gap), None)


def gap_cause(own_rows_inside, runner_alive_inside):
    """Why a position had no usable quote for a while. `stale`: its own rows exist but are older than
    the quote-age limit. `quote_miss`: the runner was recording (other route rows or a mark for this
    mint) but this position got no quote: route dead, token failure or a provider fault for this
    mint, not separable on this history. `outage`: nothing was recorded at all."""
    if own_rows_inside:
        return "stale"
    return "quote_miss" if runner_alive_inside else "outage"


def simulate(series, notional, *, tp=None, sl=None, trail=None, catastrophe=None,
             max_hold_s=MAX_HOLD_S, debounce=1, instant=INSTANT_PCT, grace_s=INSTANT_GRACE_S,
             fill_offset_s=0.0, spacing_s=HIT_SPACING_S, no_loss_s=0.0):
    """The first exit a rule set takes on `series` [(t_s, expected_usdc)], or None if none fires.

    Live trigger convention: r = unit_price / entry_price - 1 (memecoin_real_runner.py:587), with
    unit_price = expected_usdc / open_qty (_executable_unit_price). For a whole bag open_qty is the
    entry qty and entry notional = entry_price * qty, so r = expected_usdc / notional - 1.
    `trail` is (arm_pct, giveback_pct) or None. Order at one quote, as live: stop, catastrophe, TP,
    trail, max hold. Every rule exits at that quote's value, so the order changes only the label.

    LOSS RAILS AS LIVE (the funded sweep in memecoin_real_runner): a stop or catastrophe read sells
    at once when r <= -instant% unless it is a stop younger than `grace_s` after the fill that is not
    past the catastrophe floor; otherwise it needs `debounce` counted hits, where hit k+1 counts only
    once the breach is k x `spacing_s` old (stop_hit_counts), and any quote not at a loss threshold
    resets the count. A pending loss read takes no other exit on that quote (the live `continue`).
    Inside `no_loss_s` of the fill a loss read is no exit at all."""
    peak, armed, hits, breach_t = 0.0, False, 0, None
    for t, v in series:
        r = v / notional - 1.0
        peak = max(peak, v)
        past_floor = bool(catastrophe) and r <= -catastrophe / 100.0
        loss = "stop" if sl and r <= -sl / 100.0 else "catastrophe" if past_floor else None
        if loss and t - fill_offset_s < no_loss_s:
            continue                    # no count can exist yet: the window opens at the fill
        if loss:
            in_grace = (grace_s > 0 and loss == "stop" and not past_floor
                        and t - fill_offset_s < grace_s)
            if instant and r <= -instant / 100.0 and not in_grace:
                return t, v, loss
            if hits == 0 or t - breach_t >= hits * spacing_s:
                breach_t = t if hits == 0 else breach_t
                hits += 1
            if hits >= debounce:
                return t, v, loss
            continue
        hits, breach_t = 0, None
        if tp and r >= tp / 100.0:
            return t, v, "tp"
        if trail and trail[1]:
            armed = armed or peak / notional - 1.0 >= trail[0] / 100.0
            if armed and v <= peak * (1.0 - trail[1] / 100.0):
                return t, v, "trail"
        if max_hold_s and t >= max_hold_s:
            return t, v, "max_hold"
    return None


def grid_rules(arm, catastrophe, *, debounce=1, fill_offset_s=0.0, no_loss_s=0.0,
               spacing_s=HIT_SPACING_S):
    """An arm's rails; the loss-rail mechanics (debounce, instant, grace, no-loss window, hit
    spacing) are the position's own."""
    tp, sl, tr = arm
    return dict(tp=tp, sl=sl, trail=(tr, tr) if tr else None, catastrophe=catastrophe,
                debounce=debounce, fill_offset_s=fill_offset_s, no_loss_s=no_loss_s,
                spacing_s=spacing_s)


def contract_rules(policy, *, fill_offset_s=0.0, no_loss_s=0.0, spacing_s=HIT_SPACING_S):
    """The as-run contract's rails (0 = off, the funded convention) and its stop debounce."""
    g = lambda k: float(policy.get(k) or 0.0)
    return dict(tp=g("tp_pct") or None, sl=g("stop_pct") or None,
                trail=(g("trail_arm_pct"), g("trail_pct")) if g("trail_pct") else None,
                catastrophe=g("catastrophe_pct") or None, max_hold_s=g("max_hold_s") or MAX_HOLD_S,
                debounce=int(policy.get("stop_debounce_sweeps") or 1), fill_offset_s=fill_offset_s,
                no_loss_s=no_loss_s, spacing_s=spacing_s)


def mechanics(p):
    """The position's own fill offset, no-loss window and stop-hit spacing."""
    return {"fill_offset_s": p.get("fill_offset", 0.0), "no_loss_s": p.get("no_loss_s", 0.0),
            "spacing_s": p.get("spacing_s", HIT_SPACING_S)}


def is_control_contract(policy):
    """Does the as-run contract equal the control (so the control IS the rule that ran)?"""
    return all(float(policy.get(k) or 0.0) == v for k, v in CONTROL_CONTRACT.items())


def native_cost_lamports(tx, tip_accounts):
    """meta.fee plus every lamport the transaction moved into a tip account, or None if unreadable.
    Tips are SOL transfers, not part of meta.fee."""
    try:
        meta, msg = tx["meta"], tx["transaction"]["message"]
        loaded = meta.get("loadedAddresses") or {}
        keys = list(msg["accountKeys"]) + list(loaded.get("writable") or []) + list(
            loaded.get("readonly") or [])
        tip = sum(max(0, int(post) - int(pre)) for k, pre, post in
                  zip(keys, meta["preBalances"], meta["postBalances"]) if k in tip_accounts)
        return int(meta["fee"]) + tip, tip
    except (KeyError, TypeError, ValueError):
        return None


def sol_usd_from_plan(evidence):
    """SOL/USD from a whole-bag quote's own wSOL -> USDC leg, or None."""
    try:
        plan = json.loads(evidence).get("route_plan") or []
    except (TypeError, ValueError, AttributeError):
        return None
    for leg in plan:
        if leg.get("inputMint") == WSOL and leg.get("outputMint") == USDC:
            try:
                sol, usd = int(leg["inAmount"]) / 1e9, int(leg["outAmount"]) / 1e6
            except (KeyError, TypeError, ValueError):
                return None
            return usd / sol if sol > 0 and usd > 0 else None
    return None


def holm(pvals, alpha=ALPHA):
    """Keys rejected by Holm's step-down at `alpha`."""
    out = set()
    for i, (key, p) in enumerate(sorted(pvals.items(), key=lambda kv: kv[1])):
        if p > alpha / (len(pvals) - i):
            break
        out.add(key)
    return out


def boot_draws(n_clusters, rng, b=BOOT_N):
    return rng.integers(0, n_clusters, size=(b, n_clusters))


def boot_stats(sums, sizes, draws):
    """Mean, 95% CI and one-sided p (H0: mean <= 0) of a cluster bootstrap. A position-level
    bootstrap is the same call with one position per cluster."""
    sums, sizes = np.asarray(sums, float), np.asarray(sizes, float)
    means = sums[draws].sum(axis=1) / sizes[draws].sum(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    p = (int((means <= 0).sum()) + 1) / (len(means) + 1)
    return float(sums.sum() / sizes.sum()), float(lo), float(hi), p


# --- the ledger -----------------------------------------------------------------------------------

def _connect():
    path = os.getenv("DB_PATH") or str(REPO / "sniper_tracker.db")
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def load(conn):
    """Every position in the window with its exclusion cause, series and identity. Reads no exit
    value: realized proceeds are attached later, by --read only."""
    start, seal, rules_from = ts(START), ts(SEAL_AT), ts(RULES_FROM)
    rows = [r for r in conn.execute(
        "SELECT id, source_user_id, mint, opened_at, opened_recorded_at, closed_at, is_open, "
        "exit_reason, funded_contract_json FROM memecoin_positions "
        "WHERE execution_mode='real' AND network_id='solana' ORDER BY id")
        if ts(r["opened_at"]) and start <= ts(r["opened_at"]) < seal]
    ids = {r["id"] for r in rows}
    fills = {}
    for f in conn.execute("SELECT f.position_id, f.kind, f.notional, f.qty, f.tx_sig, f.created_at "
                          "FROM memecoin_fills f JOIN memecoin_positions p ON p.id = f.position_id "
                          "WHERE p.execution_mode='real' AND p.network_id='solana'"):
        if f["position_id"] in ids:
            fills.setdefault(f["position_id"], []).append(dict(f))
    own = {}
    for q in conn.execute("SELECT position_id, observed_at, quoted_at, expected_usdc, "
                          "quote_evidence_json FROM memecoin_route_series "
                          "WHERE seam='exit_decision' AND network_id='solana'"):
        if q["position_id"] in ids:
            own.setdefault(q["position_id"], []).append(
                (ts(q["observed_at"]), ts(q["quoted_at"]), float(q["expected_usdc"] or 0.0),
                 q["quote_evidence_json"]))
    alive = sorted(t for (t,) in ((ts(o),) for (o,) in conn.execute(
        "SELECT observed_at FROM memecoin_route_series WHERE observed_at >= ?", (START,))) if t)

    out = []
    for r in rows:
        p = dict(id=r["id"], wallet=r["source_user_id"], mint=r["mint"], cause=None,
                 exit_reason=r["exit_reason"] or "")
        out.append(p)
        if r["is_open"]:
            p["cause"] = "still_open"
            continue
        try:
            policy = json.loads(r["funded_contract_json"])["policy"]
        except (TypeError, ValueError, KeyError):
            policy = None
        if not policy or not str(policy.get("policy_id") or "").startswith(FAMILY):
            p["cause"] = "other_policy:%s" % ((policy or {}).get("policy_id"))
            continue
        p["policy"] = policy
        p["control_contract"] = is_control_contract(policy)
        if ts(r["opened_at"]) < rules_from:
            p["cause"] = "pre_rules_era"
            continue
        fs = fills.get(r["id"], [])
        entry = [f for f in fs if f["kind"] == "entry"]
        exits = [f for f in fs if f["kind"] != "entry"]
        if len(entry) != 1 or not entry[0]["notional"] or entry[0]["notional"] <= 0:
            p["cause"] = "bad_entry_fill"
            continue
        if not exits:
            p["cause"] = "no_exit_fill"
            continue
        p.update(entry_fill=entry[0], exit_fills=exits, notional=float(entry[0]["notional"]))
        if len(exits) > 1 or any(f["kind"] == "derisk" for f in exits):
            p["cause"] = "partial_exit"
            continue
        opened, closed = ts(r["opened_at"]), ts(r["closed_at"])
        if not closed or closed <= opened:
            p["cause"] = "bad_window"
            continue
        life = closed - opened
        recorded = ts(r["opened_recorded_at"])
        p["fill_offset"] = max(0.0, recorded - opened) if recorded else 0.0
        p["no_loss_s"] = NO_LOSS_SELL_S if opened < ts(NO_LOSS_OFF_AT) else 0.0
        p["spacing_s"] = HIT_SPACING_S if opened >= ts(SPACING_FROM) else 0.0
        quotes = own.get(r["id"], [])
        usable = sorted((o - opened, v) for o, q, v, _e in quotes
                        if o and q and v > 0 and o - q <= MAX_QUOTE_AGE_S and 0 <= o - opened <= life)
        share, gaps = coverage([t for t, _v in usable], life)
        bad = first_bad_gap(gaps)
        p.update(opened=opened, life=life, coverage=share, series=usable, quotes=quotes)
        if bad is None and share >= MIN_COVERAGE:
            continue
        if bad is None:
            p["cause"] = "thin"
            continue
        a, b = opened + bad[0], opened + bad[1]
        own_inside = any(o and a < o < b for o, _q, _v, _e in quotes)
        i = bisect.bisect_right(alive, a + 1e-3)
        runner_alive = (i < len(alive) and alive[i] < b) or _mark_inside(conn, r["mint"], a, b)
        p["cause"] = gap_cause(own_inside, runner_alive)
        p["cut_at"] = bad[0]                    # the dead-route sensitivity truncates here
        p["series_cut"] = [(t, v) for t, v in usable if t <= bad[0]]
    return out


def _iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")


def _mark_inside(conn, mint, a, b):
    return conn.execute("SELECT 1 FROM memecoin_position_marks WHERE chain_id='solana' AND mint=? "
                        "AND sweep_ts > ? AND sweep_ts < ? LIMIT 1",
                        (mint, _iso(a), _iso(b))).fetchone() is not None


# --- reporting helpers ----------------------------------------------------------------------------

def _dist(xs):
    xs = sorted(xs)
    if not xs:
        return {"n": 0}
    q = lambda f: xs[min(len(xs) - 1, int(f * (len(xs) - 1)))]
    return {"n": len(xs), "mean": statistics.fmean(xs), "median": statistics.median(xs),
            "p10": q(0.1), "p90": q(0.9)}


def cohort_report(positions):
    """Exclusion counts and per-wallet shares. The read set is the included positions whose as-run
    contract equals the control; included positions on another contract are read apart."""
    causes, wallets = {}, {}
    for p in positions:
        cause = p["cause"] or ("included" if p.get("control_contract") else "included_other_contract")
        causes[cause] = causes.get(cause, 0) + 1
        w = wallets.setdefault(p["wallet"], {"total": 0, "read": 0})
        w["total"] += 1
        w["read"] += p["cause"] is None and bool(p.get("control_contract"))
    excluded_mix = {}
    for p in positions:
        if p["cause"] in ("outage", "quote_miss", "stale", "thin"):
            d = excluded_mix.setdefault(p["cause"], {})
            d[p["wallet"]] = d.get(p["wallet"], 0) + 1
    per_wallet = {str(w): dict(v, share=v["read"] / v["total"],
                               selected_subset=v["read"] / v["total"] < MIN_INCLUDED_SHARE,
                               primary=w is not None and v["read"] >= MIN_WALLET_N)
                  for w, v in wallets.items()}
    return {"window": [START, SEAL_AT], "rules_from": RULES_FROM, "causes": causes,
            "excluded_wallet_mix": {k: {str(w): n for w, n in v.items()}
                                    for k, v in excluded_mix.items()},
            "per_wallet": per_wallet}


# --- the read -------------------------------------------------------------------------------------

def realized(p):
    return sum(float(f["notional"] or 0.0) for f in p["exit_fills"])


def validity(included):
    """The as-run contract (its own TP, stop, trail, catastrophe, max hold and stop debounce)
    replayed on each position's series against what actually happened. The relative error is the
    execution haircut of a quote against the fill; it is reported, never folded into an arm."""
    per = {}
    early_non_rail = 0
    for p in included:
        got = simulate(p["series"], p["notional"], **contract_rules(p["policy"], **mechanics(p)))
        want = RAIL_REASONS.get(p["exit_reason"])
        if want is None:
            early_non_rail += got is not None and got[0] < p["life"] - COVER_WINDOW_S
            continue
        d = per.setdefault(want, {"rel": [], "signed": [], "dt": [], "match": 0, "silent": 0})
        if got is None:
            d["silent"] += 1
            continue
        real = realized(p)
        signed = (got[1] - real) / real if real > 0 else float("inf")
        d["signed"].append(signed)
        d["rel"].append(abs(signed))
        d["dt"].append(abs(got[0] - p["life"]))
        d["match"] += got[2] == want
    out = {"replay_fired_before_a_non_rail_close": early_non_rail, "per_rail": {}}
    every = []
    for rail, d in per.items():
        every += d["rel"]
        med = statistics.median(d["rel"]) if d["rel"] else None
        out["per_rail"][rail] = {
            "fired": len(d["rel"]), "replay_silent": d["silent"],
            "reason_match_share": d["match"] / len(d["rel"]) if d["rel"] else None,
            "rel_err": _dist(d["rel"]), "signed_rel_err_median": (
                statistics.median(d["signed"]) if d["signed"] else None),
            "abs_dt_s": _dist(d["dt"]),
            "passed": med is not None and med <= MAX_VALIDITY_REL_ERR}
    med = statistics.median(every) if every else None
    out["rel_err"] = _dist(every)
    out["passed"] = med is not None and med <= MAX_VALIDITY_REL_ERR
    out["stop_passed"] = bool(out["per_rail"].get("stop", {}).get("passed"))
    return out


def price_arms(p, series=None, inherit=None):
    """{arm: (value, fired)}. An arm that does not fire inherits, by default, the quote the as-run
    contract's replay fires on (the live decision quote), else the last usable quote at or before
    the close, so every value in an arm-minus-control difference is on the quote basis and an arm
    that inherits the control's own exit differs from it by exactly zero. A caller with no quote at
    the close (the dead-route read) passes the realized proceeds."""
    s = p["series"] if series is None else series
    if inherit is None:
        as_run = simulate(s, p["notional"], **contract_rules(p["policy"], **mechanics(p)))
        inherit = as_run[1] if as_run else (s[-1][1] if s else realized(p))
    n = int(p["policy"].get("stop_debounce_sweeps") or 1)
    out = {}
    for arm in ARMS:
        got = simulate(s, p["notional"], **grid_rules(arm, CONTROL_CATASTROPHE, debounce=n,
                                                      **mechanics(p)))
        out[arm] = (got[1], True) if got else (inherit, False)
    return out


def analyse(group, rng, *, clusters_by_wallet, fired_only=False, read_arms=ARMS):
    """Arm-minus-control on `group` (positions carrying p['arms']). The difference's denominator is
    the entry notional, so it never touches fees. Pooled reads cluster by wallet (a position with no
    wallet is its own cluster); a per-wallet read resamples positions."""
    rows, pvals = {}, {}
    for arm in read_arms:
        if arm == CONTROL:
            continue
        clusters, n, inherited = {}, 0, 0
        for p in group:
            v, fired = p["arms"][arm]
            if fired_only and not fired:
                continue
            n += 1
            inherited += not fired
            key = p["wallet"] if clusters_by_wallet and p["wallet"] is not None else ("p", p["id"])
            c = clusters.setdefault(key, [0.0, 0])
            c[0] += (v - p["arms"][CONTROL][0]) / p["notional"]
            c[1] += 1
        if n == 0:
            rows[arm_label(arm)] = {"n": 0}
            continue
        sums, sizes = zip(*clusters.values())
        mean, lo, hi, pv = boot_stats(sums, sizes, boot_draws(len(sums), rng))
        share = inherited / n
        rows[arm_label(arm)] = row = {"n": n, "mean_diff": mean, "ci95": [lo, hi],
                                      "p_one_sided": pv, "inherited_share": share,
                                      "identifiable": share <= MAX_INHERITED}
        if row["identifiable"]:
            pvals[arm_label(arm)] = pv
    if fired_only:
        return {"arms": rows, "beats_control": None}
    return {"arms": rows, "beats_control": sorted(a for a in holm(pvals) if rows[a]["mean_diff"] > 0)}


def absolute(group, fees, sol_usd):
    """Mean and median pnl_pct per arm with fees and tips, over positions whose fees are known."""
    out = {}
    known = [p for p in group if fees.get(p["id"]) is not None]
    for arm in ARMS:
        xs = []
        for p in known:
            entry_cost_sol, exit_cost_sol = fees[p["id"]]
            px_in, px_out = sol_usd(p["opened"]), sol_usd(p["opened"] + p["life"])
            if px_in is None or px_out is None:
                continue
            v = p["arms"][arm][0]
            xs.append((v - exit_cost_sol * px_out) / (p["notional"] + entry_cost_sol * px_in) - 1.0)
        out[arm_label(arm)] = _dist(xs)
    return {"positions_with_fees": len(known), "positions_total": len(group), "arms": out}


def hourly_sol_usd(positions):
    buckets = {}
    for p in positions:
        for o, _q, _v, e in p.get("quotes") or ():
            px = sol_usd_from_plan(e) if o else None
            if px:
                buckets.setdefault(int(o // 3600), []).append(px)
    med = {h: statistics.median(v) for h, v in buckets.items()}

    def at(t):
        h = int(t // 3600)
        for d in range(0, 7):
            for k in (h - d, h + d):
                if k in med:
                    return med[k]
        return None
    return at


def read_fees(included):
    """(entry SOL, exit SOL) per position from the receipts, or None where a receipt is missing.
    A stop on the research key leaves the rest unknown, never zero."""
    import funded_exit_truth as fet
    from sniper.execution.solana_sender_ab import NOZOMI_TIP_ACCOUNTS, TIP_ACCOUNTS
    tips = frozenset(TIP_ACCOUNTS) | frozenset(NOZOMI_TIP_ACCOUNTS)
    out, tipped, stopped = {}, 0, None
    for p in included:
        if stopped:
            out[p["id"]] = None
            continue
        costs = []
        try:
            for sig in [p["entry_fill"]["tx_sig"]] + [f["tx_sig"] for f in p["exit_fills"]]:
                tx = fet.rpc("getTransaction", [sig, {"encoding": "json", "commitment": "confirmed",
                                                      "maxSupportedTransactionVersion": 1}]) if sig else None
                costs.append(native_cost_lamports(tx, tips) if tx else None)
        except fet.Stop as e:
            stopped = str(e)
            out[p["id"]] = None
            continue
        if any(c is None for c in costs):
            out[p["id"]] = None
            continue
        tipped += costs[0][1] > 0
        out[p["id"]] = (costs[0][0] / 1e9, sum(c[0] for c in costs[1:]) / 1e9)
    return out, {"entries_with_tip": tipped, "stopped": stopped}


def run_read(positions, out_path):
    included = [p for p in positions if p["cause"] is None]
    primary = [p for p in included if p["control_contract"]]
    other = [p for p in included if not p["control_contract"]]
    result = {"cohort": cohort_report(positions)}
    partial = [p for p in positions if p["cause"] == "partial_exit"]
    result["partial_exits"] = _dist([realized(p) / p["notional"] - 1.0 for p in partial])
    result["validity"] = validity(included)
    if not result["validity"]["passed"]:
        Path(out_path).write_text(json.dumps(result, indent=1, default=str))
        print(json.dumps(result["validity"], indent=1))
        print("VALIDITY FAILED: the as-run replay does not reproduce the live exits; no arm is read.")
        return 3
    # The stop dimension is read only if the stop rail's own replay reproduces the live stops.
    stop_ok = result["validity"]["stop_passed"]
    read_arms = ARMS if stop_ok else tuple(a for a in ARMS if a[1] == CONTROL[1])
    result["sl_dimension_read"] = stop_ok
    for p in included:
        p["arms"] = price_arms(p)
    dead = [p for p in positions if p["cause"] in ("quote_miss", "stale") and p["control_contract"]]
    for p in dead:
        p["arms"] = price_arms(p, p["series_cut"], inherit=realized(p))
    thin = [p for p in positions if p["cause"] == "thin" and p["control_contract"]]
    for p in thin:
        p["arms"] = price_arms(p)
    fired = [p["arms"][CONTROL][1] for p in primary]
    result["control_fired_share"] = sum(fired) / len(fired) if fired else None
    rng = np.random.default_rng(SEED)

    def read(group, clusters, fired_only=False):
        return analyse(group, rng, clusters_by_wallet=clusters, fired_only=fired_only,
                       read_arms=read_arms)

    wallets = sorted({p["wallet"] for p in primary if p["wallet"] is not None})
    per_wallet = {}
    for w in wallets:
        group = [p for p in primary if p["wallet"] == w]
        if len(group) >= MIN_WALLET_N:
            per_wallet[w] = dict(read(group, False), n=len(group),
                                 fired_only=read(group, False, True)["arms"])
    result["per_wallet"] = per_wallet
    result["pooled"] = dict(read(primary, True), n=len(primary))
    result["sensitivity_fired_only_pooled"] = read(primary, True, True)["arms"]
    for label, extra in (("sensitivity_dead_route_kept", dead), ("sensitivity_thin_kept", thin)):
        group = primary + extra
        result[label] = {"added_positions": len(extra), "pooled": read(group, True),
                         "per_wallet": {w: read([p for p in group if p["wallet"] == w], False)
                                        for w in per_wallet}}
    result["other_contracts_apart"] = dict(read(other, True), n=len(other)) if other else None
    fees, fee_meta = read_fees(primary)
    result["fees"] = fee_meta
    result["absolute"] = absolute(primary, fees, hourly_sol_usd(positions))
    Path(out_path).write_text(json.dumps(result, indent=1, default=str))
    print("validity:", json.dumps({k: v for k, v in result["validity"].items() if k != "per_rail"}))
    print("pooled n=%d beats control: %s" % (len(primary), result["pooled"]["beats_control"]))
    for w, r in per_wallet.items():
        print("wallet %s n=%d beats control: %s" % (w, r["n"], r["beats_control"]))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--cohort", action="store_true")
    mode.add_argument("--read", action="store_true")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    if a.read and (not a.out or Path(a.out).exists()):
        print("--read needs --out naming a file that does not exist yet (the read runs once)")
        return 2
    positions = load(_connect())
    if a.cohort:
        print(json.dumps(cohort_report(positions), indent=1, default=str))
        return 0
    return run_read(positions, a.out)


if __name__ == "__main__":
    sys.exit(main())
