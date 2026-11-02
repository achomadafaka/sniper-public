"""Canonical symbol semantics for the HL alt-short research cell."""
from __future__ import annotations


ALT_SHORT_EXCLUDED = frozenset({"BTC", "ETH", "SOL"})


def provider_coin(value: str | None) -> str | None:
    if value is None:
        return None
    coin = str(value).strip()
    return coin or None


def coin_comparison_key(value: str | None) -> str | None:
    coin = provider_coin(value)
    return coin.upper() if coin else None


def normalize_coin(value: str | None) -> str | None:
    """Legacy comparison key. Never use this value for a provider request or stored identity."""
    return coin_comparison_key(value)


def provider_coin_index(values) -> dict[str, str]:
    """Map comparison keys to exact provider names; reject case-colliding identities."""
    index: dict[str, str] = {}
    for value in values:
        coin = provider_coin(value)
        key = coin_comparison_key(coin)
        if coin is None or key is None:
            continue
        previous = index.get(key)
        if previous is not None and previous != coin:
            raise ValueError(f"ambiguous provider coin identity: {key}")
        index[key] = coin
    return index


def lookup_coin(mapping: dict, coin: str | None):
    """Return the exact key/value matching ``coin`` by comparison key, or ``None``."""
    key = coin_comparison_key(coin)
    if key is None:
        return None
    provider = provider_coin_index(mapping).get(key)
    return (provider, mapping[provider]) if provider is not None else None


def strategy_bucket(coin: str | None) -> str:
    normalized = normalize_coin(coin)
    if normalized is None:
        return "unknown"
    return "core" if normalized in ALT_SHORT_EXCLUDED else "alt"


def is_alt_short(coin: str | None, side: str | None) -> bool:
    normalized_side = str(side).strip().lower() if side is not None else ""
    return normalized_side == "short" and strategy_bucket(coin) == "alt"
