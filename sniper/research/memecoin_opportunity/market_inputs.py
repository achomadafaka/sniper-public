"""Validated, timestamped native-quote inputs for executable USD clip math."""

from __future__ import annotations

import json
import math
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Mapping

from .identity import require_utc_iso
from .types import VenueId
from .venue_policy import active_capture_venues


@dataclass(frozen=True)
class QuoteMarketInput:
    usd_price_micros: int
    quote_decimals: int
    fee_bps: int
    network_cost_quote_atomic: int
    price_as_of: str


def _coingecko_native_usd_json() -> Mapping[str, object]:
    query = urllib.parse.urlencode(
        {"ids": "solana,ethereum", "vs_currencies": "usd"}
    )
    request = urllib.request.Request(
        f"https://api.coingecko.com/api/v3/simple/price?{query}",
        headers={"Accept": "application/json", "User-Agent": "Sniper-Opportunity-Radar/1"},
    )
    with urllib.request.urlopen(request, timeout=6.0) as response:
        return json.loads(response.read())


def _usd_micros(payload: Mapping[str, object], asset: str) -> int:
    row = payload.get(asset)
    if not isinstance(row, Mapping):
        raise ValueError(f"native USD response missing {asset}")
    try:
        value = int(Decimal(str(row["usd"])) * 1_000_000)
    except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"invalid native USD response for {asset}") from exc
    if value <= 0:
        raise ValueError(f"invalid native USD response for {asset}")
    return value


class NativeUsdPriceFeed:
    """Refresh SOL/ETH USD inputs in-process while retaining explicit cost assumptions."""

    def __init__(self, *, fetch_json=None, refresh_every_s: float = 30.0):
        if not math.isfinite(refresh_every_s) or refresh_every_s <= 0:
            raise ValueError("native USD refresh interval must be finite and > 0")
        self._fetch_json = fetch_json or _coingecko_native_usd_json
        self._refresh_every_s = refresh_every_s
        self._next_refresh_s = 0.0
        self._overlay: dict[str, str] = {}

    def load(
        self,
        env: Mapping[str, str],
        *,
        now_iso: str,
        monotonic_s: float,
    ) -> tuple[dict[VenueId, QuoteMarketInput], dict[VenueId, str]]:
        source = str(
            env.get("MEMECOIN_OPPORTUNITY_NATIVE_PRICE_SOURCE", "coingecko")
        ).strip().lower()
        if source not in {"coingecko", "env"}:
            raise ValueError("MEMECOIN_OPPORTUNITY_NATIVE_PRICE_SOURCE must be coingecko or env")
        if source == "coingecko" and monotonic_s >= self._next_refresh_s:
            try:
                payload = self._fetch_json()
                self._overlay = {
                    "MEMECOIN_OPPORTUNITY_SOL_USD_MICROS": str(_usd_micros(payload, "solana")),
                    "MEMECOIN_OPPORTUNITY_SOL_USD_AS_OF": now_iso,
                    "MEMECOIN_OPPORTUNITY_4663_NATIVE_USD_MICROS": str(
                        _usd_micros(payload, "ethereum")
                    ),
                    "MEMECOIN_OPPORTUNITY_4663_NATIVE_USD_AS_OF": now_iso,
                }
                self._next_refresh_s = monotonic_s + self._refresh_every_s
            except Exception:
                self._next_refresh_s = monotonic_s + min(5.0, self._refresh_every_s)
        merged = dict(env)
        merged.update(self._overlay)
        return load_quote_market_inputs(merged, now_iso=now_iso)


def _age_s(now: str, then: str) -> float:
    left = datetime.fromisoformat(require_utc_iso(now).replace("Z", "+00:00"))
    right = datetime.fromisoformat(require_utc_iso(then).replace("Z", "+00:00"))
    return max(0.0, (left - right).total_seconds())


def load_quote_market_inputs(
    env: Mapping[str, str],
    *,
    now_iso: str,
) -> tuple[dict[VenueId, QuoteMarketInput], dict[VenueId, str]]:
    max_age_s = float(env.get("MEMECOIN_OPPORTUNITY_PRICE_MAX_AGE_S", "300"))
    if not math.isfinite(max_age_s) or max_age_s <= 0:
        raise ValueError("MEMECOIN_OPPORTUNITY_PRICE_MAX_AGE_S must be finite and > 0")
    specs = {
        VenueId.PUMPFUN_SOLANA: (
            "MEMECOIN_OPPORTUNITY_SOL_USD_MICROS",
            "MEMECOIN_OPPORTUNITY_SOL_USD_AS_OF",
            "MEMECOIN_OPPORTUNITY_SOL_NETWORK_COST_LAMPORTS",
            9,
            int(env.get("MEMECOIN_OPPORTUNITY_PUMP_FEE_BPS", "125")),
        ),
        VenueId.BAGS_ROBINHOOD_4663: (
            "MEMECOIN_OPPORTUNITY_4663_NATIVE_USD_MICROS",
            "MEMECOIN_OPPORTUNITY_4663_NATIVE_USD_AS_OF",
            "MEMECOIN_OPPORTUNITY_4663_NETWORK_COST_WEI",
            18,
            200,
        ),
        VenueId.PONS_V1_ROBINHOOD_4663: (
            "MEMECOIN_OPPORTUNITY_4663_NATIVE_USD_MICROS",
            "MEMECOIN_OPPORTUNITY_4663_NATIVE_USD_AS_OF",
            "MEMECOIN_OPPORTUNITY_4663_NETWORK_COST_WEI",
            18,
            100,
        ),
    }
    ready: dict[VenueId, QuoteMarketInput] = {}
    blocked: dict[VenueId, str] = {}
    for venue, (price_key, as_of_key, cost_key, decimals, fee_bps) in specs.items():
        if venue not in active_capture_venues():
            continue
        if price_key not in env or as_of_key not in env or cost_key not in env:
            blocked[venue] = "MISSING_TIMESTAMPED_MARKET_INPUT"
            continue
        price = int(env[price_key])
        cost = int(env[cost_key])
        as_of = require_utc_iso(env[as_of_key])
        if price <= 0 or cost < 0 or not 0 <= fee_bps < 10_000:
            raise ValueError(f"invalid quote input for {venue.value}")
        if _age_s(now_iso, as_of) > max_age_s:
            blocked[venue] = "STALE_NATIVE_USD_PRICE"
            continue
        ready[venue] = QuoteMarketInput(price, decimals, fee_bps, cost, as_of)
    return ready, blocked
