"""Replay engine — deterministic taker-fill simulation over episodes."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from sniper.sim.replay.adapters import EpisodeData, PolymarketAdapter
from sniper.sim.replay.context import ReplayContext, Fill
from sniper.sim.replay.fees import polymarket_taker_fee
from sniper.sim.replay.strategy import Strategy

LOG = logging.getLogger(__name__)


@dataclass
class EpisodeResult:
    """Outcome of replaying one episode."""

    market_id: str
    pnl: float
    starting_cash: float
    ending_cash: float
    fills: list[Fill]
    settlement_payout: float
    outcome: str  # winning outcome label
    metadata: dict[str, Any] = field(default_factory=dict)


class ReplayEngine:
    """Run strategies over episodes with honest taker-fill execution."""

    def __init__(self, starting_cash: float = 1000.0):
        self._starting_cash = starting_cash

    def run_episode(
        self,
        strategy: Strategy,
        episode: EpisodeData,
    ) -> EpisodeResult:
        """Replay a single episode.

        1. Build fee function from episode metadata
        2. Walk timeline: update quotes, let strategy act
        3. Settle positions at resolution
        4. Return deterministic result
        """
        # Build fee function from episode config
        fee_fn = self._build_fee_fn(episode)

        # Initialize context
        ctx = ReplayContext(
            cash=self._starting_cash,
            positions={},
            current_quotes={},
            current_ts="",
            fee_fn=fee_fn,
        )

        # Notify strategy of episode start
        strategy.on_episode_start(episode.metadata)

        # Current quote state per ticker (carry forward)
        quote_state: dict[str, dict] = {}

        # Walk timeline
        for event in episode.timeline:
            ctx.update_time(event.ts)

            if event.kind == "quote":
                # Update the quote for this ticker
                d = event.data
                quote_state[event.ticker] = {
                    "best_bid": d.get("best_bid"),
                    "best_ask": d.get("best_ask"),
                    "bid_size": d.get("bid_size"),
                    "ask_size": d.get("ask_size"),
                }
                ctx.update_quotes(dict(quote_state))

            # Let strategy act after every event (quote or trade)
            strategy.act(ctx)

        # Settlement
        outcomes = self._build_outcomes(episode)
        payout = ctx.settle(outcomes)

        result = EpisodeResult(
            market_id=episode.market_id,
            pnl=ctx.get_pnl(),
            starting_cash=ctx.get_starting_cash(),
            ending_cash=ctx.get_cash(),
            fills=ctx.get_fills(),
            settlement_payout=payout,
            outcome=episode.settlement.get("outcome", ""),
            metadata=episode.metadata,
        )

        strategy.on_episode_end({
            "pnl": result.pnl,
            "fills": len(result.fills),
            "outcome": result.outcome,
        })

        return result

    def run(
        self,
        strategy: Strategy,
        episode_dirs: list[str],
        adapter: Optional[PolymarketAdapter] = None,
    ) -> list[EpisodeResult]:
        """Run strategy over multiple episodes. Returns per-episode results."""
        if adapter is None:
            adapter = PolymarketAdapter()

        results = []
        for episode_dir in episode_dirs:
            episode = adapter.load(episode_dir)
            result = self.run_episode(strategy, episode)
            results.append(result)
            LOG.info(
                "Episode %s: PnL=%.2f, fills=%d, outcome=%s",
                result.market_id, result.pnl, len(result.fills), result.outcome,
            )

        return results

    def _build_fee_fn(self, episode: EpisodeData):
        """Build a ticker-aware fee function from episode metadata.

        Uses per-token fee rates when available, falls back to default fee.
        """
        cfg = episode.fee_fn_args
        if not cfg.get("fees_enabled", False):
            return lambda ticker, price, qty: 0.0

        token_rates = cfg.get("token_fee_rates", {})  # asset_id → rate
        default_fee = cfg.get("default_fee")
        default_rate = float(default_fee) if default_fee else 0.02

        # Build ticker → fee_rate lookup from token metadata
        ticker_rates: dict[str, float] = {}
        tokens = episode.metadata.get("tokens", [])
        for tok in tokens:
            tid = tok.get("token_id", "")
            label = tok.get("label") or tok.get("outcome", "")
            if tid in token_rates:
                ticker_rates[label] = token_rates[tid]

        def fee_fn(ticker: str, price: float, qty: float) -> float:
            rate = ticker_rates.get(ticker, default_rate)
            return polymarket_taker_fee(price, qty, rate)

        return fee_fn

    def _build_outcomes(self, episode: EpisodeData) -> dict[str, float]:
        """Build {ticker: payout} from settlement."""
        settlement = episode.settlement
        outcome_prices = settlement.get("outcome_prices", [])
        outcomes_list = episode.metadata.get("outcomes", [])

        payouts: dict[str, float] = {}
        for i, label in enumerate(outcomes_list):
            if i < len(outcome_prices):
                payouts[label] = float(outcome_prices[i])
            else:
                payouts[label] = 0.0

        return payouts
