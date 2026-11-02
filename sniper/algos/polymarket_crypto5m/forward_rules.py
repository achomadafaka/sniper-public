"""Phase 1D — frozen forward-paper rules (immutable after launch).

Pure functions: a known-at-time decision context -> ``(side, reason)`` where side ∈ {"Up","Down",None}.
Controls C0/C1; candidates R1/R2/R3. NO Price-to-Beat / distance / threshold rule (deferred). Binance is a
predictor only. Any change to a rule, threshold, or config is a NEW campaign version, not an edit — the
manifest hash enforces this: if RULE_MANIFEST changes, manifest_hash() changes and EXPECTED_MANIFEST_HASH
must be updated deliberately (which is the act of declaring a new frozen version).
"""
from __future__ import annotations

import hashlib
import json
import math

CAMPAIGN_ID = "btc5m-forward-paper-v0"
RULE_VERSION = "v0"

CONFIG = {
    "primary_checkpoint_s": 240,
    "diagnostic_checkpoint_s": 270,
    "latency_ms": 500,
    "gross_notional_usdc": 10.0,
    "bankroll_usdc": 400.0,
    "documented_fee_rate": 0.07,
    "imbalance_threshold": 0.20,
    "momentum_lookback_s": 30,
    "maker_fills": False,
    "compounding": False,
    "settlement_rule": "up_when_chainlink_end_ge_start",
}

RULES = {
    "C0": {"kind": "control", "logic": "deterministic_random_side", "seed": "sha256(campaign_id|condition_id)"},
    "C1": {"kind": "control", "logic": "favorite_by_independent_midpoint"},
    "R1": {"kind": "candidate", "logic": "binance_30s_log_momentum", "lookback_s": 30, "threshold": 0.0},
    "R2": {"kind": "candidate", "logic": "book_imbalance_near_top", "threshold": 0.20},
    "R3": {"kind": "candidate", "logic": "r1_and_r2_agreement"},
}
RULE_IDS = ("C0", "C1", "R1", "R2", "R3")

RULE_MANIFEST = {"campaign_id": CAMPAIGN_ID, "rule_version": RULE_VERSION, "config": CONFIG, "rules": RULES}


def manifest_hash() -> str:
    return hashlib.sha256(json.dumps(RULE_MANIFEST, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# Frozen at launch. Changing the manifest changes this; updating it = declaring a new frozen version.
EXPECTED_MANIFEST_HASH = "4b33067a7ee94c511ac5767f485d9d85280025a50b8a24730a1b376ee538a41e"


def _imbalance(bid_notional, ask_notional):
    if bid_notional is None or ask_notional is None:
        return None
    s = float(bid_notional) + float(ask_notional)
    return ((float(bid_notional) - float(ask_notional)) / s) if s > 0 else None


def _c0(ctx):
    cid = ctx.get("condition_id")
    if cid is None:
        return None, "condition_id_missing"
    h = hashlib.sha256(f"{CAMPAIGN_ID}|{cid}".encode()).hexdigest()
    return ("Up" if int(h, 16) % 2 == 0 else "Down"), "deterministic_random"


def _c1(ctx):
    up, dn = ctx.get("up_midpoint"), ctx.get("down_midpoint")
    if up is None or dn is None:
        return None, "midpoint_unavailable"
    if up > dn:
        return "Up", "favorite_up"
    if dn > up:
        return "Down", "favorite_down"
    return None, "midpoint_tie"


def _r1(ctx):
    now, ago = ctx.get("binance_price_now"), ctx.get("binance_price_30s_ago")
    try:
        now, ago = float(now), float(ago)
    except (TypeError, ValueError):
        return None, "binance_unavailable"
    if now <= 0 or ago <= 0:
        return None, "binance_unavailable"
    r30 = math.log(now / ago)
    if r30 > 0:
        return "Up", "momentum_up"
    if r30 < 0:
        return "Down", "momentum_down"
    return None, "no_momentum"


def _r2(ctx):
    up_imb = _imbalance(ctx.get("up_bid_notional"), ctx.get("up_ask_notional"))
    dn_imb = _imbalance(ctx.get("down_bid_notional"), ctx.get("down_ask_notional"))
    if up_imb is None or dn_imb is None:
        return None, "book_unavailable"
    delta = up_imb - dn_imb
    if delta >= CONFIG["imbalance_threshold"]:
        return "Up", "imbalance_up"
    if delta <= -CONFIG["imbalance_threshold"]:
        return "Down", "imbalance_down"
    return None, "imbalance_below_threshold"


def _r3(ctx):
    s1, _ = _r1(ctx)
    s2, _ = _r2(ctx)
    if s1 is not None and s1 == s2:
        return s1, "agreement"
    return None, "no_agreement"


_RULES = {"C0": _c0, "C1": _c1, "R1": _r1, "R2": _r2, "R3": _r3}


def evaluate(rule_id, ctx) -> tuple:
    """Apply a frozen rule to a decision context. Returns (side, reason); side ∈ {"Up","Down",None}."""
    fn = _RULES.get(rule_id)
    if fn is None:
        raise KeyError(f"unknown rule_id {rule_id!r}")
    return fn(ctx or {})
