"""Cross-market arbitrage v1 strategy plugin.

Replicates the GamblingIsAllYouNeed strategy ($4.4M PnL, 100% WR, clarity 30/100):
  - Scan active binary markets where YES + NO prices sum to < threshold (default: 0.97)
  - Buy BOTH sides for a guaranteed profit at resolution regardless of outcome
  - Edge: price sum < 1.0 means you pay less than $1 for a guaranteed $1 payout

Real-world findings from GamblingIsAllYouNeed:
  - Jazz vs Kings moneyline: paid 0.42 + 0.52 = 0.94 → resolved to $1.00 → 6.4% guaranteed ROI
  - Spread: Kings (-3.5): paid 0.41 + 0.51 = 0.92 → ~8.7% guaranteed ROI
  - Opportunities are brief (intraday, near game start) — requires real-time scanning

Key constraints:
  - Both legs must be fillable at the detected price (use limit orders, not market orders)
  - Polymarket CLOB shows MID price — actual fill price may be slightly higher
  - Effective threshold should include slippage: target sum < 0.97 (3% buffer)
  - Only binary markets (YES/NO or Team A/Team B)
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .base import (
    StrategyPlugin,
    StrategySignal,
    StrategyEntryDecision,
    StrategyManageState,
    StrategyExitDecision,
    RESOLUTION_WIN,
    RESOLUTION_LOSS,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULTS: Dict[str, Any] = {
    "bet_size_usdc": 10.0,           # per leg (total 2x this)
    "max_concurrent_arb_pairs": 5,    # max simultaneous arb pairs
    "max_price_sum": 0.97,            # only arb when YES + NO < this (3% slippage buffer)
    "min_volume_24h": 500.0,          # minimum liquidity
    "max_hours_to_end": 24,           # only sports events within 24h
    "min_hours_to_end": 0.1,          # at least 6 min remaining
    # Sports categories to scan (arb most common in sports, not politics)
    "category_keywords": ["vs.", "vs ", "spread", "o/u", "moneyline", "1h", "quarter"],
}


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _parse_params(strategy: Dict[str, Any]) -> Dict[str, Any]:
    params = strategy.get("parameters") or {}
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except (json.JSONDecodeError, TypeError):
            return {}
    return params if isinstance(params, dict) else {}


def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _fetch_both_prices(condition_id: str) -> Optional[Dict[str, float]]:
    """Fetch live prices for ALL outcomes of a market.
    Returns dict {outcome_lower: price} or None on failure."""
    try:
        import requests
        r = requests.get(
            f"https://clob.polymarket.com/markets/{condition_id}",
            timeout=8,
        )
        if r.ok:
            tokens = r.json().get("tokens", [])
            if len(tokens) >= 2:
                return {
                    t.get("outcome", "").lower(): max(0.0, min(1.0, float(t.get("price", 0))))
                    for t in tokens
                    if t.get("outcome")
                }
    except Exception:
        logger.debug("_fetch_both_prices failed for %s", condition_id, exc_info=True)
    return None


def _is_sports_market(title: str, keywords: List[str]) -> bool:
    title_lower = title.lower()
    return any(kw in title_lower for kw in keywords)


class CrossArbV1Strategy(StrategyPlugin):
    """Buy both sides of a binary market when YES+NO prices sum to < 0.97.

    Guarantees a profit at resolution regardless of outcome.
    Replicates GamblingIsAllYouNeed strategy ($4.4M PnL, 100% WR).
    """

    plugin_id = "cross_arb_v1"
    display_name = "Cross-Market Arb v1"
    description = (
        "Scans binary sports markets for YES+NO price sum < 0.97 and enters both legs "
        "for a guaranteed ~3%+ ROI at resolution. Replicates GamblingIsAllYouNeed strategy."
    )
    strategy_family = "cross_arb"
    required_feeds = ("polymarket",)
    supported_execution_modes = ("paper",)
    default_loop_seconds = 60   # scan every 60s — needs to be fast to catch windows

    def validate_parameters(self, parameters: dict) -> List[str]:
        errors = []
        threshold = parameters.get("max_price_sum")
        if threshold is not None:
            try:
                t = float(threshold)
                if t >= 1.0 or t <= 0.5:
                    errors.append("max_price_sum must be between 0.5 and 1.0")
            except (TypeError, ValueError):
                errors.append("max_price_sum must be numeric")
        return errors

    def replay_adapter(self):
        return None  # v1: no replay — requires real-time price data at detection time

    def discover(
        self,
        conn,
        strategy: Dict[str, Any],
    ) -> List[StrategySignal]:
        """Scan active sports markets for YES+NO < max_price_sum arb opportunities."""
        import requests

        params = _parse_params(strategy)
        max_sum = _to_float(params.get("max_price_sum", DEFAULTS["max_price_sum"]))
        min_vol = _to_float(params.get("min_volume_24h", DEFAULTS["min_volume_24h"]))
        max_hours = _to_float(params.get("max_hours_to_end", DEFAULTS["max_hours_to_end"]))
        min_hours = _to_float(params.get("min_hours_to_end", DEFAULTS["min_hours_to_end"]))
        cat_keywords = params.get("category_keywords", DEFAULTS["category_keywords"])
        now = datetime.now(timezone.utc)

        # Fetch active markets — focus on sports (where arb most common)
        try:
            r = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": 500,
                    "order": "volume24hr",
                    "ascending": "false",
                },
                timeout=15,
            )
            if not r.ok:
                logger.warning("Gamma API returned %d", r.status_code)
                return []
            markets = r.json()
        except Exception:
            logger.warning("Gamma API fetch failed", exc_info=True)
            return []

        signals: List[StrategySignal] = []
        checked = 0
        arb_found = 0

        for market in markets:
            title = str(market.get("question") or market.get("title") or "")
            if not title:
                continue

            # Sports filter
            if not _is_sports_market(title, cat_keywords):
                continue

            # Volume filter
            vol = _to_float(market.get("volume24hr") or market.get("volume"))
            if vol < min_vol:
                continue

            # Time filter
            end_date_str = market.get("endDate") or market.get("end_date_iso") or ""
            hours_to_end = None
            if end_date_str:
                try:
                    end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                    hours_to_end = (end_dt - now).total_seconds() / 3600
                    if hours_to_end < min_hours or hours_to_end > max_hours:
                        continue
                except (ValueError, TypeError):
                    pass

            # Binary only
            outcomes_raw = market.get("outcomes")
            if isinstance(outcomes_raw, str):
                try:
                    outcomes_raw = json.loads(outcomes_raw)
                except Exception:
                    outcomes_raw = []
            if not outcomes_raw or len(outcomes_raw) != 2:
                continue

            condition_id = market.get("conditionId") or market.get("condition_id", "")
            if not condition_id:
                continue

            checked += 1

            # Quick price sum check using gamma outcomePrices first (faster)
            outcome_prices_raw = market.get("outcomePrices")
            if isinstance(outcome_prices_raw, str):
                try:
                    outcome_prices_raw = json.loads(outcome_prices_raw)
                except Exception:
                    outcome_prices_raw = None

            quick_sum = None
            if isinstance(outcome_prices_raw, list) and len(outcome_prices_raw) == 2:
                try:
                    quick_sum = sum(float(p) for p in outcome_prices_raw)
                except Exception:
                    pass

            # Quick check: only hit CLOB if gamma prices look promising
            if quick_sum is not None and quick_sum >= max_sum + 0.01:
                continue  # clearly no arb

            # Fetch live CLOB prices for accuracy
            prices = _fetch_both_prices(condition_id)
            if not prices or len(prices) < 2:
                continue

            price_sum = sum(prices.values())
            if price_sum >= max_sum:
                continue

            # ARB FOUND!
            arb_found += 1
            guaranteed_roi = (1.0 - price_sum) / price_sum * 100

            logger.info(
                "ARB DETECTED: %s | sum=%.4f | guaranteed ROI=%.1f%% | prices=%s",
                title[:60], price_sum, guaranteed_roi, prices,
            )

            # Create signals for BOTH legs
            for outcome_label, outcome_price in prices.items():
                signals.append(StrategySignal(
                    wallet=f"arb_{condition_id[:8]}",
                    condition_id=condition_id,
                    outcome=outcome_label.capitalize(),
                    title=f"[ARB LEG] {title}",
                    current_price=outcome_price,
                    position_value=0.0,
                    analyzed_ts=_now_ts(),
                    metadata={
                        "price_sum": round(price_sum, 4),
                        "guaranteed_roi_pct": round(guaranteed_roi, 2),
                        "all_prices": prices,
                        "volume_24h": vol,
                        "hours_to_end": round(hours_to_end, 2) if hours_to_end else None,
                        "arb_pair_id": f"{condition_id[:12]}",
                        "is_arb_leg": True,
                        "strategy": "cross_arb_v1",
                        "source": "GamblingIsAllYouNeed_replication",
                    },
                ))

        logger.info(
            "cross_arb discover: %d markets scanned, %d arb opportunities found (%d signals)",
            checked, arb_found, len(signals),
        )
        return signals

    def enter(
        self,
        conn,
        strategy: Dict[str, Any],
        signal: StrategySignal,
        open_positions: List[Dict[str, Any]],
    ) -> StrategyEntryDecision:
        params = _parse_params(strategy)
        meta = signal.metadata or {}

        # Duplicate guard (check both legs)
        for pos in open_positions:
            if pos.get("condition_id") == signal.condition_id and pos.get("outcome", "").lower() == signal.outcome.lower():
                return StrategyEntryDecision(should_enter=False, reason="Duplicate arb leg")

        # Max concurrent arb pairs
        max_pairs = int(_to_float(params.get("max_concurrent_arb_pairs", DEFAULTS["max_concurrent_arb_pairs"])))
        # Count unique condition_ids in open positions for this strategy
        open_arb_cids = set(
            p.get("condition_id") for p in open_positions
            if "arb" in str(p.get("signal_type", "")).lower()
        )
        if len(open_arb_cids) >= max_pairs:
            return StrategyEntryDecision(should_enter=False, reason=f"Max arb pairs ({max_pairs}) reached")

        # Re-validate price sum at entry time (arb window may have closed)
        max_sum = _to_float(params.get("max_price_sum", DEFAULTS["max_price_sum"]))
        prices_live = _fetch_both_prices(signal.condition_id)
        if prices_live:
            live_sum = sum(prices_live.values())
            if live_sum >= max_sum:
                return StrategyEntryDecision(
                    should_enter=False,
                    reason=f"Arb window closed (live sum={live_sum:.4f} >= {max_sum})",
                )
            live_price = prices_live.get(signal.outcome.lower(), signal.current_price)
        else:
            live_price = signal.current_price

        bet_size = _to_float(params.get("bet_size_usdc", DEFAULTS["bet_size_usdc"]))
        price_sum = _to_float(meta.get("price_sum", 0))
        roi = _to_float(meta.get("guaranteed_roi_pct", 0))

        return StrategyEntryDecision(
            should_enter=True,
            reason=f"ARB leg {signal.outcome}@{live_price:.3f} | sum={price_sum:.3f} | guaranteed ROI={roi:.1f}%",
            size_usdc=bet_size,
            entry_price=live_price,
            execution_mode="paper",
            metadata=meta,
        )

    def manage(
        self,
        conn,
        strategy: Dict[str, Any],
        position: Dict[str, Any],
    ) -> StrategyManageState:
        from sniper.markets.polymarket_api import fetch_live_clob_price
        cond_id = position.get("condition_id", "")
        outcome = position.get("outcome", "")
        live_price = fetch_live_clob_price(cond_id, outcome)
        entry_price = _to_float(position.get("entry_price") or position.get("avg_entry_price"))
        current_price = live_price if live_price is not None else entry_price

        market_open = True
        try:
            row = conn.execute(
                "SELECT is_open FROM market_metadata WHERE condition_id = ?", (cond_id,)
            ).fetchone()
            if row:
                market_open = bool(int(row["is_open"] or 0))
        except Exception:
            pass

        return StrategyManageState(
            current_price=current_price,
            signal_open=market_open,
        )

    def exit(
        self,
        conn,
        strategy: Dict[str, Any],
        position: Dict[str, Any],
        state: StrategyManageState,
    ) -> StrategyExitDecision:
        current_price = state.current_price

        # Resolution
        if current_price is not None:
            if current_price >= RESOLUTION_WIN:
                return StrategyExitDecision(
                    should_exit=True, reason="market_resolved_win",
                    exit_price=current_price, status="closed",
                )
            if current_price <= RESOLUTION_LOSS:
                return StrategyExitDecision(
                    should_exit=True, reason="market_resolved_loss",
                    exit_price=current_price, status="closed",
                )

        if not state.signal_open and current_price is not None and RESOLUTION_LOSS < current_price < RESOLUTION_WIN:
            return StrategyExitDecision(
                should_exit=True, reason="market_closed",
                exit_price=current_price, status="closed",
            )

        return StrategyExitDecision(should_exit=False, reason="hold")
