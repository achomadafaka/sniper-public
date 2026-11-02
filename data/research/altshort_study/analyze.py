"""Standalone alt-short signal viability study. Reads cached candles/funding from
./cache, runs the pre-registered signal + baselines + robustness checks, writes CSVs
and prints a results summary consumed by the memo. Read-only vs hl_intel.sqlite.
"""
from __future__ import annotations
import argparse, hashlib, json, glob, os, sqlite3, sys
from pathlib import Path
import numpy as np
import pandas as pd

import sys as _sys, pathlib as _pathlib   # a bare `python3 data/research/altshort_study/analyze.py` puts this file's
_R = str(_pathlib.Path(__file__).resolve().parents[3])  # own dir on sys.path,
if _R not in _sys.path:                    # never the repo root
    _sys.path.insert(0, _R)
from sniper.hyperlive import funding as FUND

ROOT = "/home/bigpapi/Desktop/Sniper"
CACHE = f"{ROOT}/data/research/altshort_study/cache"
OUT = f"{ROOT}/data/research/altshort_study"
INTEL_DB = f"{ROOT}/data/hl_intel.sqlite"

STUDY_START = 1769472000000  # 2026-01-27 00:00 UTC ms
STUDY_END = 1785085200000    # 2026-07-26 15:00 UTC ms

WINDOWS = {
    "W0": (1769472000000, 1774310400000),   # 2026-01-27 -> 2026-03-24
    "W1": (1774310400000, 1779148800000),   # 2026-03-24 -> 2026-05-19
    "W2": (1779148800000, 1783728000000),   # 2026-05-19 -> 2026-07-09
    "DISCOVERY": (1783728000000, 1785085200000),  # 2026-07-09 -> 2026-07-26 (IN-SAMPLE)
}
OOS_WINDOWS = ["W0", "W1", "W2"]

MIN_HISTORY_HOURS = 720  # 30 days
FEE_BPS = 9.0
SLIP_PRIMARY_EACH = 15.0
SL_MULT = 1.20
TP_MULT = 0.85
CAP_HOURS_PRIMARY = 8
CAP_HOURS_ROBUST = 24
RNG = np.random.default_rng(20260726)
TRADE_COLUMNS = [
    "coin", "entry_t", "exit_t", "entry_i", "exit_i", "entry_px", "exit_px",
    "reason", "net_bps", "price_pnl_bps", "funding_bps", "window",
]
METRIC_COLUMNS = [
    "window", "n", "mean_bps", "std_bps", "wr", "total_bps", "sharpe",
    "max_dd_bps", "top_coin", "top_coin_share", "drop_top_coin_mean_bps",
    "drop_top_coin_total_bps",
]


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=Path(CACHE))
    parser.add_argument("--output-dir", type=Path, default=Path(OUT))
    parser.add_argument("--db-path", type=Path, default=Path(INTEL_DB))
    return parser.parse_args(argv)


def analysis_provenance(cache_dir: Path) -> dict:
    seal_path = Path(cache_dir) / "_cache_seal.json"
    if not seal_path.is_file():
        return {
            "evidence_status": "legacy_unsealed",
            "delisted_tail_status": "unknown",
            "cache_seal_sha256": None,
        }
    seal = json.loads(seal_path.read_text(encoding="utf-8"))
    return {
        "evidence_status": str(seal.get("evidence_status") or "unknown"),
        "delisted_tail_status": str(seal.get("delisted_tail_status") or "unknown"),
        "cache_seal_sha256": hashlib.sha256(seal_path.read_bytes()).hexdigest(),
        "symbol_identity": seal.get("symbol_identity"),
    }


# ---------------------------------------------------------------- loading ----

def load_universe():
    universe = json.load(open(f"{CACHE}/_universe.json"))
    alts = [c for c in universe if c not in ("BTC", "ETH", "SOL")]
    return alts


def load_candles(coin):
    fp = f"{CACHE}/candles_{coin}.json"
    if not os.path.exists(fp):
        return None
    rows = json.load(open(fp))
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df = df.sort_values("t").drop_duplicates("t").reset_index(drop=True)
    return df


def load_funding(coin):
    fp = f"{CACHE}/funding_{coin}.json"
    if not os.path.exists(fp):
        return None
    rows = json.load(open(fp))
    if not rows:
        return np.array([]), np.array([])
    rows = sorted(rows, key=lambda r: r["time"])
    times = np.array([r["time"] for r in rows], dtype=np.int64)
    rates = np.array([r["rate"] for r in rows], dtype=np.float64)
    return times, rates


def funding_bps_for_hold(funding_idx, coin, entry_t, exit_t):
    """Sum hourly funding received by a SHORT over (entry_t, exit_t]. Positive rate = longs
    pay shorts, so short receives +rate*1e4 bps per hour of exposure to that funding print."""
    if coin not in funding_idx:
        return None
    times, rates = funding_idx[coin]
    rows = [{"time": int(ts), "rate": float(rate)} for ts, rate in zip(times, rates)]
    result = FUND.funding_pnl(
        rows, side="short", entry_ts_ms=int(entry_t), exit_ts_ms=int(exit_t),
        exit_after_funding=True,
    )
    return result.value_bps if result.status in FUND.COMPLETE_STATUSES else None


# ------------------------------------------------------------ core sim ----

def simulate_exit(highs, lows, closes, i0, cap_hours):
    """From decision bar i0 (entry at closes[i0]), walk forward up to cap_hours bars.
    Returns (exit_idx, exit_price, reason) or None if insufficient forward data."""
    n = len(closes)
    if i0 + cap_hours >= n:
        return None
    entry_px = closes[i0]
    sl_px = entry_px * SL_MULT
    tp_px = entry_px * TP_MULT
    for k in range(1, cap_hours + 1):
        i = i0 + k
        if highs[i] >= sl_px:
            return i, sl_px, "stop_loss"
        if lows[i] <= tp_px:
            return i, tp_px, "take_profit"
    i = i0 + cap_hours
    return i, closes[i], "time_cap"


def cost_adjusted_bps(entry_px, exit_px, coin, entry_t, exit_t, funding_idx,
                       slip_each_bps=SLIP_PRIMARY_EACH, fee_bps=FEE_BPS):
    price_pnl_bps = (entry_px - exit_px) / entry_px * 1e4  # short
    funding_bps = funding_bps_for_hold(funding_idx, coin, entry_t, exit_t)
    if funding_bps is None:
        return None, price_pnl_bps, None
    net = price_pnl_bps + funding_bps - fee_bps - 2 * slip_each_bps
    return net, price_pnl_bps, funding_bps


# --------------------------------------------------------- signal build ----

def build_coin_frame(coin):
    df = load_candles(coin)
    if df is None or len(df) < MIN_HISTORY_HOURS + 24:
        return None
    df["runup_24"] = df["c"] / df["c"].rolling(24).min().shift(1) - 1.0
    df["runup_72"] = df["c"] / df["c"].rolling(72).min().shift(1) - 1.0
    return df


def window_of(t):
    for w, (s, e) in WINDOWS.items():
        if s <= t < e:
            return w
    return None


def eligible_from(df, coin_first_t):
    """Per-window eligibility mask: True at bar t if coin has >=30d history before
    window_start of the window containing t."""
    elig = pd.Series(True, index=df.index)
    for w, (s, e) in WINDOWS.items():
        if coin_first_t > s - MIN_HISTORY_HOURS * 3600_000:
            in_w = (df["t"] >= s) & (df["t"] < e)
            elig[in_w] = False
    return elig


def gen_signal_trades(frames, funding_idx, threshold, runup_col, cap_hours=CAP_HOURS_PRIMARY):
    trades = []
    for coin, df in frames.items():
        highs, lows, closes, ts = df["h"].values, df["l"].values, df["c"].values, df["t"].values
        runup = df[runup_col].values
        elig = df["_eligible"].values
        in_pos_until = -1
        prev = np.nan
        for i in range(len(df)):
            t = ts[i]
            if i <= in_pos_until:
                prev = runup[i]
                continue
            if not elig[i] or np.isnan(runup[i]):
                prev = runup[i]
                continue
            crossed = (not np.isnan(prev)) and prev < threshold and runup[i] >= threshold
            prev = runup[i]
            if not crossed:
                continue
            res = simulate_exit(highs, lows, closes, i, cap_hours)
            if res is None:
                continue
            exit_i, exit_px, reason = res
            entry_t, exit_t = ts[i], ts[exit_i]
            net, ppnl, fbps = cost_adjusted_bps(closes[i], exit_px, coin, entry_t, exit_t, funding_idx)
            if net is None:
                continue
            trades.append(dict(coin=coin, entry_t=entry_t, exit_t=exit_t, entry_i=i, exit_i=exit_i,
                                entry_px=closes[i], exit_px=exit_px, reason=reason,
                                net_bps=net, price_pnl_bps=ppnl, funding_bps=fbps,
                                window=window_of(entry_t)))
            in_pos_until = exit_i
    return pd.DataFrame(trades, columns=TRADE_COLUMNS)


def gen_random_trades(frames, funding_idx, n_target_per_window, cap_hours=CAP_HOURS_PRIMARY, seed_offset=0):
    """Uniform random (coin, hour) draws from the eligible grid, matched target count
    per window. No run-up filter. Same exit/cost mechanics."""
    rng = np.random.default_rng(20260726 + seed_offset)
    pool_by_window = {w: [] for w in WINDOWS}
    for coin, df in frames.items():
        elig = df["_eligible"].values
        ts = df["t"].values
        n = len(df)
        for i in range(n):
            if not elig[i]:
                continue
            if i + cap_hours >= n:
                continue
            w = window_of(ts[i])
            if w is None:
                continue
            pool_by_window[w].append((coin, i))
    trades = []
    for w, target in n_target_per_window.items():
        pool = pool_by_window.get(w, [])
        if not pool or target <= 0:
            continue
        idxs = rng.choice(len(pool), size=min(target, len(pool)), replace=False)
        for pi in idxs:
            coin, i = pool[pi]
            df = frames[coin]
            highs, lows, closes, ts = df["h"].values, df["l"].values, df["c"].values, df["t"].values
            res = simulate_exit(highs, lows, closes, i, cap_hours)
            if res is None:
                continue
            exit_i, exit_px, reason = res
            entry_t, exit_t = ts[i], ts[exit_i]
            net, ppnl, fbps = cost_adjusted_bps(closes[i], exit_px, coin, entry_t, exit_t, funding_idx)
            if net is None:
                continue
            trades.append(dict(coin=coin, entry_t=entry_t, exit_t=exit_t, entry_i=i, exit_i=exit_i,
                                entry_px=closes[i], exit_px=exit_px, reason=reason,
                                net_bps=net, price_pnl_bps=ppnl, funding_bps=fbps, window=w))
    return pd.DataFrame(trades, columns=TRADE_COLUMNS)


# -------------------------------------------------------------- metrics ----

def arm_metrics(trades: pd.DataFrame, group_col="window"):
    rows = []
    for w, g in trades.groupby(group_col):
        g = g.sort_values("entry_t")
        n = len(g)
        mean_bps = g["net_bps"].mean()
        std_bps = g["net_bps"].std(ddof=1) if n > 1 else np.nan
        wr = (g["net_bps"] > 0).mean()
        total = g["net_bps"].sum()
        sharpe = mean_bps / std_bps if std_bps and std_bps > 0 else np.nan
        cum = g["net_bps"].cumsum()
        dd = (cum - cum.cummax()).min() if n > 0 else 0.0
        by_coin = g.groupby("coin")["net_bps"].sum()
        top_coin_share = (by_coin.abs().max() / by_coin.abs().sum()) if by_coin.abs().sum() > 0 else np.nan
        top_coin = by_coin.abs().idxmax() if len(by_coin) else None
        drop_top_mean = g[g["coin"] != top_coin]["net_bps"].mean() if top_coin else mean_bps
        drop_top_total = g[g["coin"] != top_coin]["net_bps"].sum() if top_coin else total
        rows.append(dict(window=w, n=n, mean_bps=mean_bps, std_bps=std_bps, wr=wr, total_bps=total,
                          sharpe=sharpe, max_dd_bps=dd, top_coin=top_coin, top_coin_share=top_coin_share,
                          drop_top_coin_mean_bps=drop_top_mean, drop_top_coin_total_bps=drop_top_total))
    return pd.DataFrame(rows, columns=METRIC_COLUMNS)


def bootstrap_ci(x, n_boot=2000, seed=1):
    if len(x) == 0:
        return (np.nan, np.nan)
    rng = np.random.default_rng(seed)
    x = np.asarray(x)
    means = [rng.choice(x, size=len(x), replace=True).mean() for _ in range(n_boot)]
    return (np.percentile(means, 2.5), np.percentile(means, 97.5))


# ------------------------------------------------------- harness check ----

def harness_reproduction_check(frames, funding_idx):
    db_path = Path(INTEL_DB).resolve()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    q = """SELECT coin, detect_ts_ms, pnl_bps as ref_pnl_bps FROM hl_copy_book
           WHERE side='short' AND coin NOT IN ('BTC','ETH','SOL') AND status='closed'
           AND is_cold_start=0"""
    ref = pd.DataFrame([dict(r) for r in conn.execute(q)])
    conn.close()
    results = []
    for _, row in ref.iterrows():
        coin, t = row["coin"], row["detect_ts_ms"]
        df = frames.get(coin)
        if df is None:
            continue
        ts = df["t"].values
        idx = np.searchsorted(ts, t, side="right") - 1
        if idx < 0 or idx >= len(df):
            continue
        highs, lows, closes = df["h"].values, df["l"].values, df["c"].values
        res = simulate_exit(highs, lows, closes, idx, CAP_HOURS_PRIMARY)
        if res is None:
            continue
        exit_i, exit_px, reason = res
        net, ppnl, fbps = cost_adjusted_bps(closes[idx], exit_px, coin, ts[idx], ts[exit_i], funding_idx)
        if net is None:
            continue
        results.append(dict(coin=coin, ref_pnl_bps=row["ref_pnl_bps"], harness_net_bps=net, reason=reason))
    rdf = pd.DataFrame(results)
    return ref, rdf


# ------------------------------------------------------------------ main ----

def main(argv=None):
    global CACHE, OUT, INTEL_DB
    args = parse_args(argv)
    CACHE = str(args.cache_dir.resolve())
    OUT = str(args.output_dir.resolve())
    INTEL_DB = str(args.db_path.resolve())
    Path(OUT).mkdir(parents=True, exist_ok=True)
    alts = load_universe()
    print(f"alt universe from cache: {len(alts)}", flush=True)

    frames = {}
    funding_idx = {}
    for coin in alts:
        df = build_coin_frame(coin)
        if df is None:
            continue
        first_t = int(df["t"].iloc[0])
        df["_eligible"] = eligible_from(df, first_t)
        frames[coin] = df
        times, rates = (load_funding(coin) or (np.array([]), np.array([])))
        funding_idx[coin] = (times, rates)
    print(f"coins with usable candle history (>=30d): {len(frames)}", flush=True)
    funding_missing = [c for c in frames if c not in funding_idx or len(funding_idx[c][0]) == 0]
    print(f"coins with zero funding rows cached: {len(funding_missing)}", flush=True)

    # --- BTC regime series ---
    btc = load_candles("BTC")
    btc_ret_by_window = {}
    for w, (s, e) in WINDOWS.items():
        sub = btc[(btc["t"] >= s) & (btc["t"] < e)]
        if len(sub) < 2:
            btc_ret_by_window[w] = np.nan
            continue
        btc_ret_by_window[w] = float(sub["c"].iloc[-1] / sub["c"].iloc[0] - 1.0)

    # --- alt basket regime per window (equal-weight, eligible coins only) ---
    regime_rows = []
    for w, (s, e) in WINDOWS.items():
        rets = []
        for coin, df in frames.items():
            sub = df[(df["t"] >= s) & (df["t"] < e) & (df["_eligible"])]
            if len(sub) < 2:
                continue
            rets.append(float(sub["c"].iloc[-1] / sub["c"].iloc[0] - 1.0))
        basket_ret = float(np.median(rets)) if rets else np.nan
        label = (
            "unknown" if not rets or np.isnan(basket_ret)
            else "bearish" if basket_ret < -0.05
            else "bullish" if basket_ret > 0.05
            else "neutral"
        )
        regime_rows.append(dict(window=w, n_coins=len(rets), alt_basket_median_return=basket_ret,
                                 btc_return=btc_ret_by_window[w],
                                 alt_vs_btc_spread=basket_ret - btc_ret_by_window[w] if not np.isnan(btc_ret_by_window[w]) else np.nan,
                                 label=label))
    regime_df = pd.DataFrame(regime_rows)
    regime_df.to_csv(f"{OUT}/regime_by_window.csv", index=False)
    print(regime_df.to_string(index=False), flush=True)

    # --- primary signal: X=30%, N=72h, cap=8h ---
    primary = gen_signal_trades(frames, funding_idx, 0.30, "runup_72", CAP_HOURS_PRIMARY)
    primary.to_csv(f"{OUT}/trades_signal_primary.csv", index=False)
    print(f"primary signal trades: n={len(primary)}", flush=True)

    # --- sensitivity grid ---
    sens_rows = []
    for X in (0.20, 0.30, 0.50):
        for col, label in (("runup_24", "24h"), ("runup_72", "72h")):
            tr = gen_signal_trades(frames, funding_idx, X, col, CAP_HOURS_PRIMARY)
            m = arm_metrics(tr)
            m["threshold"] = X
            m["runup_window"] = label
            sens_rows.append(m)
    sens_df = pd.concat(sens_rows, ignore_index=True) if sens_rows else pd.DataFrame()
    sens_df.to_csv(f"{OUT}/sensitivity_grid.csv", index=False)

    # --- 24h cap robustness variant on primary threshold ---
    primary_24hcap = gen_signal_trades(frames, funding_idx, 0.30, "runup_72", CAP_HOURS_ROBUST)
    primary_24hcap.to_csv(f"{OUT}/trades_signal_primary_24hcap.csv", index=False)

    # --- baselines matched to primary signal counts per window ---
    n_by_window = primary.groupby("window").size().to_dict() if len(primary) else {}
    random_baseline = gen_random_trades(frames, funding_idx, n_by_window, CAP_HOURS_PRIMARY, seed_offset=1)
    random_baseline.to_csv(f"{OUT}/trades_random_baseline.csv", index=False)

    nofilter_target = {w: 750 for w in WINDOWS}
    nofilter_baseline = gen_random_trades(frames, funding_idx, nofilter_target, CAP_HOURS_PRIMARY, seed_offset=2)
    nofilter_baseline.to_csv(f"{OUT}/trades_nofilter_baseline.csv", index=False)

    # --- metrics tables ---
    m_signal = arm_metrics(primary); m_signal["arm"] = "signal_primary"
    m_random = arm_metrics(random_baseline); m_random["arm"] = "random_matched"
    m_nofilter = arm_metrics(nofilter_baseline); m_nofilter["arm"] = "nofilter_drift"
    m_signal24 = arm_metrics(primary_24hcap); m_signal24["arm"] = "signal_primary_24hcap"
    all_metrics = pd.concat([m_signal, m_random, m_nofilter, m_signal24], ignore_index=True)
    all_metrics = all_metrics.merge(regime_df[["window", "label"]], on="window", how="left")
    all_metrics.to_csv(f"{OUT}/arm_metrics_by_window.csv", index=False)
    print(all_metrics.to_string(index=False), flush=True)

    # --- regime-gate ablation: signal restricted to bearish windows vs all windows ---
    bearish_windows = set(regime_df[regime_df["label"] == "bearish"]["window"])
    gated = primary[primary["window"].isin(bearish_windows)]
    ablation = pd.DataFrame([
        dict(arm="gated_bearish_only", n=len(gated), mean_bps=gated["net_bps"].mean() if len(gated) else np.nan,
             wr=(gated["net_bps"] > 0).mean() if len(gated) else np.nan, total_bps=gated["net_bps"].sum()),
        dict(arm="ungated_all_windows", n=len(primary), mean_bps=primary["net_bps"].mean() if len(primary) else np.nan,
             wr=(primary["net_bps"] > 0).mean() if len(primary) else np.nan, total_bps=primary["net_bps"].sum()),
    ])
    ablation.to_csv(f"{OUT}/regime_gate_ablation.csv", index=False)
    print(ablation.to_string(index=False), flush=True)

    # --- cost sensitivity on primary trades (recompute net_bps at different slippage) ---
    cost_rows = []
    for slip in (5.0, 15.0, 30.0):
        adj = primary.copy()
        adj["net_bps_adj"] = adj["price_pnl_bps"] + adj["funding_bps"] - FEE_BPS - 2 * slip
        for w, g in adj.groupby("window"):
            cost_rows.append(dict(window=w, slip_each_bps=slip, n=len(g), mean_bps=g["net_bps_adj"].mean(),
                                   wr=(g["net_bps_adj"] > 0).mean(), total_bps=g["net_bps_adj"].sum()))
    cost_df = pd.DataFrame(cost_rows)
    cost_df.to_csv(f"{OUT}/cost_sensitivity.csv", index=False)

    # --- bootstrap CIs on OOS windows for signal vs random ---
    ci_rows = []
    for arm_name, df in (("signal_primary", primary), ("random_matched", random_baseline),
                          ("nofilter_drift", nofilter_baseline)):
        oos = df[df["window"].isin(OOS_WINDOWS)]
        lo, hi = bootstrap_ci(oos["net_bps"].values) if len(oos) else (np.nan, np.nan)
        ci_rows.append(dict(arm=arm_name, scope="OOS_pooled_W0W1W2", n=len(oos),
                             mean_bps=oos["net_bps"].mean() if len(oos) else np.nan, ci_lo=lo, ci_hi=hi))
    ci_df = pd.DataFrame(ci_rows)
    ci_df.to_csv(f"{OUT}/bootstrap_ci_oos.csv", index=False)
    print(ci_df.to_string(index=False), flush=True)

    # --- harness reproduction check ---
    ref, rep = harness_reproduction_check(frames, funding_idx)
    ref.to_csv(f"{OUT}/harness_check_reference.csv", index=False)
    rep.to_csv(f"{OUT}/harness_check_repro.csv", index=False)
    print(f"harness check: reference n={len(ref)} mean_ref_bps={ref['ref_pnl_bps'].mean():.1f} "
          f"wr_ref={(ref['ref_pnl_bps']>0).mean():.3f}", flush=True)
    if len(rep):
        print(f"harness check: repro n={len(rep)} mean_harness_bps={rep['harness_net_bps'].mean():.1f} "
              f"wr_harness={(rep['harness_net_bps']>0).mean():.3f}", flush=True)
    else:
        print("harness check: repro n=0 (no matching coin/timestamp coverage)", flush=True)

    provenance = analysis_provenance(Path(CACHE))
    (Path(OUT) / "analysis_provenance.json").write_text(
        json.dumps(provenance, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    print(f"evidence_status={provenance['evidence_status']}", flush=True)
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
