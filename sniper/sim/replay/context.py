"""ReplayContext — quote lookup, position tracking, order placement."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class OrderResult:
    filled: int = 0
    fill_price: float = 0.0
    fee: float = 0.0
    rejected: bool = False
    partial: bool = False
    reason: str = ""


@dataclass
class Fill:
    """Record of a single executed fill."""
    ts: str = ""
    ticker: str = ""
    side: str = ""
    count: int = 0
    price: float = 0.0
    fee: float = 0.0


class ReplayContext:
    """Mutable simulation state for one episode. Updated by the replay engine.

    Positions and fill counts are integers. This is deliberate for v1:
    Polymarket contracts are integer-denominated (whole shares), and liquidity
    sizes are truncated to int via int(available). Fractional positions and
    sub-share fills are not supported. Cash and prices remain float.
    """

    def __init__(
        self,
        cash: float,
        positions: dict[str, int],
        current_quotes: dict[str, dict],
        current_ts: str,
        fee_fn: Callable[[str, float, float], float],
    ):
        self._starting_cash = cash
        self._cash = cash
        self._positions = dict(positions)
        self._quotes = current_quotes
        self._ts = current_ts
        self._fee_fn = fee_fn
        self._fills: list[Fill] = []

    def get_cash(self) -> float:
        return self._cash

    def get_starting_cash(self) -> float:
        return self._starting_cash

    def get_pnl(self) -> float:
        """Net PnL = current cash - starting cash (valid after settlement)."""
        return self._cash - self._starting_cash

    def get_fills(self) -> list[Fill]:
        return list(self._fills)

    def get_positions(self) -> dict[str, int]:
        return dict(self._positions)

    def get_time(self) -> str:
        return self._ts

    def get_markets(self) -> dict[str, dict]:
        return dict(self._quotes)

    def update_quotes(self, quotes: dict[str, dict]) -> None:
        self._quotes = quotes

    def update_time(self, ts: str) -> None:
        self._ts = ts

    def place_order(self, ticker: str, side: str, count: int) -> OrderResult:
        if ticker not in self._quotes:
            return OrderResult(rejected=True, reason="no quote")

        quote = self._quotes[ticker]
        if side == "BUY":
            price = quote.get("best_ask")
            available = quote.get("ask_size")
        elif side == "SELL":
            price = quote.get("best_bid")
            available = quote.get("bid_size")
        else:
            return OrderResult(rejected=True, reason=f"invalid side: {side}")

        if price is None:
            return OrderResult(rejected=True, reason="no price")
        if available is None:
            return OrderResult(rejected=True, reason="no liquidity data")

        # Cap to available liquidity
        fill_count = min(count, int(available))
        if fill_count <= 0:
            return OrderResult(rejected=True, reason="no liquidity")

        # For buys: cap to what cash allows
        if side == "BUY":
            max_affordable = int(self._cash / price) if price > 0 else 0
            fill_count = min(fill_count, max_affordable)
            if fill_count <= 0:
                return OrderResult(rejected=True, reason="insufficient cash")

        # For sells: cap to held position
        if side == "SELL":
            held = self._positions.get(ticker, 0)
            fill_count = min(fill_count, held)
            if fill_count <= 0:
                return OrderResult(rejected=True, reason="no position")

        fee = self._fee_fn(ticker, price, fill_count)
        cost = price * fill_count + fee

        if side == "BUY":
            self._cash -= cost
            self._positions[ticker] = self._positions.get(ticker, 0) + fill_count
        else:
            proceeds = price * fill_count - fee
            self._cash += proceeds
            self._positions[ticker] = self._positions.get(ticker, 0) - fill_count

        partial = fill_count < count
        self._fills.append(Fill(
            ts=self._ts, ticker=ticker, side=side,
            count=fill_count, price=price, fee=fee,
        ))

        return OrderResult(
            filled=fill_count,
            fill_price=price,
            fee=fee,
            partial=partial,
        )

    def settle(self, outcomes: dict[str, float]) -> float:
        """Settle all positions. Returns gross settlement payout (not PnL).

        Use get_pnl() after settle() for net profit/loss.
        outcomes: {ticker: payout_per_share} e.g. {"YES": 1.0, "NO": 0.0}
        """
        settlement_cash = 0.0
        for ticker, shares in self._positions.items():
            payout = outcomes.get(ticker, 0.0)
            settlement_cash += shares * payout
        self._cash += settlement_cash
        self._positions.clear()
        return settlement_cash
