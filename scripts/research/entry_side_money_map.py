#!/usr/bin/env python3
"""Entry-side money map: what in our power moves funded fill outcomes (Beta, 2026-09-30).

Pre-registered as brain episode 1a5c73d8 before any knob-level outcome was read. Read-only on the
funded ledger (get_readonly_connection); every read is indexed and bounded to the population, about
600 positions. No network call.

Population: memecoin_positions execution_mode='real', opened_at >= 2026-09-13, closed, signal_source
!= 'manual', joined to memecoin_entry_attempts on attempt_id. Outcome: realized_pnl (USD) and
roi = realized_pnl / entry_notional. Hold-out: chronological median split on opened_at (H1 discovery,
H2 hold-out).

    python3 scripts/research/entry_side_money_map.py [--json out.json]
"""
import argparse
import json
import random
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SINCE = "2026-09-13T00:00:00+00:00"
STAGES = ("cluster", "chain_resolution", "market_data", "reservation", "safety_checks", "quote",
          "signing", "transaction_submitted", "chain_confirmation")
AGE_BINS = ((None, 5), (5, 15), (15, 30), (30, 60), (60, None))
MARK_TOLERANCE_S = 30.0


def _ts(iso):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None


def _secs(a, b):
    ta, tb = _ts(a), _ts(b)
    return (tb - ta).total_seconds() if ta and tb else None


def _q(values, p):
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    return v[min(len(v) - 1, int(round(p * (len(v) - 1))))]


def summary(rows):
    pnl = [r["pnl"] for r in rows]
    roi = [r["roi"] for r in rows if r["roi"] is not None]
    return {"n": len(rows), "pnl": round(sum(pnl), 2),
            "mean_roi_pct": round(100 * statistics.fmean(roi), 2) if roi else None,
            "median_roi_pct": round(100 * statistics.median(roi), 2) if roi else None,
            "win_pct": round(100 * sum(p > 0 for p in pnl) / len(pnl), 1) if pnl else None}


def boot_diff(a, b, n=2000, seed=7):
    """95% bootstrap interval of mean(roi a) - mean(roi b), in roi points (%)."""
    ra = [r["roi"] for r in a if r["roi"] is not None]
    rb = [r["roi"] for r in b if r["roi"] is not None]
    if len(ra) < 5 or len(rb) < 5:
        return None
    rnd = random.Random(seed)
    diffs = sorted(statistics.fmean(rnd.choices(ra, k=len(ra))) - statistics.fmean(rnd.choices(rb, k=len(rb)))
                   for _ in range(n))
    return [round(100 * diffs[int(0.025 * n)], 2), round(100 * diffs[int(0.975 * n)], 2)]


def halves(rows, split_at):
    return ([r for r in rows if r["opened_at"] < split_at], [r for r in rows if r["opened_at"] >= split_at])


def compare(rows, arm_fn, split_at, labels):
    """Per arm: overall, H1 and H2 summaries; plus the H1/H2 difference of the first two arms."""
    out = {}
    for lab in labels:
        arm = [r for r in rows if arm_fn(r) == lab]
        h1, h2 = halves(arm, split_at)
        out[lab] = {"all": summary(arm), "H1": summary(h1), "H2": summary(h2)}
    if len(labels) >= 2:
        a, b = labels[0], labels[1]
        for h in ("H1", "H2"):
            ra = [r for r in rows if arm_fn(r) == a and (r["opened_at"] < split_at) == (h == "H1")]
            rb = [r for r in rows if arm_fn(r) == b and (r["opened_at"] < split_at) == (h == "H1")]
            out[f"diff_{a}_minus_{b}_{h}_ci95"] = boot_diff(ra, rb)
    return out


def load(conn):
    pos = [dict(r) for r in conn.execute(
        "SELECT id, network_id, signal_source, source_user_id, source_handle, mint, entry_price, "
        "entry_notional, opened_at, opened_recorded_at, closed_at, realized_pnl, exit_reason, "
        "attempt_id, total_copy_drift_pct AS p_total_drift "
        "FROM memecoin_positions WHERE execution_mode='real' AND opened_at >= ? "
        "AND closed_at IS NOT NULL AND realized_pnl IS NOT NULL "
        "AND COALESCE(signal_source,'') != 'manual' ORDER BY opened_at", (SINCE,))]
    cols = ", ".join(["attempt_id", "delivery_lane", "source_created_at", "observed_at",
                      "decision_started_at", "transaction_submitted_at",
                      "chain_confirmation_completed_at", "source_drift_pct",
                      "execution_slippage_pct", "total_copy_drift_pct", "classification_json",
                      "funded_contract_json", "total_ms"]
                     + [f"{s}_ms" for s in STAGES])
    out = []
    for p in pos:
        a = conn.execute(f"SELECT {cols} FROM memecoin_entry_attempts WHERE attempt_id=?",
                         (p["attempt_id"],)).fetchone()
        a = dict(a) if a else {}
        cls = json.loads(a.get("classification_json") or "{}")
        contract = json.loads(a.get("funded_contract_json") or "{}")
        notional = float(p["entry_notional"] or 0)
        row = {**p, **{k: v for k, v in a.items() if k not in ("classification_json",
                                                               "funded_contract_json")}}
        row["band"] = cls.get("coin_band") or "none"
        policy = contract.get("policy") or {}
        row["policy_id"] = policy.get("policy_id")
        row["tp_pct"], row["stop_pct"] = policy.get("tp_pct"), policy.get("stop_pct")
        row["risk_scale"] = (contract.get("sizing") or {}).get("risk_scale")
        row["pnl"] = float(p["realized_pnl"])
        row["roi"] = row["pnl"] / notional if notional > 0 else None
        row["hold_s"] = _secs(p["opened_at"], p["closed_at"])
        row["decide_to_confirm_s"] = _secs(a.get("decision_started_at"),
                                           a.get("chain_confirmation_completed_at"))
        row["decide_to_submit_s"] = _secs(a.get("decision_started_at"), a.get("transaction_submitted_at"))
        row["submit_to_confirm_s"] = _secs(a.get("transaction_submitted_at"),
                                           a.get("chain_confirmation_completed_at"))
        row["age_at_decision_s"] = _secs(a.get("source_created_at"), a.get("decision_started_at"))
        row["observe_lag_s"] = _secs(a.get("source_created_at"), a.get("observed_at"))
        out.append(row)
    return out


def mark_near(conn, row, offset_s):
    """(unit price, offset error s, source) of the position mark nearest opened_at + offset_s,
    within MARK_TOLERANCE_S; None when there is none. Marks are per (chain, mint), from any
    position open on that coin, so a mark after our close exists only when another position
    (usually paper) still held it."""
    t = _ts(row["opened_at"]) + timedelta(seconds=offset_s)
    lo = (t - timedelta(seconds=MARK_TOLERANCE_S)).isoformat()
    hi = (t + timedelta(seconds=MARK_TOLERANCE_S)).isoformat()
    best = None
    for m in conn.execute("SELECT sweep_ts, mid, source, quality FROM memecoin_position_marks "
                          "WHERE chain_id=? AND mint=? AND sweep_ts>=? AND sweep_ts<=?",
                          (row["network_id"], row["mint"], lo, hi)):
        if m["mid"] is None or float(m["mid"]) <= 0:
            continue
        err = abs((_ts(m["sweep_ts"]) - t).total_seconds())
        if best is None or err < best[1]:
            best = (float(m["mid"]), err, m["source"])
    return best


# Mark sources whose last in-hold mark lands within about 10 roi points of the realised exit (p10/p90,
# measured in this study); the others (DexScreener, fastlane_mid, sol_state) miss by 25-50 points.
CALIBRATED = {("solana", "fastlane_sol_swap"), ("4663", "fastlane_rpc"),
              ("4663", "onchain_tape_post_close_post_swap_anchor")}


def _path(conn, r):
    """[(seconds since open, mark roi, source)] from calibrated in-hold marks."""
    out = []
    for m in conn.execute("SELECT sweep_ts, mid, source FROM memecoin_position_marks WHERE chain_id=? "
                          "AND mint=? AND sweep_ts>=? AND sweep_ts<=? ORDER BY sweep_ts",
                          (r["network_id"], r["mint"], r["opened_at"], r["closed_at"])):
        if m["mid"] and (r["network_id"], m["source"]) in CALIBRATED:
            out.append(((_ts(m["sweep_ts"]) - _ts(r["opened_at"])).total_seconds(),
                        float(m["mid"]) / float(r["entry_price"]) - 1.0, m["source"]))
    return out


def exit_sim(conn, rows, split_at):
    """Tighter-only exit alternatives replayed on in-hold marks (the path ends at our close, so a
    looser stop, a higher TP or a longer hold cannot be replayed). Rails only: operator closes and
    the non-default policies are left out. A stop fills at the policy level minus the stop slippage
    measured on real b_stop closes; a TP fills exactly at its level (real TP fills overshoot, so this
    understates a lower TP)."""
    base = [r for r in rows if r["exit_reason"] != "manual" and r["tp_pct"] == 25.0
            and r["stop_pct"] == 35.0 and r["entry_price"]]
    stops = [r["roi"] for r in base if r["exit_reason"] == "b_stop"]
    stop_slip = -statistics.fmean(stops) - 0.35 if stops else 0.0
    paths = {r["id"]: _path(conn, r) for r in base}
    sim = [r for r in base if paths[r["id"]]]
    # Exit cost of a mark-priced exit: realised roi minus the last calibrated mark's roi, for closes
    # with a mark in the final 10 s, per (chain, source). Applied to every simulated TP/time exit.
    gaps = defaultdict(list)
    hold = {r["id"]: r["hold_s"] for r in sim}
    for r in sim:
        t, roi, src = paths[r["id"]][-1]
        if hold[r["id"]] - t <= 10.0:
            gaps[(r["network_id"], src)].append(r["roi"] - roi)
    cost = {k: statistics.median(v) for k, v in gaps.items() if len(v) >= 10}
    policies = {}
    for T in (30, 60, 120, 300):
        policies[f"time_cap_{T}s"] = ("time", T)
    for X in (0.10, 0.15, 0.20):
        policies[f"tp_{int(X * 100)}"] = ("tp", X)
    for S in (0.20, 0.25, 0.30):
        policies[f"stop_{int(S * 100)}"] = ("stop", S)

    def replay(r, kind, v, extra=0.0):
        for t, roi, src in paths[r["id"]]:
            c = min(0.0, cost.get((r["network_id"], src), 0.0)) - extra
            if kind == "time" and t >= v:
                return roi + c
            if kind == "tp" and roi >= v:
                return v + c
            if kind == "stop" and roi <= -v:
                return -v - stop_slip
        if kind == "tp" and r["exit_reason"] == "real_tp":
            return v
        return r["roi"]

    out = {"eligible": len(base), "with_calibrated_path": len(sim),
           "stop_slippage_pts": round(100 * stop_slip, 2),
           "exit_cost_pts": {"|".join(k): round(100 * v, 2) for k, v in cost.items()}}
    for name, (kind, v) in policies.items():
        d = {}
        for h, rs in zip(("H1", "H2"), halves(sim, split_at)):
            delta = [(replay(r, kind, v) - r["roi"]) * float(r["entry_notional"]) for r in rs]
            worse = [(replay(r, kind, v, extra=0.05) - r["roi"]) * float(r["entry_notional"])
                     for r in rs]
            # Solana's time-like exits (manual, mirror) cost about 2.5 points against the marks,
            # more than the all-exit median; 4663's exact-route marks cost about 0.
            sol25 = [(replay(r, kind, v, extra=0.015 if r["network_id"] == "solana" else 0.0)
                      - r["roi"]) * float(r["entry_notional"]) for r in rs]
            by_chain = defaultdict(float)
            for r, x in zip(rs, delta):
                by_chain[r["network_id"]] += x
            d[h] = {"n": len(rs), "changed": sum(abs(x) > 1e-9 for x in delta),
                    "delta_pnl": round(sum(delta), 2),
                    "delta_pnl_if_exit_costs_5pts_more": round(sum(worse), 2),
                    "delta_pnl_solana_exit_cost_2_5pts": round(sum(sol25), 2),
                    "delta_by_chain": {k: round(v, 2) for k, v in by_chain.items()}}
        policies[name] = d
    out["policies"] = policies
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=None)
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)
    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import state_path, state_root
    load_env_files(str(state_root()))
    from sniper.persistence.db_core import get_readonly_connection
    conn = get_readonly_connection(args.db or str(state_path("sniper_tracker.db")))
    rows = load(conn)
    split_at = sorted(r["opened_at"] for r in rows)[len(rows) // 2]
    R = {"population": summary(rows), "split_at": split_at,
         "H1": summary(halves(rows, split_at)[0]), "H2": summary(halves(rows, split_at)[1])}

    # strata
    strata = defaultdict(list)
    for r in rows:
        strata[(r["network_id"], r["signal_source"], r["band"])].append(r)
    R["strata"] = {"|".join(map(str, k)): summary(v) for k, v in sorted(strata.items(),
                                                                          key=lambda kv: -len(kv[1]))}
    by_lane = defaultdict(list)
    for r in rows:
        by_lane[(r["network_id"], r["signal_source"])].append(r)

    # (1) latency: per chain x lane stage p50/p90, and slower vs faster half
    lat = {}
    for key, rs in by_lane.items():
        d = {"n": len(rs)}
        for name in ("age_at_decision_s", "observe_lag_s", "decide_to_submit_s", "submit_to_confirm_s",
                     "decide_to_confirm_s"):
            vals = [r[name] for r in rs if r[name] is not None]
            d[name] = [_q(vals, .5), _q(vals, .9), len(vals)]
        for s in STAGES:
            vals = [r[f"{s}_ms"] for r in rs if r.get(f"{s}_ms") is not None]
            if vals:
                d[f"{s}_ms"] = [round(_q(vals, .5), 1), round(_q(vals, .9), 1), len(vals)]
        lat["|".join(key)] = d
    R["latency_stages"] = lat
    for r in rows:
        rs = by_lane[(r["network_id"], r["signal_source"])]
        med = _q([x["decide_to_confirm_s"] for x in rs], .5)
        r["speed_arm"] = (None if r["decide_to_confirm_s"] is None or med is None else
                          "slow" if r["decide_to_confirm_s"] > med else "fast")
    R["latency_split"] = compare(rows, lambda r: r["speed_arm"], split_at, ["slow", "fast"])
    R["latency_split_by_lane"] = {
        "|".join(k): compare(rs, lambda r: r["speed_arm"], split_at, ["slow", "fast"])
        for k, rs in by_lane.items() if len(rs) >= 40}

    # (2) fill quality: total copy drift tertiles within chain
    by_chain = defaultdict(list)
    for r in rows:
        by_chain[r["network_id"]].append(r)
    for chain, rs in by_chain.items():
        vals = sorted(r["total_copy_drift_pct"] for r in rs if r["total_copy_drift_pct"] is not None)
        cuts = (_q(vals, 1 / 3), _q(vals, 2 / 3)) if len(vals) >= 9 else None
        for r in rs:
            v = r["total_copy_drift_pct"]
            r["drift_arm"] = (None if v is None or cuts is None else
                              "high" if v > cuts[1] else "low" if v <= cuts[0] else "mid")
    R["drift_coverage"] = {c: [sum(r["total_copy_drift_pct"] is not None for r in rs), len(rs)]
                           for c, rs in by_chain.items()}
    R["drift_tertiles"] = compare(rows, lambda r: r["drift_arm"], split_at, ["high", "low", "mid"])
    R["drift_tertiles_by_chain"] = {c: compare(rs, lambda r: r["drift_arm"], split_at,
                                               ["high", "low", "mid"])
                                    for c, rs in by_chain.items() if len(rs) >= 40}
    R["slippage_realised"] = {c: {"execution_slippage_pct p50/p90/max": [
        _q([r["execution_slippage_pct"] for r in rs], .5),
        _q([r["execution_slippage_pct"] for r in rs], .9),
        max([r["execution_slippage_pct"] for r in rs if r["execution_slippage_pct"] is not None],
            default=None)],
        "n": sum(r["execution_slippage_pct"] is not None for r in rs)} for c, rs in by_chain.items()}

    # (3) sizing: above vs below the median clip within chain x band
    by_cb = defaultdict(list)
    for r in rows:
        by_cb[(r["network_id"], r["band"])].append(r)
    for rs in by_cb.values():
        med = _q([r["entry_notional"] for r in rs], .5)
        for r in rs:
            r["size_arm"] = "big" if r["entry_notional"] > med else "small"
    R["size_split"] = compare(rows, lambda r: r["size_arm"], split_at, ["big", "small"])
    R["size_levels"] = {c: sorted({round(float(r["entry_notional"])) for r in rs})[:40]
                        for c, rs in by_chain.items()}
    R["size_vs_slippage"] = {c: {arm: [_q([r["execution_slippage_pct"] for r in rs
                                            if r["size_arm"] == arm], .5),
                                       sum(1 for r in rs if r["size_arm"] == arm)]
                                 for arm in ("big", "small")} for c, rs in by_chain.items()}

    # (4) signal age at decision
    def age_arm(r):
        a = r["age_at_decision_s"]
        if a is None:
            return None
        for lo, hi in AGE_BINS:
            if (lo is None or a >= lo) and (hi is None or a < hi):
                return f"{lo or 0}-{hi or 'inf'}"
    labels = [f"{lo or 0}-{hi or 'inf'}" for lo, hi in AGE_BINS]
    R["age_bins"] = {"|".join(k): compare(rs, age_arm, split_at, labels)
                     for k, rs in by_lane.items() if len(rs) >= 30}

    # (5) exits
    ex = defaultdict(list)
    for r in rows:
        ex[r["exit_reason"]].append(r)
    R["exit_reasons"] = {k: {**summary(v), "hold_p50_s": _q([r["hold_s"] for r in v], .5),
                             "hold_p90_s": _q([r["hold_s"] for r in v], .9)} for k, v in ex.items()}
    hb = defaultdict(list)
    for r in rows:
        h = r["hold_s"]
        hb["<30" if h < 30 else "30-60" if h < 60 else "60-120" if h < 120 else
           "120-300" if h < 300 else "300-900" if h < 900 else ">=900"].append(r)
    R["hold_bins"] = {k: summary(v) for k, v in hb.items()}
    # hold to open + 300 s, where a mark exists; rails only (operator exits excluded)
    cf = defaultdict(list)
    covered = defaultdict(lambda: [0, 0])
    for r in rows:
        if r["exit_reason"] == "manual":
            continue
        covered[r["exit_reason"]][1] += 1
        if r["hold_s"] >= 300 or not r["entry_price"]:
            continue
        m = mark_near(conn, r, 300.0)
        if m is None:
            continue
        covered[r["exit_reason"]][0] += 1
        roi_hold = m[0] / float(r["entry_price"]) - 1.0
        cf[r["exit_reason"]].append({"actual_roi": r["roi"], "hold300_roi": roi_hold,
                                     "notional": float(r["entry_notional"]), "src": m[2]})
    R["hold300_counterfactual"] = {
        k: {"covered/eligible": covered[k], "n": len(v),
            "actual_pnl": round(sum(x["actual_roi"] * x["notional"] for x in v), 2),
            "hold300_pnl_mid": round(sum(x["hold300_roi"] * x["notional"] for x in v), 2),
            "median_actual_roi_pct": round(100 * statistics.median([x["actual_roi"] for x in v]), 1),
            "median_hold300_roi_pct": round(100 * statistics.median([x["hold300_roi"] for x in v]), 1),
            "mark_sources": dict(sorted(((s, sum(x["src"] == s for x in v)) for s in {x["src"] for x in v})))}
        for k, v in cf.items() if v}
    R["hold300_note"] = ("mid-price at open+300 s (+/-30 s); a mid is not an executable exit, so the "
                         "counterfactual is an upper bound on what holding would have realised")

    # (3b) sizing within chain, and what drives the clip (risk_scale from the funded contract)
    R["size_split_by_chain"] = {c: compare(rs, lambda r: r["size_arm"], split_at, ["big", "small"])
                                for c, rs in by_chain.items() if len(rs) >= 40}
    R["risk_scale"] = compare(rows, lambda r: r["risk_scale"], split_at,
                              sorted({r["risk_scale"] for r in rows}, key=str))

    # (5b) EXPLORATORY, not pre-registered: exit-policy simulation on in-hold marks
    R["exit_sim"] = exit_sim(conn, rows, split_at)

    text = json.dumps(R, indent=1, default=str)
    if args.json:
        Path(args.json).write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
