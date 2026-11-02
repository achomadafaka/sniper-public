"""Adapter that wires Polymarket's CLOB `/prices-history` endpoint into
the wallet protocol.

Closes wallet-protocol-bulletproofing gap C5. Lets the protocol's CLI
inject a `price_history_fn` so H6 (held-analyst exit quality) can run
instead of always returning `underpowered`.

Contract
--------
The closure returned by `build_price_history_fn` matches the protocol
`PriceHistoryFn` shape:

    fn(market_id: str, ts: int) -> float | None

`market_id` is the Polymarket CLOB asset id (the same `asset` field
present on /activity rows) -- NOT the conditionId. The CLOB endpoint
silently returns an empty `history` for conditionIds, so callers must
pass asset ids.

`ts` is the raw unix timestamp to look up. The adapter applies no implicit
offset. Callers that need a future price, such as H6's T+24h exit-quality
benchmark, must add that offset before calling. Returns the mid price of
the first bar at or after `ts`, or `None` on HTTP error / empty history.

Memoization
-----------
Repeated lookups for the same `(market_id, ts)` reuse the prior
result via an in-process dict. Pass an explicit `cache` to share state
across invocations, or omit to get a fresh cache per
`build_price_history_fn` call.
"""

from __future__ import annotations

import importlib as _importlib
from typing import Any, Callable

import httpx

CLOB_PRICES_HISTORY_URL = "https://clob.polymarket.com/prices-history"

# Keep the requests module accessible on this namespace so that existing patch
# targets like `patch('...price_history_adapter.requests.Session')` continue
# to work. Loaded via importlib to avoid a bare top-level requests-import.
try:
    requests = _importlib.import_module("requests")
except ImportError:
    requests = None  # type: ignore[assignment]


def build_price_history_fn(
    session: httpx.Client | None = None,
    fidelity: int = 60,
    cache: dict | None = None,
    timeout: float = 10.0,
) -> Callable[[str, int], float | None]:
    """Return a memoized closure that fetches mid price at the given ts.

    Args:
        session: Optional session for connection reuse. A new
            requests.Session is created when None (allows existing
            patch targets to work; httpx.Client is also accepted).
        fidelity: prices-history `fidelity` query param. 60 = 60-second
            bars (per CLOB API docs verified 2026-04-26).
        cache: Optional dict for memoization. When None, a fresh dict
            is allocated per `build_price_history_fn` call.
        timeout: per-request HTTP timeout in seconds.

    Returns:
        A callable matching the protocol `PriceHistoryFn`.
    """
    if session is None:
        session = requests.Session()  # type: ignore[union-attr]
    if cache is None:
        cache = {}

    def _price_history_fn(market_id: str, ts: int) -> float | None:
        if not market_id or not ts:
            return None
        key = (market_id, int(ts))
        if key in cache:
            return cache[key]
        target_ts = int(ts)
        window = max(int(fidelity) * 60, 600)
        params: dict[str, Any] = {
            "market": market_id,
            "fidelity": fidelity,
            "startTs": target_ts,
            "endTs": target_ts + window,
        }
        try:
            resp = session.get(
                CLOB_PRICES_HISTORY_URL, params=params, timeout=timeout,
            )
        except Exception:
            # Catch any request error (httpx.RequestError, requests.ConnectionError, etc.)
            cache[key] = None
            return None
        if resp.status_code >= 400:
            cache[key] = None
            return None
        try:
            data = resp.json()
        except ValueError:
            cache[key] = None
            return None
        history = (data or {}).get("history") or []
        if not history:
            cache[key] = None
            return None
        # First bar at or after target_ts; fall back to the last bar in the
        # tight window if none reach the target (window edge effect).
        chosen: dict | None = None
        for bar in history:
            try:
                t = int(bar.get("t"))
            except (TypeError, ValueError):
                continue
            if t >= target_ts:
                chosen = bar
                break
        if chosen is None:
            chosen = history[-1]
        try:
            price = float(chosen.get("p"))
        except (TypeError, ValueError):
            cache[key] = None
            return None
        cache[key] = price
        return price

    return _price_history_fn


__all__ = ["build_price_history_fn", "CLOB_PRICES_HISTORY_URL"]
