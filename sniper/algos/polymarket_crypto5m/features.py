"""Decision-time feature snapshots (pure; known-at-time).

Every feature is computed only from events with ``source_ts <= feature_cutoff_ts`` AND, where the
value had to arrive, ``recv_ts <= decision_ts`` — you may not use a value before the process could
have known it. The post-resolution Gamma ``priceToBeat`` is NEVER an input; the decision-time
Price-to-Beat is reconstructed from the captured Chainlink ticks and carries quality ``unavailable``
until the boundary rule locks (so threshold-diffusion etc. run *diagnostically* in Phase 1A).

Inputs are plain dicts so this is unit-testable without a DB; the replay layer does the DB reads +
unit conversion (full_accuracy_value/value -> price). Realized volatility is the stdev of per-second
log returns (the Chainlink stream is ~1s-spaced) — i.e. already per-second.
"""
from __future__ import annotations

import math
from typing import Optional

from . import basis_features as DF
from . import binance_features as BFE
from . import executable_features as EXEC
from .price_to_beat_resolver import CANDIDATE_RULES

FEATURE_VERSION = "fv1"   # fv1 (2026-06-21): adds family-G flow features (depth-flow OFI + CLOB repricing)
CHECKPOINTS = (15, 30, 60, 90, 120, 150, 180, 210, 240, 255, 270, 285)
FLOW_LOOKBACK_S = 60      # window for the short-horizon flow features (vs a prior book ~60s before decision)
DIAGNOSTIC_BOUNDARY_RULE = "last_at_or_before"   # used (quality=unavailable) until the rule locks
MAX_CHAINLINK_AGE_MS = 120_000   # a chainlink reference >2min stale within a 5min window is not decision-grade
_EPS = 1e-12


def _known_ticks(ticks, *, cutoff_ts, decision_ts):
    """Filter to ticks known at decision time: source_ts <= cutoff AND received <= decision.

    The source comparison is in milliseconds (NOT a second-floor) so a tick sourced sub-second AFTER
    the cutoff can never leak in. A tick with ``recv_ts is None`` (unknown arrival — e.g. an imported /
    backfilled tick) can NOT be proven to have arrived before the decision, so it is excluded (fail
    closed); the caller flags such drops via ``receive_time_missing``."""
    cutoff_ms = cutoff_ts * 1000
    out = []
    for t in ticks or []:
        recv = t.get("recv_ts")
        if int(t["source_ts_ms"]) <= cutoff_ms and recv is not None and recv <= decision_ts:
            out.append(t)
    out.sort(key=lambda t: t["source_ts_ms"])
    return out


def _select_p2b_tick(ticks, *, start_ms, rule):
    """Reconstruct the Price-to-Beat candidate tick by the boundary RULE (over known ticks)."""
    if not ticks:
        return None
    if rule == "last_at_or_before":
        before = [t for t in ticks if t["source_ts_ms"] <= start_ms]
        return max(before, key=lambda t: t["source_ts_ms"]) if before else None
    if rule == "first_at_or_after":
        after = [t for t in ticks if t["source_ts_ms"] >= start_ms]
        return min(after, key=lambda t: t["source_ts_ms"]) if after else None
    # nearest
    return min(ticks, key=lambda t: (abs(t["source_ts_ms"] - start_ms), t["source_ts_ms"]))


def _return_over(ticks, *, now_ms, horizon_s):
    """Log return of price now vs the last tick >= horizon_s ago. None if not enough history."""
    if not ticks:
        return None
    cut = now_ms - horizon_s * 1000
    prior = [t for t in ticks if t["source_ts_ms"] <= cut]
    if not prior:
        return None
    p0, p1 = float(prior[-1]["price"]), float(ticks[-1]["price"])
    if p0 <= 0 or p1 <= 0:
        return None
    return math.log(p1 / p0)


def _realized_vol_per_s(ticks):
    """Stdev of consecutive per-second log returns. None if < 3 ticks."""
    if len(ticks) < 3:
        return None
    rets = []
    for a, b in zip(ticks, ticks[1:]):
        pa, pb = float(a["price"]), float(b["price"])
        if pa > 0 and pb > 0:
            rets.append(math.log(pb / pa))
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var)


def _mid(book) -> Optional[float]:
    if not book:
        return None
    bb, ba = book.get("best_bid"), book.get("best_ask")
    if bb is None or ba is None:
        return None
    return (float(bb) + float(ba)) / 2.0


def _side_depth(levels, n=5) -> float:
    """Total size in the top-n levels of one book side. ``levels`` = ``[[price, size], ...]``."""
    out = 0.0
    for lvl in (levels or [])[:n]:
        try:
            out += float(lvl[1])
        except (TypeError, ValueError, IndexError):
            continue
    return out


def _depth_flow(prior_book, now_book, n=5) -> Optional[float]:
    """Top-n depth-flow imbalance (simplified multi-level OFI) between two snapshots of ONE token book.
    +ve = net buy pressure on this token (bids accumulated and/or asks thinned) over the lookback. ``None``
    if either book is missing. Known-at-decision: both snapshots are sourced at/before the decision."""
    if not prior_book or not now_book:
        return None
    d_bid = _side_depth(now_book.get("bids"), n) - _side_depth(prior_book.get("bids"), n)
    d_ask = _side_depth(now_book.get("asks"), n) - _side_depth(prior_book.get("asks"), n)
    return round(d_bid - d_ask, 4)


def _midpoint_change(prior_book, now_book) -> Optional[float]:
    """Change in this token's midpoint over the lookback (the CLOB's repricing). Pairs with the existing
    chainlink_return_* path so a model can learn the oracle-gap / lead-lag (BTC moved, did the book?)."""
    pm, nm = _mid(prior_book), _mid(now_book)
    return round(nm - pm, 6) if (pm is not None and nm is not None) else None


def build_feature_snapshot(*, condition_id, market_id, window_start_ts, window_end_ts, checkpoint,
                           chainlink_ticks, up_book, down_book, code_commit="",
                           market_rule_hash="", boundary_rule=None, binance_obs=None, fee=None,
                           binance_symbol=None, up_book_prior=None, down_book_prior=None) -> dict:
    """Build a decision-time snapshot for ``checkpoint`` seconds after window start. Pure."""
    decision_ts = int(window_start_ts) + int(checkpoint)
    cutoff_ts = decision_ts
    seconds_remaining = int(window_end_ts) - decision_ts
    fraction_elapsed = checkpoint / max(window_end_ts - window_start_ts, 1)

    ticks = _known_ticks(chainlink_ticks, cutoff_ts=cutoff_ts, decision_ts=decision_ts)
    # Known-at-time audit: any tick sourced before the cutoff but with an unknown arrival time was
    # dropped above (it can't be proven known-at-decision). Flag it so the dataset exporter can exclude
    # the row from model-eligibility — the leak is already prevented, this records that it happened.
    cutoff_ms = cutoff_ts * 1000
    receive_time_missing = any(int(t.get("source_ts_ms", 0)) <= cutoff_ms and t.get("recv_ts") is None
                               for t in (chainlink_ticks or []))
    now_ms = ticks[-1]["source_ts_ms"] if ticks else None
    chainlink_now = float(ticks[-1]["price"]) if ticks else None
    chainlink_now_age_ms = (decision_ts * 1000 - now_ms) if now_ms is not None else None
    vol_per_s = _realized_vol_per_s(ticks)

    # decision-time Price-to-Beat by the boundary RULE. Quality is only authoritative when a VALID
    # locked rule is supplied; a bogus/None rule uses a diagnostic candidate with quality 'unavailable'
    # (so a bogus rule can NEVER promote the activation gate — the fail-closed guard the resolver owns).
    rule = boundary_rule if boundary_rule in CANDIDATE_RULES else DIAGNOSTIC_BOUNDARY_RULE
    p2b_tick = _select_p2b_tick(ticks, start_ms=window_start_ts * 1000, rule=rule)
    price_to_beat = float(p2b_tick["price"]) if p2b_tick else None
    p2b_known_ts = p2b_tick.get("recv_ts") if p2b_tick else None
    p2b_quality = ("validated_rtds_reconstruction" if boundary_rule in CANDIDATE_RULES
                   else "unavailable")

    # geometry (family A)
    distance = (chainlink_now - price_to_beat) if (chainlink_now and price_to_beat) else None
    norm_dist = None
    if chainlink_now and price_to_beat and vol_per_s and seconds_remaining > 0:
        denom = max(vol_per_s * math.sqrt(seconds_remaining), _EPS)
        norm_dist = math.log(chainlink_now / price_to_beat) / denom

    up_mid, down_mid = _mid(up_book), _mid(down_book)

    feats = {
        "feature_version": FEATURE_VERSION,
        # A — settlement geometry
        "chainlink_price_now": chainlink_now, "price_to_beat": price_to_beat,
        "distance_to_beat": distance,
        "absolute_distance_to_beat": abs(distance) if distance is not None else None,
        "seconds_remaining": seconds_remaining, "fraction_of_window_elapsed": fraction_elapsed,
        "normalized_distance_to_beat": norm_dist, "vol_per_s": vol_per_s,
        "chainlink_now_age_ms": chainlink_now_age_ms,
        # B — Chainlink short-horizon path
        "chainlink_return_15s": _return_over(ticks, now_ms=now_ms, horizon_s=15) if now_ms else None,
        "chainlink_return_30s": _return_over(ticks, now_ms=now_ms, horizon_s=30) if now_ms else None,
        "chainlink_return_60s": _return_over(ticks, now_ms=now_ms, horizon_s=60) if now_ms else None,
        "chainlink_intrawindow_high": max((float(t["price"]) for t in ticks
                                           if t["source_ts_ms"] >= window_start_ts * 1000), default=None),
        "chainlink_intrawindow_low": min((float(t["price"]) for t in ticks
                                          if t["source_ts_ms"] >= window_start_ts * 1000), default=None),
        # E — Polymarket book (each token independent)
        "up_best_bid": (up_book or {}).get("best_bid"), "up_best_ask": (up_book or {}).get("best_ask"),
        "down_best_bid": (down_book or {}).get("best_bid"), "down_best_ask": (down_book or {}).get("best_ask"),
        "up_spread": _spread(up_book), "down_spread": _spread(down_book),
        # F — market-implied baseline (the benchmark)
        "up_midpoint_probability": up_mid, "down_midpoint_probability": down_mid,
        # G — short-horizon flow vs a prior book ~FLOW_LOOKBACK_S before the decision (known-at-decision):
        # depth-flow OFI (research's T-60s direction signal) + the CLOB's own repricing. None until a prior
        # book exists. With chainlink_return_* (family B) the model can learn the oracle-gap / lead-lag.
        "up_depth_flow_60s": _depth_flow(up_book_prior, up_book),
        "down_depth_flow_60s": _depth_flow(down_book_prior, down_book),
        "up_midpoint_change_60s": _midpoint_change(up_book_prior, up_book),
        "down_midpoint_change_60s": _midpoint_change(down_book_prior, down_book),
    }
    # C — Binance-reframed predictor features (known-at-decision; volume/VWAP only when a volume source
    # exists; never settlement truth / never the label). Re-filters its own inputs for known-at-time.
    feats.update(BFE.build_binance_features(binance_obs or [], chainlink_ticks,
                                            cutoff_ts=cutoff_ts, decision_ts=decision_ts,
                                            expected_symbol=binance_symbol))
    # D — Binance↔Chainlink basis (fail-closed on source-time misalignment; no silent interpolation).
    feats.update(DF.build_basis_features(binance_obs or [], chainlink_ticks,
                                         cutoff_ts=cutoff_ts, decision_ts=decision_ts,
                                         expected_symbol=binance_symbol))
    # executable-price observations (Up/Down × 5/10/20 USDC); fee-inclusive null unless fee is known.
    _fee = fee or {}
    feats.update(EXEC.build_executable_features(up_book, down_book, fee_rate=_fee.get("fee_rate"),
                                                fee_known=(_fee.get("fee_status") == "known")))

    if chainlink_now is None or price_to_beat is None:
        feature_status = "boundary_unavailable_at_decision"
    elif seconds_remaining <= 0:
        feature_status = "too_close_to_close"
    elif up_book is None or down_book is None or up_mid is None or down_mid is None:
        # a present-but-unpriced book (empty / null best_ask) is NOT a usable decision book
        feature_status = "feature_window_incomplete"
    elif chainlink_now_age_ms is not None and chainlink_now_age_ms > MAX_CHAINLINK_AGE_MS:
        # the chainlink reference is too stale to be decision-grade (likely a feed gap, not a flat price)
        feature_status = "stale_reference"
    else:
        feature_status = "ok"

    return {
        "condition_id": condition_id, "market_id": market_id,
        "window_start_ts": window_start_ts, "window_end_ts": window_end_ts,
        "checkpoint": checkpoint, "decision_ts": decision_ts, "feature_cutoff_ts": cutoff_ts,
        "feature_version": FEATURE_VERSION, "feature_status": feature_status,
        "price_to_beat": price_to_beat, "price_to_beat_quality": p2b_quality,
        "price_to_beat_known_ts": p2b_known_ts,
        # per-side book integrity (never cross-contaminate); overall = worst of the two
        "up_book_integrity_status": (up_book or {}).get("book_integrity_status", "usable"),
        "down_book_integrity_status": (down_book or {}).get("book_integrity_status", "usable"),
        "book_integrity_status": _worst_integrity(up_book, down_book),
        "market_rule_hash": market_rule_hash, "code_commit": code_commit,
        "receive_time_missing": receive_time_missing,
        "features": feats,
    }


_INTEGRITY_RANK = {"usable": 0, "stale": 1, "quarantined": 2}


def _worst_integrity(up_book, down_book) -> str:
    a = (up_book or {}).get("book_integrity_status", "usable")
    b = (down_book or {}).get("book_integrity_status", "usable")
    return a if _INTEGRITY_RANK.get(a, 0) >= _INTEGRITY_RANK.get(b, 0) else b


def _spread(book):
    if not book or book.get("best_bid") is None or book.get("best_ask") is None:
        return None
    return float(book["best_ask"]) - float(book["best_bid"])
