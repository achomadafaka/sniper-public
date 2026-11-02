"""WO-15 research-only entry-quality attribution candidate.

This does not approve, revoke, size, or route trades. It normalizes each honest paper copy to one
fixed notional and values its exit through the chain's real-close-calibrated fill model. The paper
position's entry price already occurred after the measured copy lag, so lag is reported/stratified
and never double-penalized. Missing lag, open positions, and uncalibrated chains stay explicit.
"""
from __future__ import annotations

import math
import statistics

from sniper.analysis.exit_flex_v5 import market_cap_band
from sniper.analysis.wallet_protocol.statistics import bootstrap_ci
from sniper.services.memecoin_v5_replay_gate import calibrate_fill_model, real_close_samples

FIXED_NOTIONAL_USD = 50.0
MIN_WALLET_CLOSES = 20
MIN_STRATUM_CLOSES = 5


def _chain(row):
    return str(row.get("network_id") or "solana")


def _lag_bucket(value):
    if value <= 2:
        return "le_2s"
    if value <= 10:
        return "2_to_10s"
    return "gt_10s"


def _summary(values):
    if not values:
        return {"n": 0, "net_fill_modeled_pnl": None, "mean_pnl": None,
                "ci95_low": None, "ci95_high": None}
    point, lo, hi = bootstrap_ci(values, n_iter=2_000)
    return {
        "n": len(values), "net_fill_modeled_pnl": round(sum(values), 6),
        "mean_pnl": round(float(point), 6), "ci95_low": round(float(lo), 6),
        "ci95_high": round(float(hi), 6),
    }


def evaluate_wallet_candidate(positions, fill_models, *, fixed_notional=FIXED_NOTIONAL_USD,
                              min_closes=MIN_WALLET_CLOSES,
                              min_stratum_closes=MIN_STRATUM_CLOSES):
    """Pure candidate evaluation over one wallet's paper positions.

    Closed positions need a positive original notional, finite realized PnL, a known non-negative
    actual entry lag, and a calibrated chain fill model. Open rows are right-censored, not marked to
    market. Returns observation analytics with an explicit zero-authority marker.
    """
    fixed_notional = float(fixed_notional)
    if not math.isfinite(fixed_notional) or fixed_notional <= 0:
        raise ValueError("fixed_notional must be finite and positive")
    values = []
    lags = []
    strata = {}
    lag_values = {}
    n_censored = n_missing_lag = n_uncalibrated = n_invalid = 0
    for raw in positions or ():
        row = dict(raw)
        if bool(row.get("is_open")):
            n_censored += 1
            continue
        try:
            entry_notional = float(row.get("entry_notional"))
            realized_pnl = float(row.get("realized_pnl"))
        except (TypeError, ValueError):
            n_invalid += 1
            continue
        if not (math.isfinite(entry_notional) and entry_notional > 0
                and math.isfinite(realized_pnl)):
            n_invalid += 1
            continue
        try:
            lag = float(row.get("entry_lag_s"))
        except (TypeError, ValueError):
            n_missing_lag += 1
            continue
        if not math.isfinite(lag) or lag < 0:
            n_missing_lag += 1
            continue
        chain = _chain(row)
        model = fill_models.get(chain)
        if model is None or not model.calibrated:
            n_uncalibrated += 1
            continue
        gross_return = realized_pnl / entry_notional
        gross_exit = max(0.0, fixed_notional * (1.0 + gross_return))
        pnl = model.exit_value(gross_exit) - fixed_notional
        if not math.isfinite(pnl):
            n_invalid += 1
            continue
        values.append(pnl)
        lags.append(lag)
        band = market_cap_band(row.get("entry_mcap"))
        strata.setdefault((chain, band), []).append(pnl)
        lag_values.setdefault(_lag_bucket(lag), []).append(pnl)

    headline = _summary(values)
    ready = len(values) >= int(min_closes)
    missing_evidence = n_missing_lag > 0 or n_uncalibrated > 0
    status = ("READY" if ready else
              "BLOCKED_MISSING_EVIDENCE" if not values and missing_evidence else
              "INSUFFICIENT_SAMPLE")
    by_chain_band = []
    for (chain, band), bucket in sorted(strata.items()):
        item = {"chain": chain, "coin_band": band, **_summary(bucket)}
        item["sample_floor"] = int(min_stratum_closes)
        item["eligible"] = len(bucket) >= int(min_stratum_closes)
        by_chain_band.append(item)
    return {
        "metric": "fixed_notional_fill_modeled_copy_pnl_candidate",
        "status": status,
        "fixed_notional_usd": fixed_notional,
        "sample_floor": int(min_closes),
        "n_eligible": len(values),
        "n_censored": n_censored,
        "n_missing_entry_lag": n_missing_lag,
        "n_uncalibrated_fill_model": n_uncalibrated,
        "n_invalid": n_invalid,
        "median_entry_lag_s": statistics.median(lags) if lags else None,
        **{k: v for k, v in headline.items() if k != "n"},
        "by_chain_band": by_chain_band,
        "by_entry_lag": {key: _summary(bucket) for key, bucket in sorted(lag_values.items())},
        "review_flag": ("NEGATIVE_CANDIDATE_REVIEW"
                        if ready and (headline["net_fill_modeled_pnl"] or 0) < 0 else None),
        "governance_authority": False,
        "entry_lag_treatment": "actual_lagged_entry; missing_lag_excluded; no_double_penalty",
        "censoring": "open_positions_excluded_no_mark",
    }


def wallet_candidates_from_db(conn, *, fixed_notional=FIXED_NOTIONAL_USD,
                              min_closes=MIN_WALLET_CLOSES):
    """Batched read-only candidate metrics keyed by wallet id; current approval metric unchanged."""
    rows = [dict(row) for row in conn.execute(
        "SELECT id,source_user_id,network_id,entry_notional,realized_pnl,entry_lag_s,entry_mcap,is_open "
        "FROM memecoin_positions WHERE strategy='alpha_b' "
        "AND COALESCE(execution_mode,'paper')<>'real' "
        "AND id NOT IN (SELECT position_id FROM memecoin_fills WHERE fill_source='fomo') "
        "ORDER BY source_user_id,id")]
    if not rows:
        return {}
    from sniper.settings import memecoin as cfg
    cap_pct = float(cfg.MEMECOIN_SLIPPAGE_CAP) * 100.0
    models = {}
    for chain in sorted({_chain(row) for row in rows}):
        models[chain] = calibrate_fill_model(
            real_close_samples(conn, chain), impact_cap_pct=cap_pct,
            haircut_frac=float(cfg.MEMECOIN_SLIPPAGE_CAP), min_closes=20)
    grouped = {}
    for row in rows:
        grouped.setdefault(row.get("source_user_id"), []).append(row)
    return {
        uid: evaluate_wallet_candidate(
            wallet_rows, models, fixed_notional=fixed_notional, min_closes=min_closes)
        for uid, wallet_rows in grouped.items() if uid
    }
