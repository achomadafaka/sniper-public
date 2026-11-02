"""Lane A — the promotion shadow: what the funded path WOULD have done with a disarmed wallet.

THE PROBLEM THIS EXISTS FOR (2026-09-23). The paper lanes opened a position for every signal
regardless of the gates: 2,242 of 2,346 paper rows in a week came from attempts the funded path had
REFUSED. A wallet's paper record therefore answered "were its coins good", never "would we have
taken them", and a promotion read built on it measured the wrong thing. The published example was a
disarmed cohort showing +$1,901 of paper PnL, 74% of it one wallet whose real record is -$223 over
14 funded closes.

WHAT THIS LANE DOES INSTEAD. A signal relayed for a wallet that is not armed stops at the approval
check, so the funded path never evaluates its gates -- the `funded_entry_shadow_v1` substage on
those attempts carries fifteen gate families and every one reads "unknown". This module replays the
gates that ARE reproducible, using the funded path's OWN evaluators rather than a second
implementation, and admits a paper position only when every reproducible gate passes. A refused
signal produces a provenance row and no position.

⚠️ ADMISSION HERE IS LOOSER THAN FUNDED, ALWAYS, AND EVERY READOUT MUST SAY SO. Two families cannot
be reproduced after the fact:
  * the CHASE families need the price at the decision instant, which nobody fetched and which no
    longer exists. They are computed from the evaluation-time quote, stored as evidence, and
    recorded as `unknown_lagged`. They can never admit or refuse.
  * anything the market read could not answer is `unreadable`, which is NOT a refusal. A read we
    could not make must never be counted as the gates saying no.
The reentry families ARE reproducible, because they are reads of our own ledger as of the decision
stamp, and they are evaluated here.

NOTHING IN THIS MODULE TOUCHES THE FUNDED PATH. It runs in its own worker off the hot path, writes
only `memecoin_promotion_lane`, and never opens a row in `memecoin_positions`.
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger("memecoin_promotion_lane")

LANE_VERSION = "promotion-lane-v1"

# The fifteen families the funded shadow names, split by what this lane can honestly say about each.
# `m.EXEMPTIBLE_GATE_FAMILIES` is the source of the names; this split is the source of the honesty.
REPRODUCIBLE = (
    "coin_band_unreservable",
    "source_buy_floor",
    "m5_turnover_under_floor",
    "m5_turnover_over_ceiling",
    "m5_txns_under_floor",
    "reentry_cooldown",
    "losing_reentry",
    "winner_reentry_daily_cap",
)
# Needs a price from the decision instant. Computed, stored, never decisive.
LAGGED = ("chase_first_seen", "chase_past_entry", "winner_reentry_price")
# Evaluated by the funded path from state this lane does not carry.
NOT_EVALUATED = ("coin_age_ceiling", "sell_share", "turnover_under_floor", "m5_txns_unreadable")

PASS, FAIL, UNKNOWN, LAGGED_STATE = "pass", "fail", "unknown", "unknown_lagged"


class Unreadable(Exception):
    """The market read failed, so the gates were never asked. Never a refusal."""


def _f(value, *, positive=False):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    if positive and number <= 0.0:
        return None
    return number


def evaluate_gates(*, facts, market, source_size_usd, network_id, coin_band, source_user_id,
                   ledger, cfg, source_mcap=None, prior_net_usd=None):
    """Replay the reproducible gates. Returns (states, inputs, refused_family).

    `market` is the freshly fetched market read; `facts` is the attempt's stored
    `classification_json.facts`. `ledger` answers the reentry questions AS OF the decision stamp.
    Raises `Unreadable` when the market read carries nothing to gate on -- that is not a refusal.
    """
    from sniper.services import memecoin_real_runner as rr

    mcap = _f(market.get("market_cap"), positive=True) or _f(facts.get("effective_mcap"), positive=True)
    vol_h1 = _f(market.get("volume_h1"))
    vol_m5 = _f(market.get("volume_m5"))
    if mcap is None and vol_h1 is None and vol_m5 is None:
        raise Unreadable("no market data")

    states = {name: UNKNOWN for name in REPRODUCIBLE}
    states.update({name: LAGGED_STATE for name in LAGGED})
    states.update({name: UNKNOWN for name in NOT_EVALUATED})
    inputs = {"effective_mcap_usd": mcap, "raw_m5_volume_usd": vol_m5, "volume_h1_usd": vol_h1,
              "source_size_usd": source_size_usd, "coin_band": coin_band,
              "network_id": str(network_id)}
    refused = None

    # 1. The mcap band. The funded path's own decision, ceiling and floor read per wallet.
    ceiling = rr._wallet_mcap_ceiling(cfg.MEMECOIN_REAL_MAX_MCAP_USD_BY_WALLET, network_id,
                                      source_user_id, cfg.MEMECOIN_REAL_MAX_MCAP_USD)
    floor = rr._wallet_mcap_floor(cfg.MEMECOIN_REAL_MIN_MCAP_USD_BY_WALLET, network_id,
                                  source_user_id, cfg.MEMECOIN_REAL_MIN_MCAP_USD)
    allow, effective_mc, reason = rr.mcap_entry_decision(mcap, None, ceiling, floor)
    inputs["mcap_ceiling_usd"], inputs["mcap_floor_usd"] = ceiling, floor
    states["coin_band_unreservable"] = PASS if allow else FAIL
    if not allow:
        refused = refused or ("coin_band_unreservable", reason)

    # 2. The WHOLE source-buy family, through the funded path's own evaluator. Running only the
    #    absolute band here passed buys the funded path refuses -- cap $200,000, buy $900, ratio
    #    0.008 needs $1,600 (2026-09-23 review, R4). `source_mcap` and `prior_net_usd` are the
    #    CAUSAL inputs the caller read as of the decision, never the late market read: the funded
    #    leg judges against the classification's own mcap.
    # The injected config's floor, unless this wallet has its own (MEMECOIN_REAL_MIN_SOURCE_BUY_USD_BY_WALLET).
    from sniper.settings.memecoin import source_buy_floor_override
    override = source_buy_floor_override(network_id, source_user_id)
    abs_min = (override if override is not None
               else float(getattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_USD", 0.0) or 0.0))
    abs_max = float(getattr(cfg, "MEMECOIN_REAL_MAX_SOURCE_BUY_USD", 0.0) or 0.0)
    base_ratio = max(0.0, float(getattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO", 0.0) or 0.0))
    unknown_mcap_floor = max(0.0, float(
        getattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_UNKNOWN_MCAP_USD", 0.0) or 0.0))
    band_ratios = getattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO_BY_BAND", None) or {}
    source_ratio = base_ratio
    if source_mcap is not None:
        source_ratio = rr._band_source_buy_ratio(band_ratios, network_id, source_mcap, base_ratio)
    source_ratio = rr._wallet_source_buy_ratio(network_id, source_user_id, source_ratio,
        by_wallet=getattr(cfg, 'MEMECOIN_SOLANA_MIN_SOURCE_BUY_MCAP_RATIO_BY_WALLET', {}) or {})
    family_reason, family_info = rr.source_buy_family_reason(
        their_usd=source_size_usd, source_mcap=source_mcap, prior_net_usd=prior_net_usd,
        ratio=source_ratio, unknown_mcap_floor=unknown_mcap_floor, abs_min=abs_min,
        abs_max=abs_max,
        family_active=bool(base_ratio > 0 or unknown_mcap_floor > 0 or band_ratios))
    inputs["source_buy_abs_min_usd"], inputs["source_buy_abs_max_usd"] = abs_min, abs_max
    inputs["source_buy_mcap_usd"] = source_mcap
    inputs["source_buy_ratio"] = source_ratio
    inputs["source_buy_floor_usd"] = family_info["floor_usd"]
    inputs["source_buy_effective_floor_usd"] = family_info["effective_floor_usd"]
    inputs["source_buy_floor_tolerance_pct"] = family_info["floor_tolerance_pct"]
    inputs["source_buy_ratio_pct"] = family_info["ratio_pct"]
    inputs["source_buy_judged_usd"] = family_info["judged_usd"]
    inputs["source_buy_prior_net_usd"] = prior_net_usd
    states["source_buy_floor"] = FAIL if family_reason else PASS
    if family_reason:
        refused = refused or ("source_buy_floor", family_reason)

    # 3. The volume family, through the funded path's own argument builder and decision, so a
    #    threshold change reaches this lane without an edit here.
    stats = {"volume_h1": vol_h1, "volume_m5": vol_m5,
             "txns_m5_buys": _f(market.get("txns_m5_buys")), "txns_m5_sells": _f(market.get("txns_m5_sells"))}
    try:
        active, boundary, ceil_m5, kwargs = rr.volume_gate_args(
            stats, mcap=effective_mc, network_id=network_id, coin_band=coin_band,
            source_user_id=source_user_id, signal_usd=source_size_usd, source_age_s=None)
        alive, need, why = rr.volume_alive_decision(vol_h1, effective_mc, **kwargs)
    except Exception:
        logger.exception("promotion lane: volume gate raised; recorded unknown")
        alive, need, why = True, None, ""
        states["m5_turnover_under_floor"] = UNKNOWN
    else:
        inputs["volume_need_usd"] = need
        family = ("m5_turnover_over_ceiling" if str(why).endswith("over_ceiling")
                  else "m5_txns_under_floor" if "txns" in str(why)
                  else "m5_turnover_under_floor")
        for name in ("m5_turnover_under_floor", "m5_turnover_over_ceiling", "m5_txns_under_floor"):
            states[name] = PASS
        # Same per-chain switch as the funded path (MEMECOIN_REAL_VOLUME_GATES_OFF_CHAINS), so the
        # shadow does not refuse what the funded path now admits on those chains.
        if not alive and str(network_id) in getattr(cfg, "MEMECOIN_REAL_VOLUME_GATES_OFF_CHAINS",
                                                    frozenset()):
            inputs["volume_gate_off_chain_would_refuse"] = why
            alive = True
        if not alive:
            states[family] = FAIL
            refused = refused or (family, why)

    # 4. The reentry families: pure reads of our own ledger AS OF the decision stamp. A position
    #    that closed AFTER the decision cannot inform it, which is the whole point of the cutoff.
    reentry = ledger or {}
    for name, key in (("reentry_cooldown", "cooldown_blocked"),
                      ("losing_reentry", "losing_blocked"),
                      ("winner_reentry_daily_cap", "winner_cap_blocked")):
        value = reentry.get(key)
        if value is None:
            states[name] = UNKNOWN
            continue
        states[name] = FAIL if value else PASS
        if value:
            refused = refused or (name, reentry.get(key + "_reason") or name)
    inputs["reentry"] = {k: v for k, v in reentry.items() if not k.endswith("_reason")}

    return states, inputs, refused


def verdict_of(states, refused):
    """`admitted` only when nothing reproducible failed. A lagged or unknown family never admits on
    its own merit and never refuses either -- it is simply not part of the decision."""
    if refused:
        return "refused"
    if any(states.get(name) == FAIL for name in REPRODUCIBLE):
        return "refused"
    return "admitted"


def lagged_chase(*, source_price, quote_price, cfg):
    """The chase families, computed from the EVALUATION-time quote against the stored source price.

    Evidence only. A quote taken a minute after the decision measures the minute, not the decision,
    so this is recorded and never allowed to decide. Returns a dict, never raises.
    """
    src, quote = _f(source_price, positive=True), _f(quote_price, positive=True)
    if src is None or quote is None:
        return {"state": LAGGED_STATE, "reason": "no price pair"}
    gap_pct = (quote / src - 1.0) * 100.0
    cap = _f(getattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", None))
    return {"state": LAGGED_STATE, "lagged_gap_pct": gap_pct, "base_cap_pct": cap,
            "would_have_failed_if_current": None if cap is None else bool(gap_pct > cap),
            "note": "measured after the decision; never decisive"}


def dumps(value):
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return "{}"
