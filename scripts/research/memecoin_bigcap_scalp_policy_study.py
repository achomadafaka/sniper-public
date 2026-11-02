#!/usr/bin/env python3
"""
Big-cap scalp-exit-policy study (2026-07-14, feature-research-analyst).

Corrects the scope error in scripts/research/memecoin_exit_policy_conditioning.py
(docs/research/2026-07-14-memecoin-exit-policy-conditioning-viability.md), whose sample
was 66/67 non-curated firehose entries. THIS study uses the correct cohort: CURATED-wallet
copies (memecoin_positions WHERE execution_mode='paper'), which already enter big-cap coins
with no mcap gate (the gate lives only on the real leg).

Question (operator's hypothesis): is "coin worth entering" CONDITIONAL on exit policy? A
big-cap coin will rarely pay +60% (the fixed TP), so it may look worthless under the fixed rail,
but might be +EV under a scalp policy (small TP, tight stop, short hold). Test with a PAIRED
mint-grain design: the earliest curated entry per mint, one fixed 24h forward path independent of
existing-arm close times, fixed rail vs every alternative policy in the grid.

Read-only against sniper_tracker.db. Reuses sniper.replay.memecoin.slippage_haircut for the
cost haircut and sniper.services.memecoin_policy.ExitPolicy/control_reason for tp/stop trigger
semantics (max_hold / trailing stop are NOT in the v1 runner rails -- documented extensions for
the grid search only, same convention as the prior study).

Usage:
    python3 scripts/research/memecoin_bigcap_scalp_policy_study.py --db sniper_tracker.db
"""
from __future__ import annotations

import sys
import pathlib as _research_pathlib
sys.path.insert(0, str(_research_pathlib.Path(__file__).resolve().parents[2]))
from sniper.research.wallet_execution.publication import legacy_launch as _legacy_launch
_legacy_launch(__name__)

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from sniper.replay.memecoin import slippage_haircut  # noqa: E402

MIN_MARKS = 5           # position must have >=5 real forward marks (post-entry) to be usable
MAX_GAIN_SANITY_PCT = 2000.0   # same convention as memecoin_feature_store.py -- drop quote glitches
N_BOOT = 5000
DEFAULT_HORIZON_S = 24 * 3600

TP_GRID = [5, 8, 10, 15, 20, 25, 30, 40, 60, 80, 100, None]
STOP_GRID = [-15, -20, -25, -30, -40, -50, -70, None]
HOLD_GRID = [900, 1800, 3600, 10800, 21600, 43200, None]  # 15m,30m,1h,3h,6h,12h,none
TRAIL_GRID = [("off", None), ("trail_15", 15), ("trail_25", 25)]
BASELINE_KEY = (60, -50, None, "off")   # current fixed real-leg rail, MEMECOIN_REAL_TP_PCT/STOP_PCT

MCAP_BANDS = [
    (30_000_000, float("inf"), ">=30M"),
    (5_000_000, 30_000_000, "5-30M"),
    (1_000_000, 5_000_000, "1-5M"),
    (0, 1_000_000, "<1M"),
]


def band_of(mcap):
    for lo, hi, name in MCAP_BANDS:
        if lo <= mcap < hi:
            return name
    return "<1M"


def load_positions_with_band(conn) -> pd.DataFrame:
    """One row per paper position with its signal-time market cap: primary = the triggering
    event (origin_event_id, no lookahead), fallback = nearest PRECEDING event on the same mint
    (created_at <= opened_at) for the ~7% of positions missing origin_event_id. Coin-level band
    = the band of that coin's EARLIEST position (avoids a coin splitting across bands if it
    pumped after our first entry -- "what band was this coin in when we started following it").
    """
    direct = pd.read_sql(
        """
        SELECT p.id AS pos_id, p.mint, p.network_id, p.entry_price, p.opened_at, p.closed_at,
               p.is_open, p.strategy, p.source_user_id, e.their_market_cap AS mcap
        FROM memecoin_positions p
        JOIN memecoin_events e ON e.fomo_event_id = p.origin_event_id
        WHERE p.execution_mode='paper' AND e.their_market_cap IS NOT NULL AND p.entry_price > 0
        """, conn)
    have = set(direct["pos_id"])
    fallback = pd.read_sql(
        """
        SELECT p.id AS pos_id, p.mint, p.network_id, p.entry_price, p.opened_at, p.closed_at,
               p.is_open, p.strategy, p.source_user_id, e.their_market_cap AS mcap,
               ROW_NUMBER() OVER (PARTITION BY p.id
                                   ORDER BY julianday(p.opened_at) - julianday(e.created_at)) rn
        FROM memecoin_positions p
        JOIN memecoin_events e ON e.mint = p.mint AND e.created_at <= p.opened_at
        WHERE p.execution_mode='paper' AND e.their_market_cap IS NOT NULL AND p.entry_price > 0
        """, conn)
    fallback = fallback[(fallback["rn"] == 1) & (~fallback["pos_id"].isin(have))].drop(columns=["rn"])
    df = pd.concat([direct, fallback], ignore_index=True)
    df["opened_epoch"] = df["opened_at"].map(_iso_to_epoch)
    # coin-level band = band of the coin's earliest position
    first_mcap = (df.sort_values("opened_epoch").groupby("mint", as_index=False).first()
                    [["mint", "mcap"]].rename(columns={"mcap": "coin_first_mcap"}))
    df = df.merge(first_mcap, on="mint", how="left")
    df["mcap_band"] = df["coin_first_mcap"].map(band_of)
    return df


def select_mint_entries(positions: pd.DataFrame) -> pd.DataFrame:
    """Freeze the experimental unit before simulation: one earliest entry per mint.

    Simulating every strategy position and averaging afterward is not equivalent. Each existing
    arm closes at a different time, so its row exposes a different counterfactual path and leaks
    baseline-arm behavior into policy selection.
    """
    return (positions.sort_values(["opened_epoch", "pos_id"])
            .drop_duplicates("mint", keep="first")
            .sort_values(["opened_epoch", "pos_id"])
            .reset_index(drop=True))


def _iso_to_epoch(s):
    if s is None:
        return None
    v = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    if v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    return v.timestamp()


def load_marks_index(conn) -> dict:
    """One pass load of memecoin_position_marks, grouped by mint (chain_id NOT used as a join
    key -- verified zero mints collide across chain_id in this DB snapshot, and some EVM
    positions carry network_id='evm-unknown' while their marks are correctly chain-tagged, so a
    (mint,chain_id) join silently drops real data for those rows)."""
    df = pd.read_sql("SELECT mint, sweep_ts, mid FROM memecoin_position_marks "
                      "WHERE mid IS NOT NULL AND mid > 0", conn)
    df["ts"] = df["sweep_ts"].map(_iso_to_epoch)
    df = df.dropna(subset=["ts"])
    out = {}
    for mint, g in df.groupby("mint", sort=False):
        g = g.sort_values("ts")
        out[mint] = (g["ts"].to_numpy(dtype=float), g["mid"].to_numpy(dtype=float))
    return out


def build_position_paths(positions: pd.DataFrame, marks_idx: dict, *,
                         horizon_s=DEFAULT_HORIZON_S) -> tuple[pd.DataFrame, dict]:
    """For each mint entry, window marks to one fixed forward horizon.

    Never truncate at the existing arm's actual close: that would give candidate policies a path
    chosen by the baseline strategy. Prepend entry price as mark 0 (elapsed=0s), same convention as
    tests/test_memecoin_replay.py::test_acceptance_replay_reproduces_realized_pnl. Requires
    >=MIN_MARKS real forward marks. Applies the MAX_GAIN_SANITY_PCT unit-mismatch guard."""
    rows = []
    diag = {"positions_in": len(positions), "no_mark_series": 0, "too_few_marks": 0,
            "sanity_cap_dropped": 0, "kept": 0}
    for _, r in positions.iterrows():
        arr = marks_idx.get(r["mint"])
        if arr is None:
            diag["no_mark_series"] += 1
            continue
        ts_arr, px_arr = arr
        start = r["opened_epoch"]
        end = start + float(horizon_s)
        mask = (ts_arr >= start) & (ts_arr <= end)
        m_ts, m_px = ts_arr[mask], px_arr[mask]
        if len(m_px) < MIN_MARKS:
            diag["too_few_marks"] += 1
            continue
        max_gain_pct = (m_px.max() / r["entry_price"] - 1.0) * 100.0
        if max_gain_pct > MAX_GAIN_SANITY_PCT:
            diag["sanity_cap_dropped"] += 1
            continue
        elapsed = np.concatenate([[0.0], m_ts - start])
        price = np.concatenate([[r["entry_price"]], m_px])
        diag["kept"] += 1
        row = r.to_dict()
        row["elapsed"] = elapsed
        row["price"] = price
        row["n_marks"] = len(m_px)
        rows.append(row)
    return pd.DataFrame(rows), diag


def simulate_grid(entry_price, elapsed, price, haircut):
    """Vectorized policy grid over one position's forward path. tp/stop mirror
    sniper.services.memecoin_policy.control_reason (checked together on each mark, first-hit
    wins in grid order); max_hold and trailing-stop are grid-search-only extensions (documented,
    not in the v1 runner). Fill = NEXT mark after trigger, minus haircut -- same convention as
    sniper.replay.memecoin.replay_position."""
    pnl_pct = (price / entry_price - 1.0) * 100.0
    running_peak = np.maximum.accumulate(price)
    dd_pct = (price / running_peak - 1.0) * 100.0

    tp_arr = np.array([t if t is not None else 1e12 for t in TP_GRID])
    stop_arr = np.array([s if s is not None else -1e12 for s in STOP_GRID])
    hold_arr = np.array([h if h is not None else 1e18 for h in HOLD_GRID])
    trail_arr = np.array([t[1] if t[1] is not None else 1e12 for t in TRAIL_GRID])

    tp_hit = pnl_pct[:, None] >= tp_arr[None, :]
    stop_hit = pnl_pct[:, None] <= stop_arr[None, :]
    hold_hit = elapsed[:, None] >= hold_arr[None, :]
    trail_hit = dd_pct[:, None] <= -trail_arr[None, :]

    M = len(price)
    T, S, H, Tr = len(TP_GRID), len(STOP_GRID), len(HOLD_GRID), len(TRAIL_GRID)
    trig = (tp_hit[:, :, None, None, None] | stop_hit[:, None, :, None, None]
            | hold_hit[:, None, None, :, None] | trail_hit[:, None, None, None, :])
    trig = trig.reshape(M, T * S * H * Tr)
    any_trig = trig.any(axis=0)
    first_idx = trig.argmax(axis=0)
    fill_idx = np.where(any_trig, np.minimum(first_idx + 1, M - 1), M - 1)
    fill_price = price[fill_idx] * (1.0 - haircut)
    net_return = fill_price / entry_price - 1.0
    open_end = ~any_trig
    return net_return.reshape(T, S, H, Tr), open_end.reshape(T, S, H, Tr)


POLICY_KEYS = [(tp, stop, hold, trail_name) for tp in TP_GRID for stop in STOP_GRID
               for hold in HOLD_GRID for trail_name, _ in TRAIL_GRID]
N_POL = len(POLICY_KEYS)
BASELINE_IDX = POLICY_KEYS.index(BASELINE_KEY)


def policy_str(k):
    tp, stop, hold, trail = k
    return (f"tp={tp if tp is not None else 'none'} stop={stop if stop is not None else 'none'} "
            f"hold={hold if hold is not None else 'none'} trail={trail}")


def bootstrap_ci(values, n_boot=N_BOOT, seed=0):
    arr = np.asarray(values, dtype=float)
    if len(arr) == 0:
        return (float("nan"),) * 3
    rng = np.random.default_rng(seed)
    means = np.empty(n_boot)
    for i in range(n_boot):
        means[i] = rng.choice(arr, size=len(arr), replace=True).mean()
    lo, hi = np.percentile(means, [2.5, 97.5])
    return (float(arr.mean()), float(lo), float(hi))


def paired_test(diff: np.ndarray) -> dict:
    n = len(diff)
    mean, lo, hi = bootstrap_ci(diff)
    se = diff.std(ddof=1) / np.sqrt(n) if n > 1 else float("nan")
    t = mean / se if se and se > 0 else float("nan")
    return {"n": n, "mean_diff": mean, "se": se, "t": t, "ci_lo": lo, "ci_hi": hi}


def loo_best_policy(R_mint: np.ndarray) -> dict:
    """Leave-one-out by MINT: fit argmax-mean policy on all other mints, score the held-out
    mint under it. Reports OOS mean + modal policy + stability (how often the LOO-selected
    policy agrees with the modal choice)."""
    n = R_mint.shape[0]
    if n < 2:
        return {"oos_mean": float("nan"), "modal_policy": None, "stability": float("nan"),
                "chosen_idx": np.array([], dtype=int)}
    oos = np.empty(n)
    chosen = np.empty(n, dtype=int)
    for i in range(n):
        train = np.ones(n, dtype=bool)
        train[i] = False
        best = int(np.argmax(R_mint[train].mean(axis=0)))
        chosen[i] = best
        oos[i] = R_mint[i, best]
    vals, counts = np.unique(chosen, return_counts=True)
    modal = POLICY_KEYS[vals[np.argmax(counts)]]
    stability = counts.max() / n
    return {"oos_mean": float(oos.mean()), "oos_return": oos, "modal_policy": modal,
            "stability": float(stability), "chosen_idx": chosen}


def mde_for_paired_t(diff: np.ndarray, power=0.80, alpha=0.05) -> float:
    """Approx MDE (in return units) for a paired t-test at observed n and observed within-mint
    std of the paired difference, using the normal approximation z_(1-a/2)+z_power (adequate for
    a power-planning estimate, not a formal test)."""
    from scipy import stats
    n = len(diff)
    if n < 2:
        return float("nan")
    sd = diff.std(ddof=1)
    z_a = stats.norm.ppf(1 - alpha / 2)
    z_p = stats.norm.ppf(power)
    return (z_a + z_p) * sd / np.sqrt(n)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="sniper_tracker.db")
    ap.add_argument("--out-prefix", default="docs/research/assets/2026-07-14-memecoin-bigcap-scalp")
    ap.add_argument("--horizon-h", type=float, default=24.0)
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    haircut = slippage_haircut(conn, "any")
    print(f"slippage haircut in use: {haircut*100:.2f}% (memecoin_exec_latency.impact_pct p75)")

    positions = load_positions_with_band(conn)
    print(f"paper positions with a resolvable signal-time mcap: {len(positions)}")
    print("coin-level band counts (distinct mint):")
    print(positions.drop_duplicates("mint")["mcap_band"].value_counts())
    print("position counts per band:")
    print(positions["mcap_band"].value_counts())

    marks_idx = load_marks_index(conn)
    print(f"\nloaded mark series for {len(marks_idx)} distinct mints")

    mint_entries = select_mint_entries(positions)
    print(f"structural mint-grain entries before path coverage: {len(mint_entries)}")
    paths, diag = build_position_paths(
        mint_entries, marks_idx, horizon_s=args.horizon_h * 3600.0,
    )
    print(f"\nposition-path build diagnostics: {diag}")
    if paths.empty:
        raise SystemExit("no usable position paths")

    print("\nusable mint entries (>=%d marks, sanity-capped) by band:" % MIN_MARKS)
    print(paths["mcap_band"].value_counts())
    print("\ndistinct coins by band (structural mint grain):")
    print(paths["mcap_band"].value_counts())

    # --- simulate full grid for every usable position ---
    n = len(paths)
    R = np.empty((n, N_POL))
    OE = np.empty((n, N_POL), dtype=bool)
    for i, (_, r) in enumerate(paths.iterrows()):
        ret, oe = simulate_grid(r["entry_price"], r["elapsed"], r["price"], haircut)
        R[i] = ret.reshape(-1)
        OE[i] = oe.reshape(-1)

    paths_out = paths.drop(columns=["elapsed", "price"])
    os.makedirs(os.path.dirname(args.out_prefix), exist_ok=True)
    paths_out.to_csv(args.out_prefix + "-mints.csv", index=False)
    R_mint = R
    paths = paths.reset_index(drop=True)
    mints = paths["mint"].tolist()
    mint_bands = paths["mcap_band"].to_numpy()
    counts_by_mint = positions.groupby("mint").size().to_dict()
    mint_n_positions = np.array([counts_by_mint[mint] for mint in mints], dtype=float)
    np.save(args.out_prefix + "-Rmatrix-mint.npy", R_mint)
    print(f"\nmint-grain matrix: {R_mint.shape[0]} coins x {N_POL} policies "
          f"-> {args.out_prefix}-mints.csv (+ Rmatrix-mint.npy)")
    print("coins per band (mint-grain, final usable cohort):")
    print(pd.Series(mint_bands).value_counts())

    # ============================================================
    # (a) >=30M cohort: best policy, paired vs baseline, +EV under ANY policy?
    # ============================================================
    print("\n" + "=" * 70)
    print("(a) >=30M COHORT -- the operator's question")
    print("=" * 70)
    for band in [">=30M", "5-30M", "1-5M", "<1M"]:
        mask = mint_bands == band
        Rb = R_mint[mask]
        nb = Rb.shape[0]
        print(f"\n--- band {band}: n={nb} coins ({int(mint_n_positions[mask].sum())} positions) ---")
        if nb == 0:
            continue
        baseline_ret = Rb[:, BASELINE_IDX]
        bmean, blo, bhi = bootstrap_ci(baseline_ret)
        print(f"  fixed baseline (tp60/stop-50/hold-none/trail-off): mean={bmean*100:6.1f}%  "
              f"95%CI=[{blo*100:.1f}%,{bhi*100:.1f}%]  win_rate={(baseline_ret>0).mean()*100:.0f}%")

        in_sample_best = int(np.argmax(Rb.mean(axis=0)))
        is_mean = Rb[:, in_sample_best].mean()
        print(f"  IN-SAMPLE best policy (INFLATED, same-data fit+eval): "
              f"{policy_str(POLICY_KEYS[in_sample_best])}  mean={is_mean*100:6.1f}%")

        if nb >= 2:
            loo = loo_best_policy(Rb)
            print(f"  LOO-OOS best-policy mean={loo['oos_mean']*100:6.1f}%  "
                  f"modal_policy={policy_str(loo['modal_policy'])}  stability={loo['stability']*100:.0f}%")
            diff_loo_vs_base = loo["oos_return"] - baseline_ret
            pt = paired_test(diff_loo_vs_base)
            print(f"  PAIRED: LOO-OOS-best vs fixed-baseline  mean_diff={pt['mean_diff']*100:6.1f}%  "
                  f"se={pt['se']*100:.1f}%  t={pt['t']:.2f}  95%CI=[{pt['ci_lo']*100:.1f}%,{pt['ci_hi']*100:.1f}%]  n={pt['n']}")

        # explicit scalp candidates (operator's specific hypothesis)
        scalp_candidates = [(tp, stop, hold, "off") for tp in (8, 10, 15, 20)
                             for stop in (-15, -20, -25) for hold in (1800, 3600, 10800)]
        scalp_idx = [POLICY_KEYS.index(k) for k in scalp_candidates if k in POLICY_KEYS]
        scalp_means = Rb[:, scalp_idx].mean(axis=0)
        best_scalp_j = scalp_idx[int(np.argmax(scalp_means))]
        best_scalp_ret = Rb[:, best_scalp_j]
        pt_scalp = paired_test(best_scalp_ret - baseline_ret)
        print(f"  BEST explicit scalp candidate (in-sample argmax over tp in "
              f"{{8,10,15,20}}/stop in {{-15,-20,-25}}/hold in {{30m,1h,3h}}): "
              f"{policy_str(POLICY_KEYS[best_scalp_j])}  mean={Rb[:, best_scalp_j].mean()*100:6.1f}%")
        print(f"  PAIRED: best-scalp vs fixed-baseline  mean_diff={pt_scalp['mean_diff']*100:6.1f}%  "
              f"se={pt_scalp['se']*100:.1f}%  t={pt_scalp['t']:.2f}  "
              f"95%CI=[{pt_scalp['ci_lo']*100:.1f}%,{pt_scalp['ci_hi']*100:.1f}%]")

        # is the cohort +EV under ANY policy, in-sample?
        n_pos_ev = int((Rb.mean(axis=0) > 0).sum())
        print(f"  policies with positive in-sample mean return: {n_pos_ev}/{N_POL} "
              f"(best in-sample mean={Rb.mean(axis=0).max()*100:.1f}%)")

        # (b) best TP marginal, holding stop/hold/trail at grid-best conditional on tp
        print("  best-TP marginal (in-sample, argmax over stop/hold/trail for each tp):")
        tp_best = {}
        for j, tp in enumerate(TP_GRID):
            idxs = [k for k, key in enumerate(POLICY_KEYS) if key[0] == tp]
            sub = Rb[:, idxs]
            best_k = idxs[int(np.argmax(sub.mean(axis=0)))]
            tp_best[tp] = Rb[:, best_k].mean()
            print(f"    tp={tp if tp is not None else 'none':>4}  best_conditional_mean={Rb[:, best_k].mean()*100:6.1f}%  "
                  f"({policy_str(POLICY_KEYS[best_k])})")

    # ============================================================
    # (d) power statement, computed on the >=30M cohort's scalp-vs-baseline paired diff
    # ============================================================
    mask30 = mint_bands == ">=30M"
    Rb30 = R_mint[mask30]
    if Rb30.shape[0] >= 2:
        baseline_ret30 = Rb30[:, BASELINE_IDX]
        loo30 = loo_best_policy(Rb30)
        diff30 = loo30["oos_return"] - baseline_ret30
        mde = mde_for_paired_t(diff30)
        obs_sd = diff30.std(ddof=1)
        print("\n" + "=" * 70)
        print("(d) POWER STATEMENT -- >=30M cohort")
        print("=" * 70)
        print(f"  n coins={len(diff30)}  observed paired-diff mean={diff30.mean()*100:.1f}%  "
              f"sd={obs_sd*100:.1f}%")
        print(f"  MDE @ 80% power, alpha=0.05, this n and this observed sd: {mde*100:.1f} pct points")
        for target_n in (30, 50, 100):
            from scipy import stats
            z_a = stats.norm.ppf(0.975)
            z_p = stats.norm.ppf(0.80)
            mde_n = (z_a + z_p) * obs_sd / np.sqrt(target_n)
            print(f"  MDE @ n={target_n}: {mde_n*100:.1f} pct points")

    # ============================================================
    # (c) curated-cohort baseline reconciliation
    # ============================================================
    print("\n" + "=" * 70)
    print("(c) CURATED BASELINE RECONCILIATION")
    print("=" * 70)
    strat_pnl = pd.read_sql(
        "SELECT strategy, COUNT(*) n_closed, SUM(realized_pnl) total_pnl, AVG(realized_pnl) avg_pnl, "
        "SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END)*1.0/COUNT(*) win_rate "
        "FROM memecoin_positions WHERE execution_mode='paper' AND is_open=0 GROUP BY strategy", conn)
    print(strat_pnl.to_string(index=False))
    ctrl_3d = pd.read_sql(
        "SELECT COUNT(*) n_closed, SUM(realized_pnl) total_pnl FROM memecoin_positions "
        "WHERE execution_mode='paper' AND strategy='control' AND is_open=0 "
        "AND closed_at >= datetime('now','-3 days')", conn)
    print("\ncontrol arm, last 3 days:")
    print(ctrl_3d.to_string(index=False))



# Operator protocol: legacy hypothetical conclusions cannot publish by default.
from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports
_quarantine_exports(globals(), ('main',))

if __name__ == "__main__":
    main()
