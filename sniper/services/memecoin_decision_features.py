"""decision_features_v1: what a fast lane already held at its decision (spec 2026-10-14).

MEASUREMENT ONLY. A lane captures its held inputs at the decision as a partial of a builder here
(no read, lock, clock or serialization on the decision path); EntryAttemptTrace.finish calls it
with the trace's own decision_at, after the outcome, and writes trace_json.decision_features. No
gate, classification or funded contract reads it. Unknown is null with `<field>_reason`, never a
default; a known zero stays zero.
"""
from __future__ import annotations

import math

SCHEMA = "decision_features_v1"
_STATS_FIELDS = ("pair_age_s", "volume_m5_usd", "volume_h1_usd", "liquidity_usd", "stats_age_s")


def _num(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) and out >= 0 else None


def _null(out, field, reason):
    out[field] = None
    out[field + "_reason"] = reason


def _finish(out, their_usd):
    from sniper.services.memecoin_market_state import lifecycle_for
    age = out.get("pair_age_s")
    if age is None:
        _null(out, "lifecycle", out.get("pair_age_s_reason") or "not_held")
    elif age > 86400:
        _null(out, "lifecycle", "runup_not_held")    # revival vs mature needs the runup
    else:
        out["lifecycle"] = lifecycle_for(age, None)
    liquidity, paid = out.get("liquidity_usd"), _num(their_usd)
    out["source_impact_pct"] = paid / liquidity * 100.0 if liquidity and paid is not None else None
    return out


def from_cached_stats(*, decision_at, lane, held, their_usd):
    """The lane's cached provider stats. `held`: {"entry": (stats, fetched_at host epoch) | None,
    "read_at": host epoch of the decision's read, "source": "lane_cache" | "inline_read"}; no
    "entry" key = the decision never read stats."""
    from sniper.services.memecoin_real_runner import _pair_age_s
    out = {"schema": SCHEMA, "lane": lane, "decision_at": decision_at,
           "stats_source": held.get("source")}
    if "entry" not in held or not held["entry"]:
        reason = "stats_not_read" if "entry" not in held else "stats_not_cached"
        for field in _STATS_FIELDS:
            _null(out, field, reason)
        return _finish(out, their_usd)
    stats, fetched_at = held["entry"]
    age = _pair_age_s(stats, decision_at)
    if age is None:
        _null(out, "pair_age_s", "not_in_stats")
    else:
        out["pair_age_s"], out["pair_age_basis"] = age, "provider_pair_created_at"
    for field, key in (("volume_m5_usd", "volume_m5"), ("volume_h1_usd", "volume_h1")):
        value = _num(stats.get(key))
        if value is None:
            _null(out, field, "not_in_stats")
        else:
            out[field] = value
    liquidity = _num(stats.get("liquidity"))
    if liquidity is None:
        _null(out, "liquidity_usd", "not_in_stats")
    else:
        out["liquidity_usd"], out["liquidity_basis"] = liquidity, "provider_cached"
    try:
        stats_age = _num(float(held["read_at"]) - float(fetched_at))
    except (KeyError, TypeError, ValueError):
        stats_age = None
    if stats_age is None:
        _null(out, "stats_age_s", "fetched_at_unknown")
    else:
        out["stats_age_s"] = stats_age
    return _finish(out, their_usd)


def resolve(capture, decision_at):
    """Build a capture at trace finish. Never raises: a failure is recorded, the trace is kept."""
    try:
        got = capture(decision_at=decision_at)
    except Exception as exc:
        return {"schema": SCHEMA, "error": type(exc).__name__}
    return got if isinstance(got, dict) else {"schema": SCHEMA, "error": "not_a_dict"}


_WSOL = "So11111111111111111111111111111111111111112"
_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
_SLOT_NOT_HELD = ("volume_m5_usd", "volume_h1_usd", "stats_age_s")   # no provider stats on this lane


def _atomic(value):
    return value if type(value) is int and value >= 0 else None


def _epoch(iso):
    from datetime import datetime
    return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()


def from_slot_buy(*, decision_at, buy, their_usd, sol_usd, pool_facts, migration_observed_at):
    """The Solana slot lane's own buy event and caches. Curve reserves are POST-trade (TradeEvent);
    PumpSwap `real_*` are the pool's PRE-trade reserves (BuyEvent) plus the cached pool's virtual
    quote. Pair age exists only for a PumpSwap pool whose migration the lane heard live."""
    venue = getattr(buy, "venue", None)
    out = {"schema": SCHEMA, "lane": "slot_lane", "decision_at": decision_at, "venue": venue}
    for field in _SLOT_NOT_HELD:
        _null(out, field, "not_held")
    sol = _num(sol_usd)
    if venue == "pump_curve":
        for field, attr in (("curve_virtual_quote_atomic", "virtual_sol_reserves"),
                            ("curve_virtual_token_atomic", "virtual_token_reserves"),
                            ("curve_real_quote_atomic", "real_sol_reserves"),
                            ("curve_real_token_atomic", "real_token_reserves")):
            out[field] = _atomic(getattr(buy, attr, None))
        if out["curve_virtual_quote_atomic"] == 0:     # a non-SOL quote curve (quote_curve_completed)
            out["quote_mint"], out["quote_decimals"] = getattr(buy, "quote_mint", None), None
        else:
            out["quote_mint"], out["quote_decimals"] = _WSOL, 9
        real = out["curve_real_quote_atomic"]
        if out["quote_mint"] == _WSOL and real is not None and sol:
            out["liquidity_usd"] = real / 1e9 * sol
            out["liquidity_basis"] = "curve_post_trade_reserves"
        else:
            _null(out, "liquidity_usd", "quote_usd_not_held")
        _null(out, "pair_age_s", "not_in_event")
    elif venue == "pump_amm":
        pool = pool_facts or {}
        out["pool_quote_reserve_atomic"] = _atomic(getattr(buy, "real_sol_reserves", None))
        out["pool_base_reserve_atomic"] = _atomic(getattr(buy, "real_token_reserves", None))
        out["pool_virtual_quote_atomic"] = _atomic(pool.get("virtual_quote"))
        out["quote_mint"], out["quote_decimals"] = pool.get("quote_mint"), pool.get("quote_decimals")
        quote_usd = sol if out["quote_mint"] == _WSOL else 1.0 if out["quote_mint"] == _USDC else None
        quote, decimals = out["pool_quote_reserve_atomic"], out["quote_decimals"]
        if quote_usd and quote is not None and type(decimals) is int:
            out["liquidity_usd"] = 2.0 * quote / 10 ** decimals * quote_usd
            out["liquidity_basis"] = "pool_pre_trade_reserves"
        else:
            _null(out, "liquidity_usd", "quote_usd_not_held")
        if migration_observed_at is None:
            _null(out, "pair_age_s", "migration_not_observed")
        else:
            age = _epoch(decision_at) - float(migration_observed_at)
            if age < 0:
                _null(out, "pair_age_s", "migration_after_decision")
            else:
                out["pair_age_s"], out["pair_age_basis"] = age, "migration_observed"
    else:
        _null(out, "liquidity_usd", "not_held")
        _null(out, "pair_age_s", "not_held")
    return _finish(out, their_usd)
