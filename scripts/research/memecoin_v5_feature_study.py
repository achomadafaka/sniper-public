#!/usr/bin/env python3
"""Read-only V5 feature→forward-return study over the recorded 4663 tape.

Unlike the blind replay (which simulates the policy), this walks each position's FULL window on the
production `compute_state` at the production 30s cadence and, at every decision point, logs the
feature vector + what `propose_v5` said + the forward VWAP return at fixed horizons. Output ranks
evidence by measured relation to forward executable value — the input a tuning pass needs. It
answers three questions:
  1. Why do TP/DERISK/LP never fire? (feature availability / family trust / threshold reachability)
  2. Which features rank-correlate with forward returns? (discovery hints, not weights)
  3. At nominated structure breaks, what separates breaks-that-continue from dips-that-recover?

Decision points within a position are autocorrelated — pooled correlations are DISCOVERY hints
only; n_pos is reported next to every number. No DB writes; dataset CSV goes to --out.
"""

import sys
import pathlib as _research_pathlib
sys.path.insert(0, str(_research_pathlib.Path(__file__).resolve().parents[2]))
from sniper.research.wallet_execution.publication import legacy_launch as _legacy_launch
_legacy_launch(__name__)
import argparse
import csv
import os
import sys
from collections import Counter, defaultdict

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "scripts", "research"))
from dotenv import load_dotenv                                   # noqa: E402
load_dotenv(os.path.join(_ROOT, ".env"))

import numpy as np                                               # noqa: E402

from memecoin_v5_blind_replay import _ep, _policy_fields, _pool_for_mint      # noqa: E402
from sniper.analysis.exit_features import exec_price_path_per_block           # noqa: E402
from sniper.persistence import db_ticks as t                                  # noqa: E402
from sniper.services.memecoin_exit_brain import exit_engine                   # noqa: E402
from sniper.services.memecoin_exit_market_state import compute_state          # noqa: E402

MAX_HOLD_S = 12 * 3600.0
SWEEP_S = 30.0                       # MEMECOIN_EXIT_STATE_INTERVAL_S — production cadence
HORIZONS = (300, 900, 1800, 3600)    # forward-return horizons, seconds
CONTINUE_THRESH = -0.20              # fwd_1800 at/below this = break "continued"
RECOVER_THRESH = 0.0                 # fwd_1800 at/above this = "recovered"; between = gray, excluded

FEATURES = ("gain_from_entry", "drawdown_from_peak", "run_peak_gain", "break_depth", "vol_ratio",
            "arrival_rate", "arrival_accel", "net_flow", "buy_share", "held_s",
            "liquidity_removed_fraction")


def forward_labels(ts_arr, px_arr, i):
    """Pure forward-label math for point i on an (event-time, price) path.
    Horizon labels are None when the path ends before ts+h (censored)."""
    now_ts, now_px = ts_arr[i], px_arr[i]
    out = {}
    for h in HORIZONS:
        tgt = now_ts + h
        if ts_arr[-1] < tgt:
            out[f"fwd_{h}"] = None
            continue
        j = int(np.searchsorted(ts_arr, tgt, side="right")) - 1
        out[f"fwd_{h}"] = px_arr[j] / now_px - 1.0
    rest = px_arr[i:]
    out["fwd_end"] = px_arr[-1] / now_px - 1.0
    out["fwd_max"] = float(rest.max()) / now_px - 1.0
    out["fwd_min"] = float(rest.min()) / now_px - 1.0
    return out


def _num(x):
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def walk_position(tc, pos):
    """-> list of decision-point dicts, or {'skip': reason}."""
    mint = str(pos["mint"] or "").lower()
    pool, qs = _pool_for_mint(tc, mint, allow_multi=True)
    if not pool:
        return {"skip": "pool_unresolved"}
    t0 = _ep(pos["opened_at"])
    entry = float(pos["entry_price"] or 0)
    if not t0 or entry <= 0:
        return {"skip": "bad_entry_fields"}
    rows = [dict(r) for r in tc.execute(
        "SELECT kind, block_no, maker, amount0, amount1 FROM pool_ticks "
        "WHERE chain_id='4663' AND pool=? AND block_no IS NOT NULL ORDER BY block_no", (pool,))]
    if not rows:
        return {"skip": "no_tape"}
    bt = t.block_ts_map(tc, "4663", sorted({r["block_no"] for r in rows}))
    timed = []
    for r in rows:
        ep = _ep(bt.get(r["block_no"]))
        if ep is not None:
            timed.append({**r, "block_ts": ep})
    window = [r for r in timed if t0 <= r["block_ts"] <= t0 + MAX_HOLD_S]
    if len({r["block_no"] for r in window if r["kind"] in ("v2_swap", "v3_swap")}) < 5:
        return {"skip": "too_few_swap_blocks"}

    trail_pct, coin_band = _policy_fields(pos)
    contract = {"chain_id": "4663", "mint": mint, "pool": pool,
                "pool_kind": "v2" if any(r["kind"] in ("v2_swap", "v2_sync") for r in window) else "v3",
                "quote_side": qs, "position_id": pos["id"], "entry_price": entry,
                "trail_pct": trail_pct, "exact_quote_misses": 0, "coin_band": coin_band}
    _, proposer = exit_engine("v5")
    path = exec_price_path_per_block(
        [r for r in window if r["kind"] in ("v2_swap", "v3_swap")], quote_side=qs)
    if not path:
        return {"skip": "no_priced_blocks"}
    ts_arr = np.array([p[0] for p in path])
    px_arr = np.array([p[1] for p in path], dtype=float)
    bt_of = {blk: ts for ts, _px, _n, blk in path}
    bts = [blk for _ts, _px, _n, blk in path]

    grid, nxt_t = [], bt_of[bts[0]]
    for k, blk in enumerate(bts):
        if bt_of[blk] >= nxt_t:
            grid.append((k, blk))
            nxt_t = bt_of[blk] + SWEEP_S
    window.sort(key=lambda r: (r["block_ts"], r["block_no"]))
    censored = (ts_arr[-1] - t0) < MAX_HOLD_S - 600
    points, ptr = [], 0
    for k, blk in grid:
        bt_i = bt_of[blk]
        while ptr < len(window) and (window[ptr]["block_ts"], window[ptr]["block_no"]) <= (bt_i, blk):
            ptr += 1
        so_far = window[max(0, ptr - 5000):ptr]
        st = compute_state(so_far, {r["block_no"]: bt[r["block_no"]] for r in so_far},
                           contract, now_ts=bt_i)
        d = proposer({**contract, "held_s": bt_i - t0, "derisked_at": None}, st)
        prov = st.provenance or {}
        trusted = {f: getattr(prov.get(f), "quality", None) == "trusted"
                   for f in ("price", "flow", "vol", "depth")}
        px_now = px_arr[k]
        run_peak = float(px_arr[:k + 1].max())
        nominal, prior_low = st.nominal_price, st.prior_low
        brk = (nominal is not None and prior_low is not None and nominal < prior_low)
        net = _num(st.net_flow_quote)
        buy_n, sell_n = st.buy_n or 0, st.sell_n or 0
        vol_ratio = (None if st.realized_vol is None or not trail_pct
                     else float(st.realized_vol) / (float(trail_pct) / 100.0))
        pt = {
            "pos_id": pos["id"], "mint": mint, "held_s": bt_i - t0,
            "state": st.state.value, "action": d.action,
            "reason0": (d.reason_codes[0] if d.reason_codes else ""),
            "fam_price": trusted["price"], "fam_flow": trusted["flow"],
            "fam_vol": trusted["vol"], "fam_depth": trusted["depth"],
            "gain_from_entry": px_now / px_arr[0] - 1.0,
            "drawdown_from_peak": px_now / run_peak - 1.0,
            "run_peak_gain": run_peak / px_arr[0] - 1.0,
            "break_nominated": brk,
            "break_depth": ((prior_low - nominal) / prior_low if brk and prior_low else None),
            "vol_ratio": vol_ratio,
            "arrival_rate": _num(st.arrival_rate), "arrival_accel": _num(st.arrival_accel),
            "net_flow": net, "flow_negative": (net is not None and net < 0),
            "buy_share": (buy_n / (buy_n + sell_n) if (buy_n + sell_n) > 0 else None),
            "higher_low": st.higher_low_state, "lower_high": st.lower_high_state,
            "depth_pull": st.depth_pull_flag,
            "liquidity_removed_fraction": _num(st.liquidity_removed_fraction),
            "censored_pos": censored,
        }
        pt.update(forward_labels(ts_arr, px_arr, k))
        points.append(pt)
    return points


def _study_worker(pos):
    tc = t.connect_ticks_readonly()
    try:
        return walk_position(tc, pos)
    except Exception as e:
        return {"skip": f"worker_error:{type(e).__name__}"}
    finally:
        tc.close()


def spearman(x, y):
    """Rank correlation on paired non-None values. -> (rho, n)."""
    pairs = [(a, b) for a, b in zip(x, y) if a is not None and b is not None]
    if len(pairs) < 10:
        return None, len(pairs)
    a = np.array([p[0] for p in pairs], dtype=float)
    b = np.array([p[1] for p in pairs], dtype=float)
    ra = np.argsort(np.argsort(a)).astype(float)
    rb = np.argsort(np.argsort(b)).astype(float)
    sa, sb = ra.std(), rb.std()
    if sa == 0 or sb == 0:
        return None, len(pairs)
    return float(np.corrcoef(ra, rb)[0, 1]), len(pairs)


def auc(pos_vals, neg_vals):
    """P(random 'continued' value > random 'recovered' value) — Mann-Whitney U / (n1*n2)."""
    pos_vals = [v for v in pos_vals if v is not None]
    neg_vals = [v for v in neg_vals if v is not None]
    if len(pos_vals) < 5 or len(neg_vals) < 5:
        return None
    gt = sum(1 for p in pos_vals for q in neg_vals if p > q)
    eq = sum(1 for p in pos_vals for q in neg_vals if p == q)
    return (gt + 0.5 * eq) / (len(pos_vals) * len(neg_vals))


def analyze(points):
    n_pos = len({p["pos_id"] for p in points})
    print(f"\n{'=' * 72}\nDATASET: {len(points)} decision points across {n_pos} positions")

    print("\n--- 1. DEAD-RULE DIAGNOSIS ---")
    print("state labels:", dict(Counter(p["state"] for p in points).most_common()))
    print("actions:     ", dict(Counter(p["action"] for p in points).most_common()))
    for f in ("fam_price", "fam_flow", "fam_vol", "fam_depth"):
        share = sum(1 for p in points if p[f]) / len(points)
        print(f"  {f} trusted: {share:.1%}")
    for f in ("vol_ratio", "arrival_accel", "higher_low", "lower_high", "net_flow",
              "liquidity_removed_fraction"):
        avail = sum(1 for p in points if p[f] is not None) / len(points)
        print(f"  {f} available: {avail:.1%}")
    vr = [p["vol_ratio"] for p in points if p["vol_ratio"] is not None]
    if vr:
        vr = np.array(vr)
        print(f"  vol_ratio p50={np.percentile(vr, 50):.3f} p90={np.percentile(vr, 90):.3f} "
              f"p99={np.percentile(vr, 99):.3f} max={vr.max():.3f}  "
              f"share>=1.0 (blowoff): {(vr >= 1.0).mean():.2%}")
    hl_true = sum(1 for p in points if p["higher_low"] is True) / len(points)
    acc_pos = sum(1 for p in points if (p["arrival_accel"] or 0) > 0) / len(points)
    tp_joint = sum(1 for p in points if p["higher_low"] is True and (p["arrival_accel"] or 0) > 0
                   and p["vol_ratio"] is not None and p["vol_ratio"] >= 1.0) / len(points)
    print(f"  higher_low=True: {hl_true:.1%}  arrival_accel>0: {acc_pos:.1%}  "
          f"TP precondition joint (hl & accel>0 & blowoff): {tp_joint:.2%}")

    print("\n--- 2. FEATURE -> FORWARD-RETURN RANK CORRELATION (pooled; discovery only) ---")
    print(f"{'feature':<28}{'fwd_1800':>12}{'n':>8}{'fwd_end':>12}{'n':>8}")
    for f in FEATURES:
        r30, n30 = spearman([p[f] for p in points], [p["fwd_1800"] for p in points])
        rend, nend = spearman([p[f] for p in points], [p["fwd_end"] for p in points])
        fmt = lambda r: "     -" if r is None else f"{r:+.3f}"
        print(f"{f:<28}{fmt(r30):>12}{n30:>8}{fmt(rend):>12}{nend:>8}")

    print("\n--- 3. BREAK DISCRIMINATOR (points where the firing rule holds: break & flow<0) ---")
    brk = [p for p in points if p["break_nominated"] and p["flow_negative"]
           and p["fwd_1800"] is not None]
    cont = [p for p in brk if p["fwd_1800"] <= CONTINUE_THRESH]
    rec = [p for p in brk if p["fwd_1800"] >= RECOVER_THRESH]
    n_bp = len({p["pos_id"] for p in brk})
    print(f"break+neg-flow points: {len(brk)} ({n_bp} positions) -> "
          f"continued(<= {CONTINUE_THRESH:+.0%}): {len(cont)}  "
          f"recovered(>= {RECOVER_THRESH:+.0%}): {len(rec)}  gray: {len(brk) - len(cont) - len(rec)}")
    if cont and rec:
        print(f"{'feature':<28}{'AUC(cont>rec)':>14}{'med cont':>12}{'med rec':>12}")
        for f in FEATURES:
            a = auc([p[f] for p in cont], [p[f] for p in rec])
            mc = np.median([p[f] for p in cont if p[f] is not None]) if any(
                p[f] is not None for p in cont) else float("nan")
            mr = np.median([p[f] for p in rec if p[f] is not None]) if any(
                p[f] is not None for p in rec) else float("nan")
            print(f"{f:<28}{('     -' if a is None else f'{a:.3f}'):>14}{mc:>12.4g}{mr:>12.4g}")
        base = len(cont) / (len(cont) + len(rec))
        print(f"base rate P(continued | break & flow<0, ex-gray) = {base:.1%}")


CAT_DRAWDOWN = -0.50    # catastrophe label: forward executable drawdown from the break
GROUPED_FEATURES = ("arrival_rate", "held_s", "drawdown_from_peak", "buy_share", "vol_ratio",
                    "arrival_accel")


def weighted_auc(pairs_pos, pairs_neg):
    """Weighted Mann-Whitney AUC in O(n log n), with half credit for ties."""
    total_pos = sum(w for _v, w in pairs_pos)
    total_neg = sum(w for _v, w in pairs_neg)
    if total_pos <= 0 or total_neg <= 0:
        return None
    combined = sorted([(float(v), 1, float(w)) for v, w in pairs_pos]
                      + [(float(v), 0, float(w)) for v, w in pairs_neg])
    neg_before = wins = 0.0
    i = 0
    while i < len(combined):
        j = i + 1
        while j < len(combined) and combined[j][0] == combined[i][0]:
            j += 1
        group = combined[i:j]
        pos_w = sum(w for _v, is_pos, w in group if is_pos)
        neg_w = sum(w for _v, is_pos, w in group if not is_pos)
        wins += pos_w * (neg_before + 0.5 * neg_w)
        neg_before += neg_w
        i = j
    return wins / (total_pos * total_neg)


def grouped_catastrophe_auc(csv_path):
    """Reproducible generator for the episode-grouped catastrophe-label AUCs (Codex review
    2026-07-26 — these numbers were previously only produced ad hoc). Label: fwd_min <= -50%
    (forward drawdown from the break point, no gray zone). Weighting: each episode contributes ONE
    unit per class, split across its points; LOEO = leave-one-episode-out range."""
    import csv as _csv

    def fl(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    rows = list(_csv.DictReader(open(csv_path)))
    brk = [r for r in rows if r["break_nominated"] == "True" and r["flow_negative"] == "True"
           and fl(r["fwd_min"]) is not None]
    for r in brk:
        r["_cat"] = fl(r["fwd_min"]) <= CAT_DRAWDOWN
    byep = defaultdict(list)
    for r in brk:
        byep[r["pos_id"]].append(r)
    n_cat = sum(1 for r in brk if r["_cat"])
    print(f"break+neg-flow points: {len(brk)} ({len(byep)} episodes)  "
          f"catastrophic: {n_cat} ({n_cat / len(brk):.1%})")

    print(f"{'feature':<22}{'grouped AUC':>12}{'LOEO min':>10}{'LOEO max':>10}")
    for f in GROUPED_FEATURES:
        def build(exclude=None):
            pos, neg = [], []
            for pid, pts in byep.items():
                if pid == exclude:
                    continue
                cs = [fl(r[f]) for r in pts if r["_cat"] and fl(r[f]) is not None]
                bs = [fl(r[f]) for r in pts if not r["_cat"] and fl(r[f]) is not None]
                pos += [(v, 1.0 / len(cs)) for v in cs] if cs else []
                neg += [(v, 1.0 / len(bs)) for v in bs] if bs else []
            return weighted_auc(pos, neg)
        full = build()
        loeo = [x for x in (build(exclude=pid) for pid in byep) if x is not None]
        print(f"{f:<22}{full:>12.3f}{min(loeo):>10.3f}{max(loeo):>10.3f}")


def self_check():
    ts = np.array([0.0, 100.0, 300.0, 900.0, 2000.0])
    px = np.array([1.0, 1.1, 0.9, 0.5, 2.0])
    lab = forward_labels(ts, px, 0)
    assert abs(lab["fwd_300"] - (0.9 / 1.0 - 1)) < 1e-9, lab["fwd_300"]
    assert abs(lab["fwd_900"] - (0.5 / 1.0 - 1)) < 1e-9
    assert lab["fwd_3600"] is None                      # censored: path ends at 2000
    assert abs(lab["fwd_max"] - 1.0) < 1e-9 and abs(lab["fwd_min"] - (-0.5)) < 1e-9
    r, _n = spearman(list(range(20)), list(range(20)))
    assert abs(r - 1.0) < 1e-9
    assert auc([2] * 6, [1] * 6) == 1.0 and auc([1] * 6, [1] * 6) == 0.5
    print("self-check OK")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(_ROOT, "data", "research",
                                                  "v5_feature_points_4663.csv"))
    ap.add_argument("--self-check", action="store_true")
    ap.add_argument("--grouped-auc", action="store_true",
                    help="reproduce the episode-grouped catastrophe-label AUC table from the "
                         "existing dataset CSV (no walk)")
    args = ap.parse_args()
    if args.self_check:
        self_check()
        return
    if args.grouped_auc:
        grouped_catastrophe_auc(args.out)
        return

    from multiprocessing import Pool

    from sniper.persistence.db_core import get_readonly_connection
    from sniper.util.state_paths import state_root
    tc = t.connect_ticks_readonly()
    m = get_readonly_connection(str(state_root() / "sniper_tracker.db"))
    w0 = tc.execute("SELECT MIN(observed_at) FROM pool_ticks WHERE chain_id='4663'").fetchone()[0]
    # one position per mint (earliest) — control/real_shadow mirror the same entries; walking both
    # would double-weight every coin's window in the pooled stats
    # SQLite min-aggregate guarantee: bare columns come from the row holding MIN(opened_at)
    poss = [dict(r) for r in m.execute(
        "SELECT id, mint, strategy, MIN(opened_at) AS opened_at, entry_price, entry_notional,"
        "       realized_pnl, entry_mcap,"
        "       funded_contract_version, funded_contract_hash, funded_contract_json "
        "FROM memecoin_positions WHERE network_id='4663' AND is_open=0 "
        "AND strategy IN ('control','real_shadow') AND opened_at >= ? "
        "GROUP BY mint ORDER BY opened_at", (w0,))]
    print(f"eligible unique-mint positions: {len(poss)}")
    points, skips = [], Counter()
    with Pool(24) as workers:
        for r in workers.map(_study_worker, poss):
            if isinstance(r, dict):
                skips[r["skip"]] += 1
            else:
                points.extend(r)
    print(f"skips: {dict(skips)}")
    if not points:
        print("no decision points collected")
        return
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=list(points[0].keys()))
        wr.writeheader()
        wr.writerows(points)
    print(f"wrote {len(points)} points -> {args.out}")
    analyze(points)



# Operator protocol: legacy hypothetical conclusions cannot publish by default.
from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports
_quarantine_exports(globals(), ('analyze', 'main'))

if __name__ == "__main__":
    main()
