"""Capture + reconcile the economic truth of a window: which Polymarket token won.

Primary truth = the final winning token/outcome (from the ``market_resolved`` WS event and/or Gamma's
resolved fields). We do NOT require the exact Chainlink end value for Phase 0; we DO carry Gamma's
``priceToBeat`` along as the post-hoc oracle for boundary-rule calibration.
"""
from __future__ import annotations

from typing import Optional

from .models import MarketSpec, Settlement


def winner_from_outcome_prices(outcome_prices, up_token_id, down_token_id):
    """``["1","0"]`` -> Up wins, ``["0","1"]`` -> Down wins; anything else -> ``(None, None)``."""
    try:
        op = [str(x) for x in (outcome_prices or [])]
    except TypeError:
        return None, None
    if op == ["1", "0"]:
        return up_token_id, "Up"
    if op == ["0", "1"]:
        return down_token_id, "Down"
    return None, None


def _market_for_spec(spec: MarketSpec) -> dict:
    """Pick the market in ``spec.raw_payload`` that matches the spec's identity. A Gamma event can carry
    several markets; the BTC market was selected deterministically at parse time, so we must re-find THAT
    market by ``conditionId`` — never blindly take ``markets[0]`` (would read the wrong winner)."""
    markets = spec.raw_payload.get("markets") or []
    if spec.condition_id:
        for m in markets:
            if str(m.get("conditionId") or "") == str(spec.condition_id):
                return m
        return {}                       # id known but not present -> do NOT guess a market
    return markets[0] if len(markets) == 1 else {}


def from_gamma(spec: MarketSpec) -> Settlement:
    """Build a settlement from a (resolved) Gamma ``MarketSpec``."""
    mk = _market_for_spec(spec)
    op = mk.get("outcomePrices")
    import json
    if isinstance(op, str):
        try:
            op = json.loads(op)
        except (ValueError, TypeError):
            op = None
    win_token, win_outcome = winner_from_outcome_prices(op, spec.up_token_id, spec.down_token_id)
    uma = mk.get("umaResolutionStatus")
    resolution_ts = mk.get("closedTime") or spec.raw_payload.get("closedTime")
    status = "gamma_only" if win_outcome else "unresolved"
    return Settlement(
        condition_id=spec.condition_id,
        winning_token_id=win_token,
        winning_outcome=win_outcome,
        resolution_ts=resolution_ts,
        uma_resolution_status=uma,
        outcome_prices=tuple(str(x) for x in (op or ())),
        source_used="gamma",
        reconciliation_status=status,
        gamma_price_to_beat=spec.price_to_beat,
    )


# The WS `market_resolved` schema is reconstructed from docs + a synthetic fixture; flip to True only
# after a REAL captured BTC 5m event is verified. Reported via ws_parser_status() so downstream surfaces
# can label settlement WS parsing honestly.
WS_PARSER_VERIFIED = False


def ws_parser_status() -> str:
    return "verified" if WS_PARSER_VERIFIED else "provisional"


def from_ws_resolved(msg: dict, spec: MarketSpec) -> Optional[Settlement]:
    """Parse a ``market_resolved`` WS event. Schema is tolerant/provisional (no recorded fixture yet):
    looks for a winning asset/token id or a resolved outcome in common field names. Hardened: a settlement
    whose condition/market identity is PRESENT but does not match ``spec`` is rejected (don't silently
    accept a wrong-market event); field order and unknown fields are tolerated."""
    if not isinstance(msg, dict):
        return None
    # identity guard — reject a settlement for a DIFFERENT market when the id is present and mismatched
    cid = msg.get("condition_id") or msg.get("conditionId") or msg.get("market")
    if cid and spec.condition_id and str(cid) != str(spec.condition_id):
        return None
    mid = msg.get("market_id") or msg.get("marketId")
    if mid and spec.market_id and str(mid) != str(spec.market_id):
        return None
    win_token = (msg.get("winning_token_id") or msg.get("winning_asset_id")
                 or msg.get("winningAsset") or msg.get("asset_id"))
    win_outcome = None
    if win_token == spec.up_token_id:
        win_outcome = "Up"
    elif win_token == spec.down_token_id:
        win_outcome = "Down"
    else:
        # documented `winning_outcome` field as a fallback when only the outcome (not the token) is given
        wo = str(msg.get("winning_outcome") or "").strip().lower()
        if wo == "up":
            win_outcome, win_token = "Up", spec.up_token_id
        elif wo == "down":
            win_outcome, win_token = "Down", spec.down_token_id
    if win_outcome is None:
        return None
    return Settlement(
        condition_id=spec.condition_id,
        winning_token_id=win_token,
        winning_outcome=win_outcome,
        resolution_ts=str(msg.get("timestamp")) if msg.get("timestamp") is not None else None,
        uma_resolution_status=msg.get("status") or "resolved",
        outcome_prices=("1", "0") if win_outcome == "Up" else ("0", "1"),
        source_used="websocket",
        reconciliation_status="ws_only",
        gamma_price_to_beat=None,
    )


def reconcile(gamma: Optional[Settlement], ws: Optional[Settlement]) -> Settlement:
    """Merge gamma + ws settlements into one with a ``reconciliation_status``."""
    if gamma and ws:
        agree = gamma.winning_outcome == ws.winning_outcome and gamma.winning_outcome is not None
        if agree:
            return Settlement(gamma.condition_id, gamma.winning_token_id, gamma.winning_outcome,
                              gamma.resolution_ts or ws.resolution_ts, gamma.uma_resolution_status,
                              gamma.outcome_prices, "gamma+websocket", "agree",
                              gamma.gamma_price_to_beat)
        # conflict: do NOT emit a winner a downstream evaluator could consume — quarantine it.
        return Settlement(gamma.condition_id, None, None,
                          gamma.resolution_ts or ws.resolution_ts, gamma.uma_resolution_status,
                          (), "gamma+websocket", "conflict", gamma.gamma_price_to_beat)
    if gamma:
        return Settlement(gamma.condition_id, gamma.winning_token_id, gamma.winning_outcome,
                          gamma.resolution_ts, gamma.uma_resolution_status, gamma.outcome_prices,
                          "gamma", "gamma_only" if gamma.winning_outcome else "unresolved",
                          gamma.gamma_price_to_beat)
    if ws:
        return Settlement(ws.condition_id, ws.winning_token_id, ws.winning_outcome,
                          ws.resolution_ts, ws.uma_resolution_status, ws.outcome_prices,
                          "websocket", "ws_only", None)
    return Settlement("", None, None, None, None, (), "none", "unresolved", None)
