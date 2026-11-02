#!/usr/bin/env python3
"""Robustness re-evaluation of the tape predictive study (2026-07-24) against a
reviewer objection: overlapping rolling windows inflate nominal n (rows share
almost all their content with neighbors), so the label-shuffle permutation
null (z=53-177) doesn't test autocorrelation, and "100% of pools AUC>0.5" may
be one shared market-regime factor rather than 254 independent votes.

Evaluation-only: reuses the EXISTING feature matrix
(docs/research/assets/memecoin_tape_predictive_features_2026-07-24.csv), same
model classes/hyperparameters as the original study, same train/test cutoffs.
The only new methodology is the evaluation protocol:

1. Purged/embargoed test set: drop test rows within (390-block longest window
   + the target's forward horizon) blocks of the train/test boundary.
2. Recompute OOS corr / per-pool AUC on the purged set.
3. Block bootstrap (not label-shuffle): resample CONTIGUOUS per-pool chunks
   of length >= purge_gap, with replacement, to get a 95% CI that respects
   within-chunk autocorrelation.
4. Split the purged test period into 4 sub-windows, report per-window median
   per-pool AUC/corr — a regime-factor artifact should collapse in some
   sub-windows.

Models are fit ONCE per target (same HGB hyperparams as the original study)
to obtain predictions; no new features, no iterative refitting/tuning.
"""
from __future__ import annotations

import sys
import pathlib as _research_pathlib
sys.path.insert(0, str(_research_pathlib.Path(__file__).resolve().parents[2]))
from sniper.research.wallet_execution.publication import legacy_launch as _legacy_launch
_legacy_launch(__name__)

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.metrics import roc_auc_score

REPO = Path(__file__).resolve().parents[2]

# Constants are IMPORTED from the study, never restated here. The first version of this file
# duplicated them (WINDOWS_BLOCKS=[26,130,390], LONGEST_WINDOW=390, TARGETS=fwd_ret_130/390/hit20_780).
# After the cadence correction those literals would have set a purge gap of ~52 s instead of ~45 min
# — the check would have rubber-stamped precisely the overlap it exists to test (Codex 2026-07-24).
sys.path.insert(0, str(REPO))
import importlib.util as _ilu

_spec = _ilu.spec_from_file_location(
    "predictive_study", REPO / "scripts" / "research" / "memecoin_tape_predictive_study.py")
_study = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_study)

WINDOWS_BLOCKS = _study.WINDOWS_BLOCKS
LONGEST_WINDOW = max(WINDOWS_BLOCKS)
FEATURE_COLS = ([f"{m}_{w}" for m in ["net_flow", "buy_frac", "arrival_rate", "vol"]
                 for w in WINDOWS_BLOCKS]
                + [f"accel_{WINDOWS_BLOCKS[0]}", "reserve_quote", "reserve_delta"])
TARGETS = ([(name, "reg", blocks) for name, blocks in _study.HORIZONS_BLOCKS.items()]
           + [(_study.HIT_COL, "clf", _study.HIT_HORIZON_BLOCKS)])

ARMS = {
    "with_span_floor": REPO / "docs" / "research" / "assets"
                       / "memecoin_tape_predictive_features_with_span_floor_2026-07-24.csv",
    "without_span_floor": REPO / "docs" / "research" / "assets"
                          / "memecoin_tape_predictive_features_without_span_floor_2026-07-24.csv",
}
OUT_CSV = (REPO / "docs" / "research" / "assets"
           / "memecoin_tape_purged_robustness_2026-07-24.csv")

MIN_TEST_ROWS_PER_POOL = 20
N_BOOT = 1000
N_SUBWINDOWS = 4
RNG = np.random.default_rng(0)


def metric(pred, actual, ttype):
    if ttype == "reg":
        if len(set(actual)) < 2 or np.std(pred) == 0:
            return np.nan
        return stats.pearsonr(pred, actual)[0]
    if len(set(actual)) < 2:
        return np.nan
    return roc_auc_score(actual, pred)


def per_pool_metric(df, ttype, min_rows=MIN_TEST_ROWS_PER_POOL):
    vals = []
    for pool, grp in df.groupby("pool"):
        if len(grp) < min_rows:
            continue
        if ttype == "reg" and grp["actual"].std() == 0:
            continue
        if ttype == "clf" and grp["actual"].nunique() < 2:
            continue
        vals.append(metric(grp["pred"].values, grp["actual"].values, ttype))
    vals = [v for v in vals if not np.isnan(v)]
    return np.array(vals)


def block_bootstrap_ci(df, ttype, chunk_len, n_boot=N_BOOT):
    """Resample contiguous per-pool block_no chunks (length >= chunk_len) with
    replacement; recompute the metric on each resampled bag; return (lo, hi)
    of the 95% CI."""
    d = df.copy()
    d["chunk_id"] = d.groupby("pool")["block_no"].transform(lambda s: (s - s.min()) // chunk_len)
    chunks = list(d.groupby(["pool", "chunk_id"]))
    chunk_arrays = [(g["pred"].values, g["actual"].values) for _, g in chunks if len(g) > 0]
    n = len(chunk_arrays)
    if n < 5:
        return (np.nan, np.nan, n)
    boot_metrics = []
    for _ in range(n_boot):
        idx = RNG.integers(0, n, size=n)
        preds = np.concatenate([chunk_arrays[i][0] for i in idx])
        actuals = np.concatenate([chunk_arrays[i][1] for i in idx])
        m = metric(preds, actuals, ttype)
        if not np.isnan(m):
            boot_metrics.append(m)
    boot_metrics = np.array(boot_metrics)
    return (np.percentile(boot_metrics, 2.5), np.percentile(boot_metrics, 97.5), n)


def run_arm(arm, csv):
    print(f"\n########## ARM: {arm} ##########", file=sys.stderr)
    print(f"Loading feature matrix (existing CSV, no new features): {csv.name}", file=sys.stderr)
    cols = FEATURE_COLS + ["block_no", "pool"] + [t for t, _, _ in TARGETS]
    df = pd.read_csv(csv, usecols=cols)
    min_b, max_b = df["block_no"].min(), df["block_no"].max()
    rng_blocks = max_b - min_b
    cut60 = min_b + 0.6 * rng_blocks
    cut80 = min_b + 0.8 * rng_blocks
    print(f"block range [{min_b},{max_b}] cut60={cut60:.0f} cut80={cut80:.0f}", file=sys.stderr)

    results = {}
    for target, ttype, horizon in TARGETS:
        print(f"\n=== {target} ===", file=sys.stderr)
        d = df.dropna(subset=[target])
        train = d[d["block_no"] < cut60]
        test_full = d[d["block_no"] >= cut80].copy()

        cols_used = [c for c in FEATURE_COLS if train[c].nunique(dropna=True) >= 2]
        dropped = set(FEATURE_COLS) - set(cols_used)
        if dropped:
            print(f"  dropped degenerate cols: {sorted(dropped)}", file=sys.stderr)

        Xtr, ytr = train[cols_used].values, train[target].values
        if ttype == "reg":
            model = HistGradientBoostingRegressor(max_depth=4, max_iter=150, random_state=0).fit(Xtr, ytr)
            test_full["pred"] = model.predict(test_full[cols_used].values)
        else:
            model = HistGradientBoostingClassifier(max_depth=4, max_iter=150, random_state=0).fit(Xtr, ytr)
            test_full["pred"] = model.predict_proba(test_full[cols_used].values)[:, 1]
        test_full["actual"] = test_full[target].values

        purge_gap = LONGEST_WINDOW + horizon
        boundary = cut80 + purge_gap
        purged = test_full[test_full["block_no"] >= boundary].copy()

        headline = metric(test_full["pred"].values, test_full["actual"].values, ttype)
        purged_metric = metric(purged["pred"].values, purged["actual"].values, ttype)
        pp_headline = per_pool_metric(test_full.rename(columns={}), ttype)
        pp_purged = per_pool_metric(purged, ttype)

        lo, hi, n_chunks = block_bootstrap_ci(purged, ttype, purge_gap)

        # 4 time sub-windows of the PURGED test period
        sw_bounds = np.linspace(boundary, max_b, N_SUBWINDOWS + 1)
        subwindow_stats = []
        for i in range(N_SUBWINDOWS):
            lo_b, hi_b = sw_bounds[i], sw_bounds[i + 1]
            sw = purged[(purged["block_no"] >= lo_b) & (purged["block_no"] < hi_b if i < N_SUBWINDOWS - 1 else purged["block_no"] <= hi_b)]
            pp_sw = per_pool_metric(sw, ttype, min_rows=10)
            subwindow_stats.append((len(sw), len(pp_sw), np.median(pp_sw) if len(pp_sw) else np.nan))

        results[target] = dict(headline=headline, purged_metric=purged_metric,
                                pp_headline_median=np.median(pp_headline) if len(pp_headline) else np.nan,
                                pp_headline_n=len(pp_headline), pp_headline_frac_pos=(pp_headline > (0 if ttype == "reg" else 0.5)).mean() if len(pp_headline) else np.nan,
                                pp_purged_median=np.median(pp_purged) if len(pp_purged) else np.nan,
                                pp_purged_n=len(pp_purged), pp_purged_frac_pos=(pp_purged > (0 if ttype == "reg" else 0.5)).mean() if len(pp_purged) else np.nan,
                                boot_lo=lo, boot_hi=hi, n_chunks=n_chunks,
                                n_test_full=len(test_full), n_purged=len(purged),
                                purge_gap=purge_gap, subwindows=subwindow_stats)

        print(f"  n_test_full={len(test_full)} n_purged={len(purged)} purge_gap={purge_gap} blocks", file=sys.stderr)
        print(f"  headline (unpurged) metric={headline:.4f} | purged metric={purged_metric:.4f}", file=sys.stderr)
        print(f"  per-pool headline: n={len(pp_headline)} median={np.median(pp_headline):.3f} frac_pos={(pp_headline > (0 if ttype=='reg' else 0.5)).mean():.2f}", file=sys.stderr)
        print(f"  per-pool PURGED:   n={len(pp_purged)} median={np.median(pp_purged):.3f} frac_pos={(pp_purged > (0 if ttype=='reg' else 0.5)).mean():.2f}", file=sys.stderr)
        print(f"  block-bootstrap 95% CI (n_chunks={n_chunks}, chunk_len={purge_gap}): [{lo:.4f}, {hi:.4f}]", file=sys.stderr)
        print(f"  4 sub-windows of purged test period (n_rows, n_pools, median per-pool {('corr' if ttype=='reg' else 'AUC')}):", file=sys.stderr)
        for i, (n_rows, n_pools, med) in enumerate(subwindow_stats):
            print(f"    sw{i+1}: n_rows={n_rows} n_pools={n_pools} median={med:.3f}" if not np.isnan(med) else f"    sw{i+1}: n_rows={n_rows} n_pools={n_pools} median=NaN(insufficient pools)", file=sys.stderr)

    return results


def main():
    rows = []
    for arm, csv in ARMS.items():
        if not csv.exists():
            print(f"MISSING arm csv, skipped: {csv}", file=sys.stderr)
            continue
        res = run_arm(arm, csv)
        for target, r in res.items():
            rows.append({"arm": arm, "target": target, **{
                k: v for k, v in r.items() if k != "subwindows"},
                "subwindow_medians": ";".join(
                    "nan" if np.isnan(m) else f"{m:.4f}" for _n, _p, m in r["subwindows"])})
    if rows:
        pd.DataFrame(rows).to_csv(OUT_CSV, index=False)
        print(f"\nwrote {OUT_CSV}", file=sys.stderr)
    return rows



# Operator protocol: legacy hypothetical conclusions cannot publish by default.
from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports
_quarantine_exports(globals(), ('main',))

if __name__ == "__main__":
    main()
