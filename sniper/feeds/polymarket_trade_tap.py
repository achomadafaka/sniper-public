"""Polymarket Trade Tap — lightweight async WebSocket client.

Subscribes to ``last_trade_price`` events for a dynamic set of token_ids and
emits unfiltered ``TradeTick`` objects to a caller-supplied callback.

Key design decisions:
- Uses ``assets_ids`` (plural) in the subscription payload — ADR-005 / CLAUDE.md.
  Using ``asset_ids`` (singular) is silently accepted by Polymarket but delivers
  zero data.
- Reconnects with exponential back-off (5s → 120s cap).
- Resubscribes automatically when the watchlist changes between reconnects.
- Completely separate from ``stream_low_buys.py`` which has its own filtering
  logic for wallet/conviction signals.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Callable, Awaitable

logger = logging.getLogger(__name__)

_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TradeTick:
    """A single resolved trade event from the Polymarket CLOB WebSocket."""

    token_id: str
    condition_id: str
    price: float
    size: float
    side: str
    timestamp_ms: int


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class TradeTapState:
    """Mutable watchlist — thread-safe for single-threaded async use."""

    def __init__(self) -> None:
        self.watched_tokens: set[str] = set()

    def watch(self, token_id: str) -> None:
        """Add *token_id* to the watched set."""
        self.watched_tokens.add(token_id)

    def unwatch(self, token_id: str) -> None:
        """Remove *token_id* from the watched set (no-op if absent)."""
        self.watched_tokens.discard(token_id)

    def is_watched(self, token_id: str) -> bool:
        """Return True if *token_id* is currently being watched."""
        return token_id in self.watched_tokens


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_trade_message(msg: list) -> list[TradeTick]:
    """Parse a WebSocket message array into a list of ``TradeTick`` objects.

    Only entries with ``event_type == "last_trade_price"`` are processed.
    Malformed entries (KeyError, ValueError, TypeError) are silently skipped
    so one bad event never kills the feed.

    Args:
        msg: The outer list received from the WebSocket (already JSON-decoded).

    Returns:
        A (possibly empty) list of ``TradeTick`` objects.
    """
    ticks: list[TradeTick] = []
    for entry in msg:
        try:
            if not isinstance(entry, dict):
                continue
            if entry.get("event_type") != "last_trade_price":
                continue
            tick = TradeTick(
                token_id=entry["asset_id"],
                condition_id=entry["market"],
                price=float(entry["price"]),
                size=float(entry["size"]),
                side=entry["side"],
                timestamp_ms=int(entry["timestamp_ms"]),
            )
            ticks.append(tick)
        except (KeyError, ValueError, TypeError):
            # Skip malformed entries — never raise; the feed must stay alive.
            continue
    return ticks


# ---------------------------------------------------------------------------
# Async runner
# ---------------------------------------------------------------------------


async def run_trade_tap(
    state: TradeTapState,
    on_tick: Callable[[TradeTick], Awaitable[None] | None],
    reconnect_delay: float = 5.0,
    max_reconnect_delay: float = 120.0,
) -> None:
    """Run the Polymarket trade tap forever, reconnecting on any error.

    Args:
        state: Mutable watchlist. The runner reads ``state.watched_tokens`` on
               each (re)connect to build the subscription payload.
        on_tick: Async or sync callable invoked for every parsed tick.
                 Signature: ``on_tick(tick: TradeTick)``.
        reconnect_delay: Initial back-off seconds before reconnect.
        max_reconnect_delay: Cap for exponential back-off.

    Notes:
        - Subscription key is ``assets_ids`` (plural) per ADR-005.
        - An empty watchlist is a valid (no-op) subscription — the WS
          connection is still opened so new tokens can be picked up on
          the next reconnect.
    """
    try:
        import websockets  # type: ignore[import]
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "websockets package is required for run_trade_tap. "
            "Install with: pip install websockets"
        ) from exc

    delay = reconnect_delay

    while True:
        token_snapshot = list(state.watched_tokens)
        logger.info(
            "trade_tap connecting url=%s tokens=%d", _WS_URL, len(token_snapshot)
        )
        try:
            async with websockets.connect(
                _WS_URL,
                ping_interval=20,
                ping_timeout=60,
                close_timeout=10,
            ) as ws:
                # Subscribe to the current watchlist.
                sub_msg = json.dumps(
                    {
                        "type": "market",
                        # CRITICAL: assets_ids (plural) — see ADR-005 / CLAUDE.md
                        "assets_ids": token_snapshot,
                    }
                )
                await ws.send(sub_msg)
                logger.info("trade_tap subscribed assets_ids=%s", token_snapshot)

                # Reset back-off on successful connect.
                delay = reconnect_delay

                async for raw in ws:
                    try:
                        data = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        logger.warning("trade_tap: unparseable message skipped")
                        continue

                    # The Polymarket WS sends either a list or a single dict.
                    if isinstance(data, dict):
                        data = [data]
                    if not isinstance(data, list):
                        continue

                    for tick in parse_trade_message(data):
                        try:
                            result = on_tick(tick)
                            if asyncio.isfuture(result) or asyncio.iscoroutine(result):
                                await result
                        except Exception:
                            logger.exception(
                                "trade_tap: on_tick raised for token=%s", tick.token_id
                            )

        except Exception:
            logger.warning(
                "trade_tap disconnected, reconnecting in %.1fs", delay, exc_info=True
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_reconnect_delay)
