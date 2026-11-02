"""
instance_profile.py — Typed instance configuration loaded from environment variables.

Each VPS instance reads its own profile from env vars, allowing the same codebase
to run different strategy profiles on different machines.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping, Optional


@dataclass(frozen=True)
class InstanceProfile:
    instance_tag: str
    display_name: str
    role: str
    enabled: bool
    enabled_strategies: tuple[str, ...]
    enabled_feeds: tuple[str, ...]
    paper_only: bool
    real_trading_enabled: bool
    bankroll_usdc: float
    max_total_exposure_usdc: float
    max_market_exposure_usdc: float
    max_concurrent_positions: int
    daily_loss_limit_usdc: float
    default_loop_seconds: int
    # --- Hyperliquid venue limits ---
    hl_max_exposure_usdc: float = 0.0      # 0 = venue disabled
    hl_max_leverage: float = 3.0
    hl_max_concurrent: int = 5


def _parse_bool(value: str, default: bool) -> bool:
    """Parse a boolean-like env var string."""
    v = value.strip().lower()
    if v in ("1", "true", "yes"):
        return True
    if v in ("0", "false", "no"):
        return False
    return default


def _parse_csv(value: str) -> tuple[str, ...]:
    """Split a CSV string, strip whitespace, lowercase, filter empty strings."""
    return tuple(
        part.strip().lower()
        for part in value.split(",")
        if part.strip()
    )


def load_instance_profile(env: Optional[Mapping[str, str]] = None) -> InstanceProfile:
    """
    Build an InstanceProfile from environment variables.

    Parameters
    ----------
    env:
        Optional mapping to use instead of ``os.environ``. Useful in tests.

    Returns
    -------
    InstanceProfile

    Raises
    ------
    ValueError
        If a numeric env var contains a non-numeric value.
    """
    if env is None:
        env = os.environ

    # --- string fields ---
    instance_tag = env.get("INSTANCE_TAG", "").strip() or "default-local"
    display_name = env.get("INSTANCE_DISPLAY_NAME", "").strip() or instance_tag
    role = env.get("INSTANCE_ROLE", "paper").strip() or "paper"

    # --- bool fields ---
    enabled = _parse_bool(env.get("INSTANCE_ENABLED", "1"), default=True)
    paper_only = _parse_bool(env.get("PAPER_ONLY", "1"), default=True)

    real_trading_raw = _parse_bool(env.get("REAL_TRADING_ENABLED", "0"), default=False)
    # paper_only always wins
    real_trading_enabled = False if paper_only else real_trading_raw

    # --- CSV fields ---
    enabled_strategies = _parse_csv(env.get("ENABLED_STRATEGIES", ""))
    enabled_feeds = _parse_csv(env.get("ENABLED_FEEDS", ""))

    # --- numeric fields ---
    try:
        bankroll_usdc = float(env.get("INSTANCE_BANKROLL_USDC", "1000.0"))
    except ValueError:
        raise ValueError(
            f"INSTANCE_BANKROLL_USDC is not a valid number: {env.get('INSTANCE_BANKROLL_USDC')!r}"
        )

    try:
        max_total_exposure_usdc = float(env.get("INSTANCE_MAX_TOTAL_EXPOSURE_USDC", "1000.0"))
    except ValueError:
        raise ValueError(
            f"INSTANCE_MAX_TOTAL_EXPOSURE_USDC is not a valid number: "
            f"{env.get('INSTANCE_MAX_TOTAL_EXPOSURE_USDC')!r}"
        )

    try:
        max_market_exposure_usdc = float(env.get("INSTANCE_MAX_MARKET_EXPOSURE_USDC", "200.0"))
    except ValueError:
        raise ValueError(
            f"INSTANCE_MAX_MARKET_EXPOSURE_USDC is not a valid number: "
            f"{env.get('INSTANCE_MAX_MARKET_EXPOSURE_USDC')!r}"
        )

    try:
        max_concurrent_positions = int(env.get("INSTANCE_MAX_CONCURRENT_POSITIONS", "20"))
    except ValueError:
        raise ValueError(
            f"INSTANCE_MAX_CONCURRENT_POSITIONS is not a valid integer: "
            f"{env.get('INSTANCE_MAX_CONCURRENT_POSITIONS')!r}"
        )

    try:
        daily_loss_limit_usdc = float(env.get("INSTANCE_DAILY_LOSS_LIMIT_USDC", "50.0"))
    except ValueError:
        raise ValueError(
            f"INSTANCE_DAILY_LOSS_LIMIT_USDC is not a valid number: "
            f"{env.get('INSTANCE_DAILY_LOSS_LIMIT_USDC')!r}"
        )

    try:
        default_loop_seconds = int(env.get("STRATEGY_LOOP_SECONDS", "60"))
    except ValueError:
        raise ValueError(
            f"STRATEGY_LOOP_SECONDS is not a valid integer: "
            f"{env.get('STRATEGY_LOOP_SECONDS')!r}"
        )

    # --- Hyperliquid venue limits ---
    try:
        hl_max_exposure_usdc = float(env.get("INSTANCE_HL_MAX_EXPOSURE_USDC", "0.0"))
    except ValueError:
        raise ValueError(
            f"INSTANCE_HL_MAX_EXPOSURE_USDC is not a valid number: "
            f"{env.get('INSTANCE_HL_MAX_EXPOSURE_USDC')!r}"
        )

    try:
        hl_max_leverage = float(env.get("INSTANCE_HL_MAX_LEVERAGE", "3.0"))
    except ValueError:
        raise ValueError(
            f"INSTANCE_HL_MAX_LEVERAGE is not a valid number: "
            f"{env.get('INSTANCE_HL_MAX_LEVERAGE')!r}"
        )

    try:
        hl_max_concurrent = int(env.get("INSTANCE_HL_MAX_CONCURRENT", "5"))
    except ValueError:
        raise ValueError(
            f"INSTANCE_HL_MAX_CONCURRENT is not a valid integer: "
            f"{env.get('INSTANCE_HL_MAX_CONCURRENT')!r}"
        )

    return InstanceProfile(
        instance_tag=instance_tag,
        display_name=display_name,
        role=role,
        enabled=enabled,
        enabled_strategies=enabled_strategies,
        enabled_feeds=enabled_feeds,
        paper_only=paper_only,
        real_trading_enabled=real_trading_enabled,
        bankroll_usdc=bankroll_usdc,
        max_total_exposure_usdc=max_total_exposure_usdc,
        max_market_exposure_usdc=max_market_exposure_usdc,
        max_concurrent_positions=max_concurrent_positions,
        daily_loss_limit_usdc=daily_loss_limit_usdc,
        default_loop_seconds=default_loop_seconds,
        hl_max_exposure_usdc=hl_max_exposure_usdc,
        hl_max_leverage=hl_max_leverage,
        hl_max_concurrent=hl_max_concurrent,
    )
