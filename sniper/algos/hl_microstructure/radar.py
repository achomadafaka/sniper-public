"""Market-selection radar for Hyperliquid live paper research."""
from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class RadarCandidate:
    asset: str
    score: float
    rank: int
    reasons: list[str]
    volume_usdc: float
    open_interest: float
    funding: float | None
    premium: float | None
    selected: bool = True


def _to_float(value, default: float | None = None) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _ctx(row: dict) -> dict:
    if isinstance(row.get("ctx"), dict):
        return row["ctx"]
    return row


def _name(row: dict) -> str:
    return str(row.get("name") or row.get("asset") or row.get("coin") or "").upper()


def _candidate(row: dict, *, min_volume_usdc: float) -> RadarCandidate | None:
    if row.get("isDelisted"):
        return None
    asset = _name(row)
    ctx = _ctx(row)
    mid = _to_float(ctx.get("midPx") or ctx.get("mid") or ctx.get("markPx"))
    volume = _to_float(ctx.get("dayNtlVlm") or ctx.get("volume_usdc"), 0.0) or 0.0
    oi = _to_float(ctx.get("openInterest") or ctx.get("open_interest"), 0.0) or 0.0
    if not asset or mid is None or mid <= 0 or volume < min_volume_usdc:
        return None
    funding = _to_float(ctx.get("funding"))
    premium = _to_float(ctx.get("premium"))
    score = math.log10(volume + 1.0)
    reasons = ["volume"]
    if oi > 0:
        score += min(math.log10(oi + 1.0) / 3.0, 3.0)
        reasons.append("open_interest")
    if funding is not None and abs(funding) >= 0.00005:
        score += min(abs(funding) / 0.00005, 2.0) * 0.8
        reasons.append("funding_extreme")
    if premium is not None and abs(premium) >= 0.001:
        score += min(abs(premium) / 0.001, 2.0) * 0.7
        reasons.append("premium_dislocation")
    return RadarCandidate(asset, score, 0, reasons, volume, oi, funding, premium)


def rank_markets(markets: list[dict], *, max_assets: int = 6, min_volume_usdc: float = 1_000_000) -> list[RadarCandidate]:
    """Return the best active assets for live paper subscriptions."""
    candidates = [c for c in (_candidate(row, min_volume_usdc=min_volume_usdc) for row in markets) if c is not None]
    candidates.sort(key=lambda c: (-c.score, -c.volume_usdc, c.asset))
    out = []
    for idx, c in enumerate(candidates[: max(0, int(max_assets))], start=1):
        out.append(RadarCandidate(
            c.asset, c.score, idx, c.reasons, c.volume_usdc, c.open_interest,
            c.funding, c.premium, True,
        ))
    return out
