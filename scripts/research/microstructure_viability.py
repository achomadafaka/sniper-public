"""Anomaly feature viability study.

Computes pre-trade microstructure features for each fill in a pooled sample
(low_buy_trades ∪ followed_wallet_activity BUYs, last 90d), then correlates
them with 24h post-fill price drift on the bought outcome side.

Outputs:
  docs/research/assets/microstructure_fills.csv   (one row per fill with features)
  docs/research/assets/microstructure_results.csv (per-feature summary stats)
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from scipy import stats

DB = os.path.join(os.path.dirname(__file__), "..", "..", "sniper_tracker.db")
ASSETS = os.path.join(os.path.dirname(__file__), "..", "..", "docs", "research", "assets")
os.makedirs(ASSETS, exist_ok=True)

PRE_WINDOW_S = 3600            # 1 hour pre-fill
POST_LO_S = 20 * 3600          # 20h
POST_HI_S = 28 * 3600          # 28h
NOW_TS = int(datetime.now(timezone.utc).timestamp())
NINETY_D_AGO = NOW_TS - 90 * 86400


def run() -> None:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row

    print("[1/5] Building unified trade price tape (condition_id, outcome, ts, price)…")
    c.executescript(
        """
        DROP TABLE IF EXISTS tmp_tape;
        CREATE TEMP TABLE tmp_tape AS
        SELECT condition_id, outcome, timestamp AS ts, price FROM low_buy_trades
          WHERE outcome IS NOT NULL AND outcome != ''
        UNION ALL
        SELECT condition_id, outcome, activity_ts AS ts, price FROM followed_wallet_activity
          WHERE activity_type='TRADE' AND side IN ('BUY','SELL') AND outcome != '';
        CREATE INDEX IF NOT EXISTS idx_tmp_tape ON tmp_tape(condition_id, outcome, ts);
        """
    )
    tape_n = c.execute("SELECT COUNT(*) FROM tmp_tape").fetchone()[0]
    print(f"    tape rows: {tape_n:,}")

    print("[2/5] Pulling candidate fills (pooled BUYs, last 90d, with outcome set)…")
    fills_sql = """
        SELECT
          'lbt' AS src, transaction_hash AS id, proxy_wallet AS wallet,
          condition_id, outcome, price AS fill_price, notional AS fill_notional,
          timestamp AS fill_ts
        FROM low_buy_trades
        WHERE outcome IS NOT NULL AND outcome != ''
          AND timestamp >= ?
        UNION ALL
        SELECT
          'fwa' AS src, activity_key AS id, wallet,
          condition_id, outcome, price AS fill_price, usdc_size AS fill_notional,
          activity_ts AS fill_ts
        FROM followed_wallet_activity
        WHERE activity_type='TRADE' AND side='BUY' AND outcome != ''
          AND activity_ts >= ?
    """
    fills = pd.read_sql_query(fills_sql, c, params=(NINETY_D_AGO, NINETY_D_AGO))
    fills = fills.drop_duplicates(subset=["condition_id", "outcome", "wallet", "fill_ts"])
    print(f"    fills after dedup: {len(fills):,}")

    print("[3/5] Joining market_metadata for volume_24h and end_date_iso…")
    md = pd.read_sql_query(
        "SELECT condition_id, volume_24h, end_date_iso, last_synced_ts FROM market_metadata",
        c,
    )
    fills = fills.merge(md, on="condition_id", how="left")

    # Feature 3: time-to-resolution (days).
    def _end_ts(iso: str) -> float:
        if not iso:
            return np.nan
        try:
            s = iso.replace("Z", "+00:00")
            return datetime.fromisoformat(s).timestamp()
        except Exception:
            return np.nan

    fills["end_ts"] = fills["end_date_iso"].fillna("").apply(_end_ts)
    fills["days_to_resolution"] = (fills["end_ts"] - fills["fill_ts"]) / 86400.0

    # Feature 2: notional-to-volume ratio (volume_24h is single-snapshot — caveat in memo)
    fills["vol_24h"] = fills["volume_24h"].fillna(0.0)
    fills["notional_to_vol"] = np.where(
        fills["vol_24h"] > 0, fills["fill_notional"] / fills["vol_24h"], np.nan
    )

    print("[4/5] Computing pre-trade 1h slope and 24h post-fill price per fill…")
    # Pull tape into pandas once for efficient per-fill window ops.
    tape = pd.read_sql_query("SELECT condition_id, outcome, ts, price FROM tmp_tape", c)
    # clamp bad prices
    tape = tape[(tape["price"] > 0.0) & (tape["price"] < 1.0)].copy()
    tape.sort_values(["condition_id", "outcome", "ts"], inplace=True)

    grouped = {k: g.reset_index(drop=True) for k, g in tape.groupby(["condition_id", "outcome"])}

    pre_slopes = np.full(len(fills), np.nan)
    pre_counts = np.zeros(len(fills), dtype=int)
    post_prices = np.full(len(fills), np.nan)
    post_counts = np.zeros(len(fills), dtype=int)

    fill_arr = fills[["condition_id", "outcome", "fill_ts", "fill_price"]].to_numpy()
    for i, (cid, oc, ts, fp) in enumerate(fill_arr):
        g = grouped.get((cid, oc))
        if g is None or len(g) == 0:
            continue
        ts_arr = g["ts"].to_numpy()
        pr_arr = g["price"].to_numpy()
        # pre window
        pre_lo, pre_hi = ts - PRE_WINDOW_S, ts - 1
        pm = (ts_arr >= pre_lo) & (ts_arr <= pre_hi)
        if pm.sum() >= 2:
            x = ts_arr[pm].astype(float)
            y = pr_arr[pm].astype(float)
            # OLS slope in price-per-second, rescale to price-per-hour
            x_mean = x.mean()
            x_var = ((x - x_mean) ** 2).sum()
            if x_var > 0:
                slope = ((x - x_mean) * (y - y.mean())).sum() / x_var
                pre_slopes[i] = slope * 3600.0  # per hour
            pre_counts[i] = int(pm.sum())
        # post window
        post_lo, post_hi = ts + POST_LO_S, ts + POST_HI_S
        pm2 = (ts_arr >= post_lo) & (ts_arr <= post_hi)
        if pm2.any():
            post_prices[i] = float(pr_arr[pm2].mean())
            post_counts[i] = int(pm2.sum())

    fills["pre_slope_per_h"] = pre_slopes
    fills["pre_trade_count_1h"] = pre_counts
    fills["post_price_24h"] = post_prices
    fills["post_trade_count"] = post_counts

    # Outcome variable: 24h post-fill drift on the bought side.
    # Positive = price moved in buyer's favor (bought outcome got more likely).
    # This is NOT signed drift-to-resolution (market_outcomes has sparse coverage).
    fills["drift_24h"] = fills["post_price_24h"] - fills["fill_price"]

    # Feature 4 (news gate): news_items is empty → omitted, flagged in memo.

    # Save raw fills with features.
    fills_out = fills.copy()
    fills_out_path = os.path.join(ASSETS, "microstructure_fills.csv")
    fills_out.to_csv(fills_out_path, index=False)
    print(f"    wrote {fills_out_path} ({len(fills_out):,} rows)")

    # Analysis subset: fills with BOTH pre slope and post price.
    sub = fills.dropna(subset=["pre_slope_per_h", "post_price_24h", "drift_24h"]).copy()
    print(f"[5/5] Fills with full coverage (pre slope + post price): {len(sub):,}")

    def feature_stats(df: pd.DataFrame, feat: str, outcome: str = "drift_24h") -> dict:
        d = df.dropna(subset=[feat, outcome])
        n = len(d)
        if n < 20:
            return {"feature": feat, "n": n, "note": "n<20"}
        x = d[feat].to_numpy(float)
        y = d[outcome].to_numpy(float)
        pr, pp = stats.pearsonr(x, y)
        sr, sp = stats.spearmanr(x, y)
        # top/bottom 10% buckets by feature
        lo_cut = np.quantile(x, 0.10)
        hi_cut = np.quantile(x, 0.90)
        bot_y = y[x <= lo_cut]
        top_y = y[x >= hi_cut]
        top_mean = float(top_y.mean()) if len(top_y) else np.nan
        bot_mean = float(bot_y.mean()) if len(bot_y) else np.nan
        delta = top_mean - bot_mean
        # bootstrap 95% CI on the top-minus-bot delta
        rng = np.random.default_rng(42)
        boots = []
        top_arr = top_y
        bot_arr = bot_y
        B = 500
        for _ in range(B):
            if len(top_arr) and len(bot_arr):
                ts = rng.choice(top_arr, size=len(top_arr), replace=True).mean()
                bs = rng.choice(bot_arr, size=len(bot_arr), replace=True).mean()
                boots.append(ts - bs)
        boots = np.array(boots)
        ci_lo, ci_hi = (float(np.quantile(boots, 0.025)), float(np.quantile(boots, 0.975))) if len(boots) else (np.nan, np.nan)
        return {
            "feature": feat,
            "n": n,
            "pearson_r": float(pr), "pearson_p": float(pp),
            "spearman_rho": float(sr), "spearman_p": float(sp),
            "lo_cut": float(lo_cut), "hi_cut": float(hi_cut),
            "bot_mean_drift": bot_mean, "top_mean_drift": top_mean,
            "delta": float(delta),
            "ci_lo": ci_lo, "ci_hi": ci_hi,
            "bot_n": int(len(bot_y)), "top_n": int(len(top_y)),
        }

    results = [
        feature_stats(sub, "pre_slope_per_h"),
        feature_stats(sub, "notional_to_vol"),
        feature_stats(sub, "days_to_resolution"),
    ]

    res_df = pd.DataFrame(results)
    res_path = os.path.join(ASSETS, "microstructure_results.csv")
    res_df.to_csv(res_path, index=False)
    print(f"    wrote {res_path}")

    # Also: baseline — what's the mean and std of drift_24h overall?
    overall = {
        "drift_mean": float(sub["drift_24h"].mean()),
        "drift_median": float(sub["drift_24h"].median()),
        "drift_std": float(sub["drift_24h"].std()),
        "drift_n": int(len(sub)),
    }
    print("\nDRIFT BASELINE:", overall)

    # Feature-coverage summary
    cov = {
        "total_fills": int(len(fills)),
        "with_pre_slope": int(fills["pre_slope_per_h"].notna().sum()),
        "with_post_price": int(fills["post_price_24h"].notna().sum()),
        "with_both_slope_and_post": int(len(sub)),
        "with_vol_24h": int((fills["vol_24h"] > 0).sum()),
        "with_end_date_iso": int(fills["end_ts"].notna().sum()),
    }
    print("\nCOVERAGE:", cov)

    print("\n=== PER-FEATURE RESULTS ===")
    for r in results:
        print(r)


if __name__ == "__main__":
    run()
