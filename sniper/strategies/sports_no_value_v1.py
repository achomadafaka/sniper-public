"""Sports NO-value v1 strategy plugin.

Replicates the geniusMC strategy (clarity score 90/100, $2.4M PnL, 34 trades, 100% WR):
  - Scan Polymarket "Will [TEAM] win on [date]?" binary markets
  - Buy NO when YES price is in the configured band (default: YES between 0.10 and 0.35)
  - Theory: markets correctly price low-probability win events; collect near-certain NO outcomes

Why this works:
  - Soccer/sports teams playing away or vs strong opponents often have 10-25% win probability
  - These NO outcomes (~75-90¢) resolve correctly most of the time
  - geniusMC runs this on UCL / major soccer systematically — avg NO price 0.841, 100% WR

NOT a contrarian bet — this is going WITH the market on near-certain outcomes.
"""
from __future__ import annotations

import json
import logging
import os
import re
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
from sniper.filters import kelly_size as _kelly_size  # shared with political_no_value_v1
from sniper.settings.paper import PAPER_BANKROLL_USDC

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULTS: Dict[str, Any] = {
    "bet_size_usdc": 10.0,
    "min_bet_size_usdc": 10.0,
    "max_bet_size_usdc": 50.0,
    "kelly_fraction": 0.10,
    "estimated_win_prob": 0.65,  # calibrated 2026-03-21: actual WR=63.6% (7/11), was 0.85
    "max_concurrent_positions": 10,
    "max_exposure_per_market_usdc": 50.0,
    # YES price band: only enter if YES is in [min_yes, max_yes]
    # i.e. the team has a low-but-not-trivially-low win probability
    "min_yes_price": 0.05,   # ignore if YES < 5¢ (already near-certain NO)
    "max_yes_price": 0.35,   # ignore if YES > 35¢ (team has real chance to win)
    # Minimum liquidity (market volume proxy)
    "min_volume_24h": 100.0,
    # Only enter with enough time before resolution — entry filter only
    "max_hours_to_end": 72,
    "min_hours_to_end": 0.5,
    # Max hold before forcing a staleness exit (separate from max_hours_to_end entry filter)
    # Sports: give 4 days past entry in case match is delayed or runs long
    "max_hold_hours": 96,
    # Market title filter — regex patterns for "Will X win" type markets
    "title_patterns": [
        r"Will .+ win on \d{4}-\d{2}-\d{2}",
        r"Will .+ win the .+ match",
        r"Will .+ win\?",
    ],
    # Sport category allowlist (empty = all sports)
    "allowed_categories": ["soccer", "football", "champions league", "la liga",
                           "premier league", "nba", "nfl", "nhl", "baseball"],
    # Structural NO bet — news spike is noise, don't cascade exit
    "cascade_exit_mode": "hold_through",
    # Stop-loss: exit if price moves this far against us from entry (0 = disabled)
    "stop_loss_pct": 0.0,
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


def _title_matches(title: str, patterns: List[str]) -> bool:
    """Return True if the title matches any of the configured patterns."""
    title_lower = title.lower()
    for pat in patterns:
        try:
            if re.search(pat, title, re.IGNORECASE):
                return True
        except re.error:
            if pat.lower() in title_lower:
                return True
    return False


def _category_matches(market: Dict, allowed: List[str]) -> bool:
    """Return True if the market belongs to an allowed sport/category.

    If *allowed* is empty, all categories pass (allow-all semantics).
    Checks ``category``, ``categories``, ``tags``, ``sport``, and ``league``
    fields from the Gamma API response, plus the market title as a fallback.
    Matching is case-insensitive substring search.
    """
    if not allowed:
        return True

    # Collect all category-bearing text from the market object.
    candidates: List[str] = []
    for field in ("category", "categories", "tags", "sport", "league"):
        val = market.get(field)
        if isinstance(val, str):
            candidates.append(val.lower())
        elif isinstance(val, list):
            candidates.extend(str(v).lower() for v in val)

    # Title as fallback — sport keywords often appear in "Will Arsenal win?" titles.
    title = str(market.get("question") or market.get("title") or "").lower()
    if title:
        candidates.append(title)

    allowed_lower = [c.lower() for c in allowed]
    return any(
        allowed_cat in candidate
        for allowed_cat in allowed_lower
        for candidate in candidates
    )


# _kelly_size imported from filters.kelly_size above


def _fetch_all_prices_for_market(condition_id: str) -> Dict[str, float]:
    """Fetch YES and NO prices for a binary market from CLOB API."""
    try:
        import requests
        r = requests.get(
            f"https://clob.polymarket.com/markets/{condition_id}",
            timeout=8,
        )
        if r.ok:
            tokens = r.json().get("tokens", [])
            return {
                t.get("outcome", "").lower(): max(0.0, min(1.0, float(t.get("price", 0))))
                for t in tokens
                if t.get("outcome") and t.get("price") is not None
            }
    except Exception:
        logger.debug("_fetch_all_prices_for_market failed for %s", condition_id, exc_info=True)
    return {}


class SportsNoValueV1Strategy(StrategyPlugin):
    """Buy NO on sports markets where YES (win probability) is 5-35%.

    Replicates geniusMC strategy: systematically collect near-certain NO outcomes
    on soccer/sports "Will X win?" binary markets.
    """

    plugin_id = "sports_no_value_v1"
    display_name = "Sports NO Value v1"
    description = (
        "Buys NO on 'Will X win?' sports markets when YES price is in [5%, 35%]. "
        "Replicates geniusMC strategy ($2.4M PnL, clarity 90/100)."
    )
    strategy_family = "sports_no_value"
    required_feeds = ("polymarket",)
    supported_execution_modes = ("paper",)
    default_loop_seconds = 300

    def validate_parameters(self, parameters: dict) -> List[str]:
        errors = []
        for key in ("min_yes_price", "max_yes_price", "bet_size_usdc", "min_volume_24h"):
            val = parameters.get(key)
            if val is not None:
                try:
                    float(val)
                except (TypeError, ValueError):
                    errors.append(f"{key} must be numeric")
        min_yes = _to_float(parameters.get("min_yes_price", DEFAULTS["min_yes_price"]))
        max_yes = _to_float(parameters.get("max_yes_price", DEFAULTS["max_yes_price"]))
        if min_yes >= max_yes:
            errors.append("min_yes_price must be < max_yes_price")
        return errors

    def replay_adapter(self):
        return None  # v1: no replay until historical signal data backfilled

    def discover(
        self,
        conn,
        strategy: Dict[str, Any],
    ) -> List[StrategySignal]:
        """Find sports NO signals from active Polymarket markets."""
        import requests

        params = _parse_params(strategy)
        min_yes = _to_float(params.get("min_yes_price", DEFAULTS["min_yes_price"]))
        max_yes = _to_float(params.get("max_yes_price", DEFAULTS["max_yes_price"]))
        min_vol = _to_float(params.get("min_volume_24h", DEFAULTS["min_volume_24h"]))
        max_hours = _to_float(params.get("max_hours_to_end", DEFAULTS["max_hours_to_end"]))
        min_hours = _to_float(params.get("min_hours_to_end", DEFAULTS["min_hours_to_end"]))
        title_patterns = params.get("title_patterns", DEFAULTS["title_patterns"])
        allowed_categories = params.get("allowed_categories", DEFAULTS["allowed_categories"])
        if isinstance(allowed_categories, str):
            allowed_categories = [allowed_categories]
        elif not isinstance(allowed_categories, list):
            allowed_categories = list(DEFAULTS["allowed_categories"])

        signals: List[StrategySignal] = []
        now = datetime.now(timezone.utc)

        # Fetch active sports markets from Gamma API
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

        checked = 0
        matched = 0

        for market in markets:
            title = str(market.get("question") or market.get("title") or "")
            if not title:
                continue

            # Filter: must match "Will X win?" pattern
            if not _title_matches(title, title_patterns):
                continue

            # Filter: allowed sport categories (checks category/tags/title fields)
            if not _category_matches(market, allowed_categories):
                continue

            # Filter: binary only (YES/NO outcomes)
            outcomes_raw = market.get("outcomes")
            if isinstance(outcomes_raw, str):
                try:
                    outcomes_raw = json.loads(outcomes_raw)
                except Exception:
                    outcomes_raw = []
            outcomes = [str(o).lower() for o in (outcomes_raw or [])]
            if set(outcomes) != {"yes", "no"}:
                continue

            # Filter: volume
            vol = _to_float(market.get("volume24hr") or market.get("volume"))
            if vol < min_vol:
                continue

            # Filter: time to end
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

            condition_id = market.get("conditionId") or market.get("condition_id", "")
            if not condition_id:
                continue

            # Fetch live YES price
            yes_price = _to_float(market.get("outcomePrices", [None])[0])
            if yes_price <= 0:
                # Try CLOB live prices
                prices = _fetch_all_prices_for_market(condition_id)
                yes_price = prices.get("yes", 0.0)

            if yes_price <= 0:
                continue

            checked += 1

            # Core signal gate: YES in [min_yes, max_yes]
            if not (min_yes <= yes_price <= max_yes):
                continue

            no_price = 1.0 - yes_price  # approximate; confirmed via CLOB if needed
            prices_live = _fetch_all_prices_for_market(condition_id)
            if prices_live:
                no_price = prices_live.get("no", no_price)
                yes_price = prices_live.get("yes", yes_price)
                # Re-check gate with live price
                if not (min_yes <= yes_price <= max_yes):
                    continue

            matched += 1
            signals.append(StrategySignal(
                wallet=f"no_value_{condition_id[:8]}",
                condition_id=condition_id,
                outcome="No",
                title=title,
                current_price=no_price,
                position_value=0.0,
                analyzed_ts=_now_ts(),
                metadata={
                    "yes_price": round(yes_price, 4),
                    "no_price": round(no_price, 4),
                    "volume_24h": vol,
                    "hours_to_end": round(hours_to_end, 2) if hours_to_end is not None else None,
                    "end_date_iso": end_date_str,
                    "strategy": "sports_no_value_v1",
                    "source": "geniusMC_replication",
                },
            ))

        logger.info(
            "sports_no_value discover: %d markets checked, %d matched title pattern, %d signals",
            len(markets), checked, matched,
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

        # Duplicate guard
        for pos in open_positions:
            if pos.get("condition_id") == signal.condition_id and pos.get("outcome") == signal.outcome:
                return StrategyEntryDecision(should_enter=False, reason="Duplicate open position")

        # Max concurrent
        max_conc = int(_to_float(params.get("max_concurrent_positions", DEFAULTS["max_concurrent_positions"])))
        if len(open_positions) >= max_conc:
            return StrategyEntryDecision(should_enter=False, reason=f"Max concurrent {max_conc} reached")

        # Max exposure per market
        max_mkt = _to_float(params.get("max_exposure_per_market_usdc", DEFAULTS["max_exposure_per_market_usdc"]))
        mkt_exp = sum(_to_float(p.get("size_usdc", 0)) for p in open_positions if p.get("condition_id") == signal.condition_id)

        # Kelly-derived bet size
        yes_price_kelly = _to_float(meta.get("yes_price", 0))
        _kelly_frac = _to_float(params.get("kelly_fraction", DEFAULTS["kelly_fraction"]))
        _win_prob = _to_float(params.get("estimated_win_prob", DEFAULTS["estimated_win_prob"]))
        _min_bet = _to_float(params.get("min_bet_size_usdc", DEFAULTS["min_bet_size_usdc"]))
        _max_bet = _to_float(params.get("max_bet_size_usdc", DEFAULTS["max_bet_size_usdc"]))
        _bankroll = PAPER_BANKROLL_USDC
        bet_size = _kelly_size(
            yes_price=yes_price_kelly,
            estimated_win_prob=_win_prob,
            bankroll=_bankroll,
            kelly_fraction=_kelly_frac,
            min_size=_min_bet,
            max_size=_max_bet,
        )
        if bet_size <= 0:
            return StrategyEntryDecision(should_enter=False, reason=f"Kelly size=0 (no edge at YES={yes_price_kelly:.3f})")

        if mkt_exp + bet_size > max_mkt:
            return StrategyEntryDecision(should_enter=False, reason=f"Market exposure would exceed ${max_mkt}")

        # Re-validate YES price at entry time (live)
        min_yes = _to_float(params.get("min_yes_price", DEFAULTS["min_yes_price"]))
        max_yes = _to_float(params.get("max_yes_price", DEFAULTS["max_yes_price"]))
        yes_price = _to_float(meta.get("yes_price", 0))
        if not (min_yes <= yes_price <= max_yes):
            return StrategyEntryDecision(
                should_enter=False,
                reason=f"YES price {yes_price:.3f} outside gate [{min_yes},{max_yes}]"
            )

        # Gate: no open position in paper_trades (cross-system dedup)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT 1 FROM paper_trades WHERE condition_id=? AND LOWER(outcome)=LOWER(?) AND status='open' AND duplicate_of IS NULL LIMIT 1",
                (signal.condition_id, signal.outcome)
            )
            if cur.fetchone():
                return StrategyEntryDecision(should_enter=False, reason="paper_trades already open on this market/outcome")
        except Exception:
            logger.debug("sports_no_value_v1 enter: paper_trades cross-dedup query failed", exc_info=True)

        no_price = signal.current_price
        vol = _to_float(meta.get("volume_24h", 0))

        return StrategyEntryDecision(
            should_enter=True,
            reason=f"NO@{no_price:.3f} (YES={yes_price:.3f}, vol=${vol:.0f}) — near-certain NO",
            size_usdc=bet_size,
            entry_price=no_price,
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
        outcome = position.get("outcome", "No")
        live_price = fetch_live_clob_price(cond_id, outcome)
        entry_price = _to_float(position.get("entry_price") or position.get("avg_entry_price"))
        if live_price is None:
            logger.debug("manage: no live price for %s (%s) — holding at entry %.3f",
                         cond_id[:12], outcome, entry_price)
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
            metadata={"live_price_available": live_price is not None},
        )

    def exit(
        self,
        conn,
        strategy: Dict[str, Any],
        position: Dict[str, Any],
        state: StrategyManageState,
    ) -> StrategyExitDecision:
        params = _parse_params(strategy)
        cascade_active = bool(position.get("cascade_active", False))
        cascade_mode = params.get("cascade_exit_mode", DEFAULTS["cascade_exit_mode"])
        current_price = state.current_price
        entry_price = _to_float(position.get("avg_entry_price") or position.get("entry_price") or 0.0)

        # Resolution exits always fire
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

        # Stop-loss — fires even in cascade mode (catastrophic loss protection)
        stop_loss_pct = _to_float(params.get("stop_loss_pct", DEFAULTS.get("stop_loss_pct", 0.0)))
        if stop_loss_pct > 0 and entry_price > 0 and current_price is not None:
            stop_level = entry_price * (1.0 - stop_loss_pct)
            if current_price <= stop_level:
                return StrategyExitDecision(
                    should_exit=True,
                    reason=f"stop_loss: price {current_price:.4f} <= stop {stop_level:.4f} ({stop_loss_pct:.0%} from entry {entry_price:.4f})",
                    exit_price=current_price,
                    status="closed",
                )

        # Cascade: hold_through — structural NO bet, news spike is noise
        if cascade_active and cascade_mode == "hold_through":
            logger.info(
                "sports_no_value_v1: cascade_active, cascade_mode=hold_through — holding for %s",
                position.get("condition_id", ""),
            )
            return StrategyExitDecision(should_exit=False, reason="hold_cascade")

        # Market closed but unresolved
        if not state.signal_open and current_price is not None and RESOLUTION_LOSS < current_price < RESOLUTION_WIN:
            return StrategyExitDecision(
                should_exit=True, reason="market_closed",
                exit_price=current_price, status="closed",
            )

        # Staleness exit — uses max_hold_hours, NOT max_hours_to_end (which is the entry-time filter)
        max_hold = _to_float(params.get("max_hold_hours", DEFAULTS["max_hold_hours"]))
        opened_ts = int(position.get("opened_ts", 0) or 0)
        if opened_ts > 0 and max_hold > 0:
            age_hours = (time.time() - opened_ts) / 3600
            if age_hours > max_hold:
                return StrategyExitDecision(
                    should_exit=True,
                    reason=f"stale: held {age_hours:.0f}h > {max_hold:.0f}h max",
                    exit_price=current_price or 0.0,
                    status="closed",
                )

        return StrategyExitDecision(should_exit=False, reason="hold")
