"""Shadow expert predictions for the Hyperliquid Market Radar.

Each expert is a tiny, named hypothesis over a specific horizon. They are settled later against
future mids and used as reliability-weighted evidence, not as a standalone trading strategy.
"""
from __future__ import annotations

from dataclasses import dataclass

from sniper.algos.hl_microstructure.models import FeatureSnapshot


@dataclass(frozen=True)
class ExpertSignal:
    expert_id: str
    family: str
    side: str
    confidence: float
    horizon_ms: int
    rationale: str
    reliability: float = 0.5


def build_expert_signals(snapshot: FeatureSnapshot, reliability: dict | None = None) -> list[ExpertSignal]:
    if snapshot.status != "ok":
        return []
    reliability = reliability or {}
    f = snapshot.features
    out: list[ExpertSignal] = []
    _flow(out, f, reliability)
    _momentum(out, f, reliability)
    _carry(out, f, reliability)
    _reversal(out, f, reliability)
    return out


def _side(value: float, threshold: float) -> str | None:
    if value >= threshold:
        return "long"
    if value <= -threshold:
        return "short"
    return None


def _scale(expert_id: str, base: float, reliability: dict) -> tuple[float, float]:
    rel = reliability.get(expert_id, {})
    acc = _float(rel.get("accuracy"), 0.55 if rel else 0.55)
    n = int(_float(rel.get("n"), 0) or 0)
    # Small samples can help or hurt, but cannot fully dominate the live rule.
    sample_weight = min(1.0, n / 20.0) if n else 0.0
    effective = 0.55 * (1.0 - sample_weight) + max(0.0, min(1.0, acc)) * sample_weight
    if n == 1:
        effective = max(0.0, min(1.0, acc))
    return max(0.0, min(1.0, base * (0.25 + 0.75 * effective))), effective


def _add(out: list[ExpertSignal], reliability: dict, *, expert_id: str, family: str, side: str | None,
         base_confidence: float, horizon_ms: int, rationale: str) -> None:
    if side not in ("long", "short") or base_confidence <= 0:
        return
    confidence, rel = _scale(expert_id, min(1.0, base_confidence), reliability)
    out.append(ExpertSignal(expert_id, family, side, confidence, horizon_ms, rationale, rel))


def _flow(out: list[ExpertSignal], f: dict, reliability: dict) -> None:
    for seconds, threshold, min_count in ((5, 0.45, 3), (10, 0.35, 4), (30, 0.28, 8), (60, 0.22, 12)):
        imb = _float(f.get(f"flow_imbalance_{seconds}s"))
        count = int(_float(f.get(f"trade_count_{seconds}s"), 0) or 0)
        if imb is None or count < min_count:
            continue
        _add(
            out,
            reliability,
            expert_id=f"flow_{seconds}s",
            family="flow",
            side=_side(imb, threshold),
            base_confidence=abs(imb),
            horizon_ms=seconds * 1000,
            rationale=f"{seconds}s taker-flow imbalance",
        )


def _momentum(out: list[ExpertSignal], f: dict, reliability: dict) -> None:
    for seconds, threshold in ((5, 0.0007), (15, 0.0010), (30, 0.0014), (60, 0.0020)):
        ret = _float(f.get(f"return_{seconds}s"))
        if ret is None:
            continue
        _add(
            out,
            reliability,
            expert_id=f"momentum_{seconds}s",
            family="momentum",
            side=_side(ret, threshold),
            base_confidence=min(1.0, abs(ret) / (threshold * 4.0)),
            horizon_ms=seconds * 1000,
            rationale=f"{seconds}s continuation",
        )


def _carry(out: list[ExpertSignal], f: dict, reliability: dict) -> None:
    funding = _float(f.get("funding"))
    if funding is not None and abs(funding) >= 0.00005:
        _add(
            out,
            reliability,
            expert_id="carry_funding",
            family="carry",
            side="short" if funding > 0 else "long",
            base_confidence=min(1.0, abs(funding) / 0.0002),
            horizon_ms=60_000,
            rationale="funding stress mean reversion",
        )
    premium = _float(f.get("premium"))
    if premium is not None and abs(premium) >= 0.001:
        _add(
            out,
            reliability,
            expert_id="carry_premium",
            family="carry",
            side="short" if premium > 0 else "long",
            base_confidence=min(1.0, abs(premium) / 0.005),
            horizon_ms=60_000,
            rationale="premium dislocation mean reversion",
        )


def _reversal(out: list[ExpertSignal], f: dict, reliability: dict) -> None:
    ret_60 = _float(f.get("return_60s"))
    ret_5 = _float(f.get("return_5s"), 0.0)
    vol = _float(f.get("realized_vol_60s"), 0.001) or 0.001
    if ret_60 is None or abs(ret_60) < max(0.003, vol * 3.0):
        return
    if ret_5 is not None and abs(ret_5) > abs(ret_60) * 0.4:
        return
    _add(
        out,
        reliability,
        expert_id="reversal_60s",
        family="reversal",
        side="short" if ret_60 > 0 else "long",
        base_confidence=min(1.0, abs(ret_60) / max(0.006, vol * 5.0)),
        horizon_ms=30_000,
        rationale="60s extension without fresh follow-through",
    )


def _float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
