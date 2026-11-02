"""Fusion policy for Hyperliquid market-radar paper trades."""
from __future__ import annotations

from dataclasses import dataclass, field

from sniper.algos.hl_microstructure.catalysts import CatalystSignal
from sniper.algos.hl_microstructure.experts import ExpertSignal
from sniper.algos.hl_microstructure.liquidation import LiquidationStress
from sniper.algos.hl_microstructure.models import FeatureSnapshot, PolicyDecision
from sniper.algos.hl_microstructure.radar import RadarCandidate


@dataclass(frozen=True)
class FusionContext:
    catalysts: dict[str, CatalystSignal] = field(default_factory=dict)
    liquidation_stress: dict[str, LiquidationStress] = field(default_factory=dict)
    radar_candidates: dict[str, RadarCandidate] = field(default_factory=dict)
    expert_signals: dict[str, list[ExpertSignal]] = field(default_factory=dict)


def _feature(snapshot: FeatureSnapshot, key: str, default=None):
    value = snapshot.features.get(key)
    return default if value is None else value


def _side_from_signed(value: float, threshold: float) -> str | None:
    if value >= threshold:
        return "long"
    if value <= -threshold:
        return "short"
    return None


def _vote(votes: dict[str, tuple[str, float]], family: str, side: str | None, score: float) -> None:
    if side in ("long", "short") and score > 0:
        votes[family] = (side, min(1.0, float(score)))


def source_votes(snapshot: FeatureSnapshot, context: FusionContext) -> dict[str, tuple[str, float]]:
    asset = snapshot.asset.upper()
    votes: dict[str, tuple[str, float]] = {}
    flow = float(_feature(snapshot, "flow_imbalance_10s", 0.0))
    book = float(_feature(snapshot, "book_imbalance_5", 0.0))
    ret5 = float(_feature(snapshot, "return_5s", 0.0))
    funding = _feature(snapshot, "funding")
    premium = _feature(snapshot, "premium")
    _vote(votes, "flow", _side_from_signed(flow, 0.35), abs(flow))
    _vote(votes, "book", _side_from_signed(book, 0.25), abs(book))
    if abs(ret5) >= 0.0007:
        _vote(votes, "momentum", "long" if ret5 > 0 else "short", min(1.0, abs(ret5) / 0.003))
    if funding is not None and abs(float(funding)) >= 0.00005:
        _vote(votes, "funding", "short" if float(funding) > 0 else "long", min(1.0, abs(float(funding)) / 0.0002))
    if premium is not None and abs(float(premium)) >= 0.001:
        _vote(votes, "premium", "short" if float(premium) > 0 else "long", min(1.0, abs(float(premium)) / 0.005))
    catalyst = context.catalysts.get(asset)
    if catalyst is not None:
        _vote(votes, "catalyst", catalyst.direction, catalyst.score)
    liq = context.liquidation_stress.get(asset)
    if liq is not None and not liq.stale:
        _vote(votes, "liquidation", liq.direction, liq.score)
    expert_side, expert_score = _expert_vote(context.expert_signals.get(asset) or [])
    _vote(votes, "expert", expert_side, expert_score)
    return votes


def _expert_vote(signals: list[ExpertSignal]) -> tuple[str | None, float]:
    totals = {"long": 0.0, "short": 0.0}
    for s in signals:
        if s.side in totals and s.confidence >= 0.25:
            totals[s.side] += float(s.confidence)
    if totals["long"] > totals["short"]:
        return "long", min(1.0, totals["long"] - totals["short"])
    if totals["short"] > totals["long"]:
        return "short", min(1.0, totals["short"] - totals["long"])
    return None, 0.0


def _winner(votes: dict[str, tuple[str, float]]) -> tuple[str | None, float, int]:
    totals = {"long": 0.0, "short": 0.0}
    counts = {"long": 0, "short": 0}
    for side, score in votes.values():
        totals[side] += score
        counts[side] += 1
    if totals["long"] > totals["short"]:
        return "long", totals["long"] - totals["short"], counts["long"]
    if totals["short"] > totals["long"]:
        return "short", totals["short"] - totals["long"], counts["short"]
    return None, 0.0, 0


def evaluate_fusion_policy(snapshot: FeatureSnapshot, context: FusionContext | None = None) -> PolicyDecision:
    context = context or FusionContext()
    if snapshot.status != "ok":
        return PolicyDecision("fusion_radar", "skip", None, snapshot.status, 0.0)
    spread = snapshot.features.get("spread_bps")
    if spread is None:
        return PolicyDecision("fusion_radar", "skip", None, "book_missing", 0.0)
    if float(spread) > 15.0:
        return PolicyDecision("fusion_radar", "skip", None, "spread_too_wide", 0.0)
    if int(_feature(snapshot, "trade_count_10s", 0)) < 3:
        return PolicyDecision("fusion_radar", "skip", None, "not_enough_flow", 0.0)
    votes = source_votes(snapshot, context)
    side, edge, count = _winner(votes)
    if side is None or count < 2:
        return PolicyDecision("fusion_radar", "skip", None, "needs_two_source_families", 0.0)
    confidence = min(1.0, edge / max(1.0, count * 0.85))
    if confidence < 0.35:
        return PolicyDecision("fusion_radar", "skip", None, "weak_fusion_score", confidence)
    vote_payload = {
        family: {"active": True, "side": vote_side, "score": score}
        for family, (vote_side, score) in votes.items()
    }
    return PolicyDecision(
        "fusion_radar",
        "enter",
        side,
        f"fusion_{count}_source_{side}",
        confidence,
        {
            "source_votes": vote_payload,
            "exit_plan": dynamic_exit_plan(snapshot, side),
            "thesis": f"{snapshot.asset} {side}: {count} source families agree in the Market Radar.",
        },
    )


def dynamic_exit_plan(snapshot: FeatureSnapshot, side: str) -> dict:
    vol = snapshot.features.get("realized_vol_60s")
    try:
        vol_f = max(0.0007, float(vol))
    except (TypeError, ValueError):
        vol_f = 0.001
    stop = min(0.012, max(0.0015, vol_f * 1.4))
    take = min(0.024, max(stop * 1.8, vol_f * 2.6))
    return {
        "side": side,
        "stop_loss_pct": stop,
        "take_profit_pct": take,
        "trail_after_pct": max(take * 0.65, stop),
        "time_stop_ms": 180_000 if vol_f < 0.006 else 300_000,
        "min_signal_decay_hold_ms": 30_000,
        "decay_exit": "exit when fewer than two source families still agree",
    }
