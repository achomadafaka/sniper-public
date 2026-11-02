"""Binance observation adapter — a PREDICTOR feature source only.

Binance is NEVER settlement truth, NEVER the Price-to-Beat, NEVER the label (those come from the
Chainlink-rule Polymarket outcome). This module normalizes three delivery sources to a common
``BinanceObservation``:

- ``rtds_price``      — Polymarket RTDS Binance feed: symbol/timestamp/value (PRICE ONLY). Volume fields
                        stay ``None`` and are NEVER fabricated from price.
- ``binance_aggtrade`` — direct Binance aggTrade stream: price + traded quantity (+ aggregated count).
- ``binance_kline_1s`` — direct Binance 1s kline stream: close + volume + quote volume + trade count.

Parsers are pure (dict in, observation out) so they are unit-testable without a socket. ``source_event_ts``
is the exchange event time in MILLISECONDS; ``receive_ts`` is our server-time-of-receipt in SECONDS — both
carried so the feature layer can enforce known-at-decision cutoffs.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

RTDS_PRICE = "rtds_price"
AGG_TRADE = "binance_aggtrade"
KLINE_1S = "binance_kline_1s"


@dataclass(frozen=True)
class BinanceObservation:
    symbol: str
    source_event_ts: int            # exchange event time (ms)
    receive_ts: int                 # our receipt time (s)
    price: float
    quantity: Optional[float]       # None for price-only (rtds_price) — never fabricated
    quote_quantity: Optional[float]
    trade_count: Optional[int]
    delivery_source: str
    raw: dict
    seq: Optional[str] = None        # within-(symbol,ts,source) disambiguator (e.g. aggTrade last id)

    @property
    def dedup_key(self) -> str:
        base = f"{self.symbol}|{self.source_event_ts}|{self.delivery_source}"
        return f"{base}|{self.seq}" if self.seq is not None else base


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None       # malformed numeric field -> fail closed (no observation), never raise


def parse_rtds_price(msg, *, recv_ts: int) -> Optional[BinanceObservation]:
    """Polymarket RTDS Binance price tick: ``{symbol, timestamp(ms), value}``. PRICE ONLY — no volume."""
    if not isinstance(msg, dict):
        return None
    sym = msg.get("symbol")
    ts, rts, price = _int(msg.get("timestamp")), _int(recv_ts), _f(msg.get("value"))
    if sym is None or ts is None or rts is None or price is None:
        return None
    return BinanceObservation(str(sym).upper(), ts, rts, price, None, None, None, RTDS_PRICE, msg)


def parse_agg_trade(msg, *, recv_ts: int) -> Optional[BinanceObservation]:
    """Direct Binance ``aggTrade`` stream: ``{s, p, q, T, f, l}``. Carries traded quantity (real volume)."""
    if not isinstance(msg, dict):
        return None
    sym = msg.get("s")
    T, rts = _int(msg.get("T")), _int(recv_ts)
    price, qty = _f(msg.get("p")), _f(msg.get("q"))
    if sym is None or T is None or rts is None or price is None or qty is None:
        return None
    first, last = _int(msg.get("f")), _int(msg.get("l"))
    tc = (last - first + 1) if (first is not None and last is not None) else None
    seq = str(last) if last is not None else None     # distinct same-ms trades must not dedup-collide
    return BinanceObservation(str(sym).upper(), T, rts, price, qty, price * qty, tc, AGG_TRADE, msg, seq)


def parse_kline_1s(msg, *, recv_ts: int) -> Optional[BinanceObservation]:
    """Direct Binance 1s ``kline`` stream: ``{s, k:{T, c, v, q, n, x}}``. Close + volume + quote + trades.
    REQUIRES the closed flag ``k.x is True`` — a partial/ambiguous bar is never persisted (fail closed)."""
    if not isinstance(msg, dict):
        return None
    k = msg.get("k") if isinstance(msg.get("k"), dict) else {}
    if k.get("x") is not True:
        return None
    sym = msg.get("s") or k.get("s")
    close, T, rts = _f(k.get("c")), _int(k.get("T")), _int(recv_ts)
    if sym is None or close is None or T is None or rts is None:
        return None
    return BinanceObservation(str(sym).upper(), T, rts, close, _f(k.get("v")), _f(k.get("q")),
                              _int(k.get("n")), KLINE_1S, msg)
