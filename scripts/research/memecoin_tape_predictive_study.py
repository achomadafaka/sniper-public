#!/usr/bin/env python3
"""Tape predictive viability study — chain 4663 (Robinhood) pool_ticks.

Question: do per-block order-flow features (tick tape) predict short-horizon
memecoin forward returns, beyond a naive momentum baseline?

Pre-registered BEFORE seeing results (see docstring below + memo text in
final report). Read-only: queries data/memecoin_ticks.db, writes only to
docs/research/assets/. Does not touch any live path, settings, or exit-brain
code.

Method
------
1. Per-pool per-block features from pool_ticks (chain_id='4663', kinds
   v2_swap/v3_swap/v2_sync). block_no is the clock; block time is a MEASURED
   0.1003 s/block (chain 4663, 1,066,852 blocks over 29.7 h, 2026-07-24), so a
   window of W blocks == W*BLOCK_SECONDS of "pseudo time"
   (pseudo_time = block_no * BLOCK_SECONDS, arbitrary epoch). Because pseudo_time
   is a strictly increasing LINEAR function of block_no, a pandas time-based
   rolling window of D seconds on pseudo_time is EXACTLY a D/BLOCK_SECONDS-block
   window — not an approximation — and it correctly skips blocks with zero
   swaps (sparse series), which a plain row-count rolling window would not.
2. Windows: 26 / 130 / 390 blocks (~1 / 5 / 15 min).
3. Restrict to pools with >=200 distinct swap blocks (density floor).
4. Dead-gap guard: any consecutive block_no jump > 500 blocks (within a
   pool) starts a new `group_id`. All rolling/asof lookups are grouped by
   (pool, group_id), so no feature or label window can span a gap —
   including the observed ~9h 07-24 dead period. This is stricter than
   "exclude the one known gap": it excludes ANY >500-block gap per pool.
5. Labels: forward VWAP log-return at +130 / +390 blocks (point lookup,
   nearest actual future block via merge_asof, direction='forward', grouped
   so it cannot cross a gap) and binary hit-+20%-within-780-blocks (forward
   rolling max within the same group).
6. Walk-forward split BY TIME: cutoffs are fractions of the GLOBAL block_no
   RANGE (not row-count quantiles): train = first 60% of the block range,
   val = next 20%, test = last 20%. Models are fit on train only and scored
   on test only (val is reported but not used for any selection step here —
   single fixed hyperparameter set, no tuning loop).
7. Models: Ridge (features requiring no NaN) + HistGradientBoosting
   (sklearn — tolerates NaN natively, so it also gets the V2-only reserve
   features). Both compared against a momentum-only baseline (single
   past-return feature at the matching horizon) and a zero baseline.
8. Reported per-feature-set: OOS Pearson corr (regression) / AUC
   (classification), momentum-baseline delta, per-pool stability (test-set
   corr/AUC distribution across pools with >=20 test rows), and feature
   importances (GBM built-in).

Run: python3 scripts/research/memecoin_tape_predictive_study.py
"""
from __future__ import annotations

import sys
import pathlib as _research_pathlib
sys.path.insert(0, str(_research_pathlib.Path(__file__).resolve().parents[2]))
from sniper.research.wallet_execution.publication import legacy_launch as _legacy_launch
_legacy_launch(__name__)

import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.metrics import roc_auc_score
from sklearn.preprocessing import StandardScaler

REPO = Path(__file__).resolve().parents[2]
DB_PATH = REPO / "data" / "memecoin_ticks.db"
ASSETS_DIR = REPO / "docs" / "research" / "assets"
ASSETS_DIR.mkdir(parents=True, exist_ok=True)

# MEASURED cadence, not an assumption (P1, Codex 2026-07-24). The previous literal 2.3 s/block was
# 23x too slow, so every window and label in this study was 23x too SHORT: the "1 / 5 / 15 min"
# windows were really 2.6 / 13 / 39 seconds and the "30 min" hit label was 78 seconds. Every result
# produced under it is void.
#
# Measured 2026-07-24 from block_times, chain 4663: blocks 17,159,858..18,226,710 (1,066,852 blocks)
# spanning 107,017 s (29.7 h) => 9.969 blocks/s => 0.1003 s/block.
BLOCK_SECONDS = 0.1003
_BLOCK_SECONDS_TOLERANCE = 0.25          # fail if reality drifts >25% from the frozen constant

# Windows are declared in SECONDS — the unit the hypothesis is actually about — and converted to
# blocks here. Declaring them in blocks is what let the cadence error hide in plain sight.
WINDOWS_SECONDS = [60, 300, 900]                       # 1 / 5 / 15 min
HORIZONS_SECONDS = {"fwd_ret_5m": 300, "fwd_ret_15m": 900}
HIT_HORIZON_SECONDS = 1800                             # 30 min
GAP_SECONDS = 1150                                     # dead-gap guard (preserves the prior intent)

WINDOWS_BLOCKS = [int(round(w / BLOCK_SECONDS)) for w in WINDOWS_SECONDS]
HORIZONS_BLOCKS = {k: int(round(v / BLOCK_SECONDS)) for k, v in HORIZONS_SECONDS.items()}
HIT_HORIZON_BLOCKS = int(round(HIT_HORIZON_SECONDS / BLOCK_SECONDS))
GAP_BLOCKS = int(round(GAP_SECONDS / BLOCK_SECONDS))
# Two SEPARATE floors. `MIN_SWAP_BLOCKS` was chosen as a sample-size floor, but under the old
# (23x-too-slow) cadence it also silently implied a lifetime floor of ~8 minutes. Correcting the
# cadence collapsed that second meaning to ~20 s, which would let a flash-pool qualify. Rather than
# retune one number to carry both jobs, each is now stated explicitly (Codex 2026-07-24).
MIN_SWAP_BLOCKS = 200          # sample density: distinct swap-blocks per pool. Unchanged.
# Lifetime floor. NOT "time containing trades" — it is elapsed span inside non-gap groups, which
# includes quiet stretches shorter than GAP_SECONDS. Calling it "active trading" overstated it.
MIN_ACTIVE_SPAN_S = 1800
# This is a NEW COHORT RULE, so it can introduce survivor bias: pools that die inside 30 minutes
# are exactly the ones whose forward returns are worst, and excluding them flatters every label.
# Pre-registered 2026-07-24 BEFORE any rerun output was read. The rerun MUST report both arms.
REPORT_SPAN_FLOOR_SENSITIVITY = True
HIT_COL = "hit20_30m"                                  # derived from HIT_HORIZON_SECONDS


def _study_arms():
    if REPORT_SPAN_FLOOR_SENSITIVITY:
        return (("with_span_floor", True), ("without_span_floor", False))
    return (("with_span_floor", True),)


def _arm_output_path(kind: str, arm: str) -> Path:
    return ASSETS_DIR / f"memecoin_tape_predictive_{kind}_{arm}_2026-07-24.csv"


def assert_cadence_matches_db(conn):
    """Fail loudly if the chain's real cadence has drifted from the frozen BLOCK_SECONDS.

    pseudo_time = block_no * BLOCK_SECONDS is exact only while the cadence holds. The constant stays
    frozen for reproducibility; this check makes a drift an error instead of a silently wrong study.
    """
    from datetime import datetime
    rows = conn.execute("SELECT block_no, block_ts FROM block_times WHERE chain_id='4663' "
                        "ORDER BY block_no").fetchall()
    if len(rows) < 1000:
        raise SystemExit("cadence check: too few headers to verify BLOCK_SECONDS")
    lo_b, hi_b = rows[0][0], rows[-1][0]
    lo_t = datetime.fromisoformat(rows[0][1]).timestamp()
    hi_t = datetime.fromisoformat(rows[-1][1]).timestamp()
    measured = (hi_t - lo_t) / (hi_b - lo_b)
    if abs(measured - BLOCK_SECONDS) / BLOCK_SECONDS > _BLOCK_SECONDS_TOLERANCE:
        raise SystemExit(
            f"cadence check FAILED: headers say {measured:.4f} s/block, constant is "
            f"{BLOCK_SECONDS} s/block. Re-freeze BLOCK_SECONDS and rerun; every window in this "
            f"study is expressed in seconds and converted through it.")
    return measured
HIT_THRESHOLD = 0.20
MIN_TEST_ROWS_PER_POOL = 20

EPOCH = pd.Timestamp("2020-01-01")


def w_seconds(w_blocks: int) -> float:
    return w_blocks * BLOCK_SECONDS


def grouped_time_rolling(df: pd.DataFrame, group_col: str, time_col: str, window: str,
                          specs: list[tuple[str, str, str]]) -> pd.DataFrame:
    """`df.groupby(group_col).rolling(window, on=time_col)[value_col].agg()` returns a
    (group, time-value) MultiIndex ordered group-major, NOT in df's original row order
    (verified empirically — interleaved groups get reordered). Realign to df's row
    order via an explicit merge on (group_col, time_col), which is safe because
    time_col is unique within each group here (derived 1:1 from unique block_no).

    specs: list of (out_col_name, value_col, agg_method_name).
    Returns a DataFrame indexed exactly like df.
    """
    key = df[[group_col, time_col]].copy()
    key["_rid"] = df.index
    grouped = df.groupby(group_col).rolling(window, on=time_col)
    out = pd.DataFrame(index=df.index)
    for out_name, value_col, agg in specs:
        s = getattr(grouped[value_col], agg)().reset_index().rename(columns={value_col: out_name})
        merged = key.merge(s[[group_col, time_col, out_name]], on=[group_col, time_col], how="left")
        out[out_name] = merged.set_index("_rid")[out_name].reindex(df.index).values
    return out


def load_swaps(conn: sqlite3.Connection) -> pd.DataFrame:
    q = """
        SELECT pool, kind, block_no, amount0, amount1
        FROM pool_ticks
        WHERE chain_id = '4663' AND kind IN ('v2_swap', 'v3_swap')
        ORDER BY pool, block_no
    """
    df = pd.read_sql_query(q, conn)
    # amounts are signed TEXT ints that can exceed int64 range (raw 18-decimal token
    # units, up to ~1e24) -> float64 (15-17 sig figs is ample for return-magnitude math;
    # we never need exact integer wei precision here).
    df["amount0"] = df["amount0"].apply(float)
    df["amount1"] = df["amount1"].apply(float)
    return df


def load_syncs(conn: sqlite3.Connection) -> pd.DataFrame:
    q = """
        SELECT pool, block_no, amount0, amount1
        FROM pool_ticks
        WHERE chain_id = '4663' AND kind = 'v2_sync'
        ORDER BY pool, block_no
    """
    df = pd.read_sql_query(q, conn)
    if df.empty:
        return df
    df["amount0"] = df["amount0"].apply(float)
    df["amount1"] = df["amount1"].apply(float)
    return df


def pick_quote_side(swaps: pd.DataFrame) -> pd.Series:
    """Per pool: the side (0 or 1) with the smaller median |amount| is the
    quote asset (WETH-like — small raw magnitude). The side with the larger
    median magnitude is the base memecoin token (huge raw supply -> huge
    raw swap amounts even for small USD size). Heuristic, documented — no
    token-address/decimals table available in pool_ticks.
    """
    med0 = swaps.groupby("pool")["amount0"].apply(lambda s: s.abs().median())
    med1 = swaps.groupby("pool")["amount1"].apply(lambda s: s.abs().median())
    quote_is_0 = (med0 < med1).rename("quote_is_0")
    return quote_is_0


def build_pool_block_features(pool_swaps: pd.DataFrame, quote_is_0: bool,
                              pool_syncs: pd.DataFrame | None, *,
                              apply_span_floor: bool = True) -> pd.DataFrame:
    """One pool's swap events -> per-block feature/label frame. Returns
    empty frame if the pool doesn't clear the density floor."""
    s = pool_swaps
    if quote_is_0:
        quote_amt = s["amount0"].abs()
        base_signed = s["amount1"]
    else:
        quote_amt = s["amount1"].abs()
        base_signed = s["amount0"]
    base_amt = base_signed.abs()
    is_buy = base_signed < 0  # base flowing OUT of pool (positive=into pool, per prompt) = buy
    signed_flow = np.where(is_buy, quote_amt, -quote_amt)
    price = np.where(base_amt > 0, quote_amt / base_amt.replace(0, np.nan), np.nan)

    ev = pd.DataFrame({
        "block_no": s["block_no"].values,
        "quote_amt": quote_amt.values,
        "base_amt": base_amt.values,
        "signed_flow": signed_flow,
        "is_buy": is_buy.values,
        "price": price,
    })

    g = ev.groupby("block_no", as_index=False).agg(
        n_swaps=("price", "size"),
        n_buys=("is_buy", "sum"),
        sum_quote=("quote_amt", "sum"),
        sum_base=("base_amt", "sum"),
        net_flow=("signed_flow", "sum"),
    )
    # true VWAP (quote-notional weighted): sum(quote)/sum(base) over the block.
    # Guard vwap<=0/non-finite -> NaN so downstream log-returns can't produce +/-inf
    # (a zero-quote or zero-base block is a data artifact, not a real zero price).
    vwap = g["sum_quote"] / g["sum_base"].replace(0, np.nan)
    g["vwap"] = vwap.where(np.isfinite(vwap) & (vwap > 0))
    g["n_sells"] = g["n_swaps"] - g["n_buys"]
    g = g.sort_values("block_no").reset_index(drop=True)

    if len(g) < MIN_SWAP_BLOCKS:
        return pd.DataFrame()

    # dead-gap guard: >GAP_BLOCKS jump starts a new group
    gap = g["block_no"].diff().fillna(0)
    g["group_id"] = (gap > GAP_BLOCKS).cumsum()

    # Lifetime floor, measured as non-gap span: the sum of within-group block spans. Dead gaps
    # (>GAP_SECONDS) are excluded by construction; quiet stretches shorter than that are NOT, so
    # this is elapsed span inside active groups, not time containing trades. Whole-pool
    # first-to-last would let two 10-second bursts three hours apart clear a 30-minute floor.
    span_blocks = g.groupby("group_id")["block_no"].agg(lambda x: x.max() - x.min()).sum()
    span_s = span_blocks * BLOCK_SECONDS
    clears_span_floor = bool(span_s >= MIN_ACTIVE_SPAN_S)
    if apply_span_floor and not clears_span_floor:
        return pd.DataFrame()
    g["pseudo_time"] = EPOCH + pd.to_timedelta(g["block_no"] * BLOCK_SECONDS, unit="s")

    if pool_syncs is not None and not pool_syncs.empty:
        sy = pool_syncs.sort_values("block_no").copy()
        if quote_is_0:
            sy["reserve_quote"] = sy["amount0"].astype(float)
        else:
            sy["reserve_quote"] = sy["amount1"].astype(float)
        sy["reserve_delta"] = sy["reserve_quote"].diff()
        sy["pseudo_time"] = EPOCH + pd.to_timedelta(sy["block_no"] * BLOCK_SECONDS, unit="s")
        g = pd.merge_asof(
            g.sort_values("pseudo_time"), sy[["pseudo_time", "reserve_quote", "reserve_delta"]].sort_values("pseudo_time"),
            on="pseudo_time", direction="backward",
        )
        g = g.sort_values("block_no").reset_index(drop=True)
    else:
        g["reserve_quote"] = np.nan
        g["reserve_delta"] = np.nan

    g["log_ret_1"] = np.log(g["vwap"]).diff()

    for w in WINDOWS_BLOCKS:
        win = f"{int(w_seconds(w))}s"
        rolled = grouped_time_rolling(g, "group_id", "pseudo_time", win, [
            ("net_flow_roll", "net_flow", "sum"),
            ("buys_roll", "n_buys", "sum"),
            ("sells_roll", "n_sells", "sum"),
            ("nswaps_roll", "n_swaps", "sum"),
            ("vol_roll", "log_ret_1", "std"),
        ])
        buys_roll, sells_roll = rolled["buys_roll"], rolled["sells_roll"]
        g[f"net_flow_{w}"] = rolled["net_flow_roll"].values
        g[f"buy_frac_{w}"] = (buys_roll / (buys_roll + sells_roll).replace(0, np.nan)).values
        g[f"arrival_rate_{w}"] = (rolled["nswaps_roll"] / w_seconds(w)).values
        g[f"vol_{w}"] = rolled["vol_roll"].values

    # acceleration: row-over-row first difference of the fastest arrival rate
    # (documented approximation — natural swap-block resolution, not an
    # exact W-block-lagged derivative; ceiling noted, upgrade = resample to
    # a dense block grid if this ever matters).
    _fast = WINDOWS_BLOCKS[0]          # fastest window; derived, never a literal
    g[f"accel_{_fast}"] = g.groupby("group_id")[f"arrival_rate_{_fast}"].diff()

    # momentum baseline features: past return at each horizon (backward asof)
    gsrc = g[["pseudo_time", "group_id", "vwap"]].dropna(subset=["vwap"]).sort_values("pseudo_time")
    for w in WINDOWS_BLOCKS:
        tgt = g[["pseudo_time", "group_id"]].copy()
        tgt["target_time"] = tgt["pseudo_time"] - pd.to_timedelta(w_seconds(w), unit="s")
        tgt = tgt.sort_values("target_time")
        m = pd.merge_asof(tgt, gsrc.rename(columns={"pseudo_time": "match_time", "vwap": "past_vwap", "group_id": "match_group"}),
                           left_on="target_time", right_on="match_time", direction="backward")
        # merge_asof preserves left-frame ROW ORDER but resets the index -> reattach
        # tgt's original index by position, not by reindex-as-label-lookup (that was
        # a silent misalignment bug: tgt.index is a permutation of 0..n-1, so a
        # label-based reindex on m's fresh 0..n-1 index pulled the wrong rows).
        m.index = tgt.index
        valid = m["match_group"] == tgt["group_id"]
        past_vwap = m["past_vwap"].where(valid)
        g[f"past_ret_{w}"] = np.nan
        g.loc[tgt.index, f"past_ret_{w}"] = np.log(g.loc[tgt.index, "vwap"].values / past_vwap.values)

    # forward labels
    gsrc2 = g[["pseudo_time", "group_id", "vwap"]].dropna(subset=["vwap"]).sort_values("pseudo_time")
    for name, wb in HORIZONS_BLOCKS.items():
        tgt = g[["pseudo_time", "group_id"]].copy()
        tgt["target_time"] = tgt["pseudo_time"] + pd.to_timedelta(w_seconds(wb), unit="s")
        tgt = tgt.sort_values("target_time")
        m = pd.merge_asof(tgt, gsrc2.rename(columns={"pseudo_time": "match_time", "vwap": "fut_vwap", "group_id": "match_group"}),
                           left_on="target_time", right_on="match_time", direction="forward")
        m.index = tgt.index  # see past_ret note above — merge_asof resets the index
        valid = m["match_group"] == tgt["group_id"]
        fut_vwap = m["fut_vwap"].where(valid)
        g[name] = np.nan
        g.loc[tgt.index, name] = np.log(fut_vwap.values / g.loc[tgt.index, "vwap"].values)

    # binary hit target: forward rolling max within same group, window HIT_HORIZON_BLOCKS.
    # Reverse time so a trailing rolling-max == a forward-looking max in original order.
    rev = g[["pseudo_time", "group_id", "vwap"]].copy()
    rev["orig_idx"] = g.index
    rev["neg_time"] = EPOCH - (rev["pseudo_time"] - EPOCH)
    rev = rev.sort_values("neg_time").reset_index(drop=True)
    win = f"{int(w_seconds(HIT_HORIZON_BLOCKS))}s"
    rolled = grouped_time_rolling(rev, "group_id", "neg_time", win, [("fwd_max_vwap", "vwap", "max")])
    rev["fwd_max_vwap"] = rolled["fwd_max_vwap"].values
    fwd_max = rev.set_index("orig_idx")["fwd_max_vwap"].reindex(g.index)
    g["fwd_max_vwap"] = fwd_max.values
    # only valid if there IS at least HIT_HORIZON_BLOCKS of future coverage in the same group;
    # approximate via: last block_no in group - current block_no >= HIT_HORIZON_BLOCKS
    last_block_in_group = g.groupby("group_id")["block_no"].transform("max")
    coverage_ok = (last_block_in_group - g["block_no"]) >= HIT_HORIZON_BLOCKS
    g[HIT_COL] = np.where(coverage_ok, (g["fwd_max_vwap"] >= g["vwap"] * (1 + HIT_THRESHOLD)).astype(float), np.nan)

    g = g.reset_index(drop=True)
    g.attrs["span_s"] = span_s
    g.attrs["clears_span_floor"] = clears_span_floor
    return g


def _iter_arm_frames(swaps, syncs, qside, qualifying):
    """Yield both cohorts from one source snapshot without retaining both feature frames."""
    for arm, apply_span_floor in _study_arms():
        frames = []
        for i, pool in enumerate(qualifying):
            ps = swaps[swaps["pool"] == pool]
            py = syncs[syncs["pool"] == pool] if not syncs.empty else None
            qi = bool(qside.get(pool, True))
            feat = build_pool_block_features(
                ps, qi, py, apply_span_floor=apply_span_floor
            )
            if feat.empty:
                continue
            feat["pool"] = pool
            frames.append(feat)
            if (i + 1) % 200 == 0:
                print(f"  [{arm}] ...{i+1}/{len(qualifying)} pools processed", file=sys.stderr)
        if not frames:
            raise RuntimeError(f"{arm}: no pools qualify; refusing an unlabeled empty analysis")
        yield arm, pd.concat(frames, ignore_index=True)


def _run_study_arm(full, *, arm):
    numeric_cols = full.select_dtypes(include=[np.number]).columns
    full[numeric_cols] = full[numeric_cols].replace([np.inf, -np.inf], np.nan)
    print(f"  [{arm}] {len(full)} pool-block rows across {full['pool'].nunique()} pools",
          file=sys.stderr)

    feature_cols_ridge = [f"{m}_{w}" for m in ["net_flow", "buy_frac", "arrival_rate", "vol"] for w in WINDOWS_BLOCKS] + [f"accel_{WINDOWS_BLOCKS[0]}"]
    feature_cols_gbm = feature_cols_ridge + ["reserve_quote", "reserve_delta"]

    raw_out = _arm_output_path("features", arm)
    full.to_csv(raw_out, index=False)
    print(f"  wrote {raw_out} ({raw_out.stat().st_size/1e6:.1f} MB)", file=sys.stderr)

    min_block, max_block = full["block_no"].min(), full["block_no"].max()
    rng = max_block - min_block
    cut60 = min_block + 0.6 * rng
    cut80 = min_block + 0.8 * rng

    full["split"] = np.select(
        [full["block_no"] < cut60, full["block_no"] < cut80],
        ["train", "val"],
        default="test",
    )
    print(f"  block range [{min_block},{max_block}] cut60={cut60:.0f} cut80={cut80:.0f}", file=sys.stderr)
    print(full["split"].value_counts().to_dict(), file=sys.stderr)

    results = []
    per_pool_rows = []
    importances = []

    targets = [(name, "reg", blocks) for name, blocks in HORIZONS_BLOCKS.items()] \
              + [(HIT_COL, "clf", HIT_HORIZON_BLOCKS)]

    for target, ttype, horizon_blocks in targets:
        # momentum control: the widest window at or below this horizon
        _le = [w for w in WINDOWS_BLOCKS if w <= horizon_blocks] or [WINDOWS_BLOCKS[0]]
        mom_col = f"past_ret_{max(_le)}"
        cols_needed = feature_cols_gbm + [target, mom_col, "pool", "split", "block_no"]
        d = full[cols_needed].copy()
        d_ridge = d.dropna(subset=feature_cols_ridge + [target, mom_col])
        d_gbm = d.dropna(subset=[target])  # GBM tolerates NaN features

        for label, cols, dset in [("ridge_full", feature_cols_ridge, d_ridge), ("gbm_full", feature_cols_gbm, d_gbm)]:
            train = dset[dset["split"] == "train"]
            test = dset[dset["split"] == "test"]
            if len(train) < 200 or len(test) < 50:
                results.append(dict(target=target, model=label, n_train=len(train), n_test=len(test),
                                     metric=np.nan, note="insufficient rows"))
                continue
            # HGB's binning step crashes on a column with <2 distinct finite train
            # values (e.g. reserve_* is all-NaN for V3-only pools with no v2_sync).
            # Drop degenerate columns per-fit rather than skip the whole model.
            dropped = [c for c in cols if train[c].nunique(dropna=True) < 2]
            cols = [c for c in cols if c not in dropped]
            if dropped:
                print(f"  [{target}/{label}] dropped degenerate cols: {dropped}", file=sys.stderr)
            Xtr, Xte = train[cols].values, test[cols].values
            ytr, yte = train[target].values, test[target].values

            if ttype == "reg":
                if label.startswith("ridge"):
                    scaler = StandardScaler()
                    Xtr_s = scaler.fit_transform(Xtr)
                    Xte_s = scaler.transform(Xte)
                    model = Ridge(alpha=1.0).fit(Xtr_s, ytr)
                    pred = model.predict(Xte_s)
                else:
                    model = HistGradientBoostingRegressor(max_depth=4, max_iter=150, random_state=0).fit(Xtr, ytr)
                    pred = model.predict(Xte)
                corr, p = stats.pearsonr(pred, yte)
                results.append(dict(target=target, model=label, n_train=len(train), n_test=len(test),
                                     metric=corr, p_value=p, note=""))
                if label == "gbm_full":
                    imp = pd.Series(model.feature_importances_ if hasattr(model, "feature_importances_") else
                                     _permutation_importance(model, Xte, yte, cols), index=cols)
                    for c, v in imp.sort_values(ascending=False).items():
                        importances.append(dict(target=target, feature=c, importance=v))
                    test_pred_df = test[["pool"]].copy()
                    test_pred_df["pred"] = pred
                    test_pred_df["actual"] = yte
                    pp = test_pred_df.groupby("pool").filter(lambda x: len(x) >= MIN_TEST_ROWS_PER_POOL)
                    for pool, grp in pp.groupby("pool"):
                        if grp["actual"].std() == 0 or grp["pred"].std() == 0:
                            continue
                        c, _ = stats.pearsonr(grp["pred"], grp["actual"])
                        per_pool_rows.append(dict(target=target, model=label, pool=pool, n=len(grp), corr=c))
            else:
                if label.startswith("ridge"):
                    scaler = StandardScaler()
                    Xtr_s = scaler.fit_transform(Xtr)
                    Xte_s = scaler.transform(Xte)
                    model = LogisticRegression(max_iter=1000).fit(Xtr_s, ytr)
                    proba = model.predict_proba(Xte_s)[:, 1]
                else:
                    model = HistGradientBoostingClassifier(max_depth=4, max_iter=150, random_state=0).fit(Xtr, ytr)
                    proba = model.predict_proba(Xte)[:, 1]
                if len(set(yte)) < 2:
                    results.append(dict(target=target, model=label, n_train=len(train), n_test=len(test),
                                         metric=np.nan, note="single-class test set"))
                    continue
                auc = roc_auc_score(yte, proba)
                results.append(dict(target=target, model=label, n_train=len(train), n_test=len(test),
                                     metric=auc, p_value=np.nan, note=""))
                if label == "gbm_full":
                    imp = model.feature_importances_ if hasattr(model, "feature_importances_") else \
                        _permutation_importance(model, Xte, yte, cols)
                    imp = pd.Series(imp, index=cols)
                    for c, v in imp.sort_values(ascending=False).items():
                        importances.append(dict(target=target, feature=c, importance=v))
                    test_pred_df = test[["pool"]].copy()
                    test_pred_df["pred"] = proba
                    test_pred_df["actual"] = yte
                    pp = test_pred_df.groupby("pool").filter(lambda x: len(x) >= MIN_TEST_ROWS_PER_POOL and x["actual"].nunique() >= 2)
                    for pool, grp in pp.groupby("pool"):
                        try:
                            a = roc_auc_score(grp["actual"], grp["pred"])
                        except ValueError:
                            continue
                        per_pool_rows.append(dict(target=target, model=label, pool=pool, n=len(grp), corr=a))

        # momentum-only baseline (single feature, same eval protocol)
        train = d_ridge[d_ridge["split"] == "train"]
        test = d_ridge[d_ridge["split"] == "test"]
        if len(train) >= 200 and len(test) >= 50:
            Xtr = train[[mom_col]].values
            Xte = test[[mom_col]].values
            ytr, yte = train[target].values, test[target].values
            if ttype == "reg":
                model = Ridge(alpha=1.0).fit(Xtr, ytr)
                pred = model.predict(Xte)
                corr, p = stats.pearsonr(pred, yte)
                results.append(dict(target=target, model="momentum_only", n_train=len(train), n_test=len(test),
                                     metric=corr, p_value=p, note=f"feature={mom_col}"))
            else:
                if len(set(yte)) < 2:
                    results.append(dict(target=target, model="momentum_only", n_train=len(train), n_test=len(test),
                                         metric=np.nan, note="single-class test set"))
                else:
                    model = LogisticRegression(max_iter=1000).fit(Xtr, ytr)
                    proba = model.predict_proba(Xte)[:, 1]
                    auc = roc_auc_score(yte, proba)
                    results.append(dict(target=target, model="momentum_only", n_train=len(train), n_test=len(test),
                                         metric=auc, p_value=np.nan, note=f"feature={mom_col}"))
        # zero baseline
        if ttype == "reg" and len(test) >= 50:
            results.append(dict(target=target, model="zero_baseline", n_train=len(train), n_test=len(test),
                                 metric=0.0, p_value=np.nan, note="predict 0 always -> corr defined as 0"))
        elif ttype == "clf" and len(test) >= 50 and len(set(yte)) >= 2:
            results.append(dict(target=target, model="zero_baseline", n_train=len(train), n_test=len(test),
                                 metric=0.5, p_value=np.nan, note="predict base rate always -> AUC=0.5"))

    res_df = pd.DataFrame(results)
    pp_df = pd.DataFrame(per_pool_rows)
    imp_df = pd.DataFrame(importances)
    for frame in (res_df, pp_df, imp_df):
        frame.insert(0, "arm", arm)

    res_out = _arm_output_path("results", arm)
    pp_out = _arm_output_path("perpool", arm)
    imp_out = _arm_output_path("importances", arm)
    res_df.to_csv(res_out, index=False)
    pp_df.to_csv(pp_out, index=False)
    imp_df.to_csv(imp_out, index=False)

    print(f"\n=== RESULTS: {arm} ===")
    print(res_df.to_string(index=False))
    print(f"\n=== PER-POOL STABILITY: {arm} "
          f"(gbm_full, pools with >={MIN_TEST_ROWS_PER_POOL} test rows) ===")
    if not pp_df.empty:
        for (target, model), grp in pp_df.groupby(["target", "model"]):
            print(f"{target}/{model}: n_pools={len(grp)} median={grp['corr'].median():.3f} "
                  f"IQR=[{grp['corr'].quantile(.25):.3f},{grp['corr'].quantile(.75):.3f}] "
                  f"frac_positive={(grp['corr']>0).mean():.2f}")
    else:
        print("(no pools cleared the per-pool test-row floor)")
    print(f"\n=== TOP FEATURE IMPORTANCES: {arm} (gbm_full) ===")
    if not imp_df.empty:
        for target, grp in imp_df.groupby("target"):
            top = grp.sort_values("importance", ascending=False).head(5)
            print(f"{target}:")
            print(top[["feature", "importance"]].to_string(index=False))
    print(f"\nCSVs: {raw_out}\n{res_out}\n{pp_out}\n{imp_out}")
    return raw_out, res_out, pp_out, imp_out


def main() -> int:
    conn = sqlite3.connect(str(DB_PATH))
    measured = assert_cadence_matches_db(conn)      # hard gate: a drifted cadence voids every window
    print(f"cadence OK: {measured:.4f} s/block (constant {BLOCK_SECONDS}); "
          f"windows={WINDOWS_SECONDS}s -> {WINDOWS_BLOCKS} blocks", file=sys.stderr)
    print("Loading swap events (chain 4663)...", file=sys.stderr)
    swaps = load_swaps(conn)
    syncs = load_syncs(conn)
    conn.close()
    print(f"  {len(swaps)} swap rows, {len(syncs)} sync rows", file=sys.stderr)

    qside = pick_quote_side(swaps)
    pool_counts = swaps.groupby("pool")["block_no"].nunique()
    qualifying = pool_counts[pool_counts >= MIN_SWAP_BLOCKS].index.tolist()
    print(f"  {len(qualifying)} pools clear >= {MIN_SWAP_BLOCKS} distinct swap blocks", file=sys.stderr)

    for arm, full in _iter_arm_frames(swaps, syncs, qside, qualifying):
        _run_study_arm(full, arm=arm)
    return 0


def _permutation_importance(model, X, y, cols):
    from sklearn.inspection import permutation_importance
    r = permutation_importance(model, X, y, n_repeats=3, random_state=0)
    return r.importances_mean



# Operator protocol: legacy hypothetical conclusions cannot publish by default.
from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports
_quarantine_exports(globals(), ('main',))

if __name__ == "__main__":
    raise SystemExit(main())
