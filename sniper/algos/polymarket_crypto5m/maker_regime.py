"""Pure regime router for the crypto5m paper maker.

This module does not quote, fill, persist, fetch, settle, or send orders. It only classifies the
known-at-time state and returns conservative overrides for the existing maker policy.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


POLICY_ID = "maker-regime-v1"


@dataclass(frozen=True)
class MakerRegimeConfig:
    late_window_s: float = 30.0
    saturation_frac: float = 0.98
    moderate_extreme_lo: float = 0.20
    moderate_extreme_hi: float = 0.80
    severe_extreme_lo: float = 0.05
    severe_extreme_hi: float = 0.95
    mid_chop_lo: float = 0.40
    mid_chop_hi: float = 0.60
    jump_divergence: float = 2.0
    jump_vol_ratio: float = 1.8
    high_confidence: float = 0.75
    medium_confidence: float = 0.60
    base_min_edge: float = 0.04
    base_min_seconds_quote: float = 30.0
    base_quote_ttl_ms: int = 12_000


@dataclass(frozen=True)
class MakerRegimeSnapshot:
    fair: Optional[float]
    best_bid: Optional[float]
    best_ask: Optional[float]
    seconds_left: Optional[float]
    jump_flag: bool = False
    divergence: Optional[float] = None
    vol_fast: Optional[float] = None
    vol_slow: Optional[float] = None
    confirmed_side: Optional[str] = None
    direction_confidence: float = 0.0
    book_stale: bool = False
    book_quarantined: bool = False
    fee_conflict: bool = False

    def spread(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid


@dataclass(frozen=True)
class MakerRegimeInventory:
    token_inventory: float
    opposite_inventory: float
    max_shares: float

    def exposure(self) -> dict:
        token = max(0.0, float(self.token_inventory or 0.0))
        opposite = max(0.0, float(self.opposite_inventory or 0.0))
        return {
            "gross_inventory": round(token + opposite, 8),
            "net_directional": round(token - opposite, 8),
            "complete_sets": round(min(token, opposite), 8),
        }

    def token_saturation(self) -> float:
        cap = max(1.0, float(self.max_shares or 0.0))
        return max(0.0, float(self.token_inventory or 0.0)) / cap


@dataclass(frozen=True)
class MakerRegimePolicy:
    regime_id: str
    policy_id: str = POLICY_ID
    regime_confidence: str = "medium"
    margin_multiplier: float = 1.0
    margin_add: float = 0.0
    clip_multiplier: float = 1.0
    max_shares_multiplier: float = 1.0
    min_edge: float = 0.04
    min_seconds_quote: float = 30.0
    bid_enabled: bool = True
    ask_enabled: bool = True
    exit_only: bool = False
    risk_action: str = "normal"
    can_hard_cut: bool = True
    quote_ttl_ms: int = 12_000
    reason: str = ""
    warnings: tuple[str, ...] = field(default_factory=tuple)


def route_maker_regime(snapshot: MakerRegimeSnapshot, inventory: MakerRegimeInventory,
                       config: MakerRegimeConfig = MakerRegimeConfig()) -> MakerRegimePolicy:
    warnings = tuple(_invalid_reasons(snapshot))
    if warnings:
        return _policy(
            config, "hard_invalid", confidence="low", margin_multiplier=2.0,
            clip_multiplier=0.0, max_shares_multiplier=0.0, bid_enabled=False, ask_enabled=False,
            exit_only=False, risk_action="pull_all", can_hard_cut=False, quote_ttl_ms=0,
            reason="; ".join(warnings), warnings=warnings,
        )

    token_inv = max(0.0, float(inventory.token_inventory or 0.0))

    if snapshot.seconds_left is not None and snapshot.seconds_left <= config.late_window_s:
        return _policy(
            config, "late_window", confidence="high", margin_multiplier=2.5,
            clip_multiplier=0.25, max_shares_multiplier=0.35, bid_enabled=False,
            ask_enabled=token_inv > 0.0, exit_only=token_inv > 0.0,
            risk_action=("reduce_only" if token_inv > 0.0 else "pull_all"),
            can_hard_cut=token_inv > 0.0, quote_ttl_ms=3_000,
            reason=f"{snapshot.seconds_left:.0f}s left; no new inventory near settlement",
        )

    if inventory.token_saturation() >= config.saturation_frac:
        return _policy(
            config, "inventory_saturated", confidence="high", margin_multiplier=1.8,
            clip_multiplier=0.25, max_shares_multiplier=0.0, bid_enabled=False,
            ask_enabled=token_inv > 0.0, exit_only=True, risk_action="reduce_only",
            can_hard_cut=token_inv > 0.0, quote_ttl_ms=5_000,
            reason=f"token inventory {token_inv:.2f} is at cap {inventory.max_shares:.2f}",
        )

    if _is_extreme(snapshot, config):
        severe = _is_severe_extreme(snapshot, config)
        can_reduce = token_inv > 0.0
        return _policy(
            config, "extreme_odds", confidence=("high" if severe else "medium"),
            margin_multiplier=(3.0 if severe else 2.0), clip_multiplier=(0.0 if severe and not can_reduce else 0.20),
            max_shares_multiplier=(0.0 if severe else 0.25), bid_enabled=False,
            ask_enabled=can_reduce, exit_only=can_reduce,
            risk_action=("reduce_only" if can_reduce else "pull_all"), can_hard_cut=can_reduce,
            quote_ttl_ms=(3_000 if severe else 5_000),
            reason=f"fair {snapshot.fair:.2f} is in {'severe' if severe else 'moderate'} extreme odds band",
        )

    if _is_jump(snapshot, config):
        return _policy(
            config, "jump", confidence="medium", margin_multiplier=2.0,
            clip_multiplier=0.35, max_shares_multiplier=0.50, bid_enabled=True,
            ask_enabled=True, exit_only=False, risk_action="widen", can_hard_cut=True,
            quote_ttl_ms=4_000, reason="jump/divergence state; quote wider and smaller",
        )

    if snapshot.confirmed_side in ("Up", "Down"):
        conf = _confidence(snapshot.direction_confidence, config)
        return _policy(
            config, "trend_confirmed", confidence=conf,
            margin_multiplier=(1.7 if conf == "low" else 1.35),
            clip_multiplier=(0.40 if conf == "low" else 0.70),
            max_shares_multiplier=(0.60 if conf == "low" else 0.85),
            bid_enabled=True, ask_enabled=True, exit_only=False, risk_action="widen",
            can_hard_cut=True, quote_ttl_ms=(5_000 if conf == "low" else 7_000),
            reason=f"confirmed {snapshot.confirmed_side} read; maker posture only",
        )

    if config.mid_chop_lo <= float(snapshot.fair) <= config.mid_chop_hi:
        can_reduce = token_inv > 0.0
        return _policy(
            config, "mid_chop", confidence="medium", margin_multiplier=2.0,
            clip_multiplier=0.20, max_shares_multiplier=0.0, min_edge=0.06,
            bid_enabled=False, ask_enabled=can_reduce, exit_only=can_reduce,
            risk_action=("reduce_only" if can_reduce else "pull_all"),
            can_hard_cut=can_reduce, quote_ttl_ms=4_000,
            reason="mid-chop live bleed; no new inventory, reduce only when long",
        )

    return _policy(config, "normal", confidence="medium", reason="moderate state; base maker policy")


def _policy(config: MakerRegimeConfig, regime_id: str, *, confidence: str = "medium",
            margin_multiplier: float = 1.0, margin_add: float = 0.0, clip_multiplier: float = 1.0,
            max_shares_multiplier: float = 1.0, min_edge: Optional[float] = None,
            min_seconds_quote: Optional[float] = None, bid_enabled: bool = True, ask_enabled: bool = True,
            exit_only: bool = False, risk_action: str = "normal", can_hard_cut: bool = True,
            quote_ttl_ms: Optional[int] = None, reason: str = "",
            warnings: tuple[str, ...] = ()) -> MakerRegimePolicy:
    return MakerRegimePolicy(
        regime_id=regime_id,
        regime_confidence=confidence,
        margin_multiplier=margin_multiplier,
        margin_add=margin_add,
        clip_multiplier=clip_multiplier,
        max_shares_multiplier=max_shares_multiplier,
        min_edge=config.base_min_edge if min_edge is None else min_edge,
        min_seconds_quote=config.base_min_seconds_quote if min_seconds_quote is None else min_seconds_quote,
        bid_enabled=bid_enabled,
        ask_enabled=ask_enabled,
        exit_only=exit_only,
        risk_action=risk_action,
        can_hard_cut=can_hard_cut,
        quote_ttl_ms=config.base_quote_ttl_ms if quote_ttl_ms is None else quote_ttl_ms,
        reason=reason,
        warnings=warnings,
    )


def _invalid_reasons(snapshot: MakerRegimeSnapshot) -> list[str]:
    reasons = []
    if snapshot.fair is None or not (0.0 < snapshot.fair < 1.0):
        reasons.append("no valid fair")
    if snapshot.best_bid is None or snapshot.best_ask is None:
        reasons.append("missing book")
    elif snapshot.best_bid >= snapshot.best_ask:
        reasons.append("crossed/invalid book")
    if snapshot.book_stale:
        reasons.append("stale book")
    if snapshot.book_quarantined:
        reasons.append("quarantined book")
    if snapshot.fee_conflict:
        reasons.append("fee conflict")
    return reasons


def _is_extreme(snapshot: MakerRegimeSnapshot, config: MakerRegimeConfig) -> bool:
    return bool(snapshot.fair <= config.moderate_extreme_lo or snapshot.fair >= config.moderate_extreme_hi)


def _is_severe_extreme(snapshot: MakerRegimeSnapshot, config: MakerRegimeConfig) -> bool:
    return bool(snapshot.fair <= config.severe_extreme_lo or snapshot.fair >= config.severe_extreme_hi)


def _is_jump(snapshot: MakerRegimeSnapshot, config: MakerRegimeConfig) -> bool:
    if snapshot.jump_flag:
        return True
    if snapshot.divergence is not None and snapshot.divergence >= config.jump_divergence:
        return True
    if snapshot.vol_fast and snapshot.vol_slow and snapshot.vol_slow > 0:
        return snapshot.vol_fast / snapshot.vol_slow >= config.jump_vol_ratio
    return False


def _confidence(value: float, config: MakerRegimeConfig) -> str:
    if value >= config.high_confidence:
        return "high"
    if value >= config.medium_confidence:
        return "medium"
    return "low"
