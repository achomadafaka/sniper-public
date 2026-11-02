from __future__ import annotations

from dataclasses import dataclass

from .evaluator import observation_order_key


@dataclass(frozen=True)
class ControlMatch:
    observation_id: int | str
    network_id: str
    mint: str
    observed_at: str
    market_cap: float


def _band(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    if value < 1_000_000:
        return 0
    if value < 5_000_000:
        return 1
    if value < 30_000_000:
        return 2
    return 3


def _mint(value):
    value = str(value or "")
    return value.lower() if value.lower().startswith("0x") else value


def select_past_control(signal, eligible_observations, window_s=1800):
    """Pick the nearest already-observed nonmatch in the same chain and market-cap band."""
    signal_key = observation_order_key(signal)
    signal_band = _band(signal.get("their_market_cap"))
    if signal_band is None:
        return None
    signal_network = str(signal.get("network_id") or "")
    signal_mint = _mint(signal.get("mint"))
    choices = []
    for row in eligible_observations:
        key = observation_order_key(row)
        if key >= signal_key:
            continue
        age_s = (signal_key[0] - key[0]).total_seconds()
        if age_s < 0 or age_s > float(window_s):
            continue
        if row.get("predicate_matched") not in (False, 0):
            continue
        if row.get("normalized_side") != "buy":
            continue
        if str(row.get("network_id") or "") != signal_network:
            continue
        if _mint(row.get("mint")) == signal_mint or _band(row.get("their_market_cap")) != signal_band:
            continue
        choices.append((age_s, key[2], row))
    if not choices:
        return None
    row = min(choices, key=lambda item: (item[0], item[1]))[2]
    return ControlMatch(
        observation_id=row.get("id"), network_id=str(row.get("network_id") or ""),
        mint=_mint(row.get("mint")), observed_at=row.get("observed_at"),
        market_cap=float(row.get("their_market_cap")),
    )
