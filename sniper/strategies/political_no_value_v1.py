"""Political NO-value v1 strategy plugin.

Replicates the YatSen strategy ($2.41M PnL, 582 predictions, clarity 94/100):
  - Scan Polymarket political/geopolitical binary markets for extreme events
  - Buy NO when YES price is in the configured band (default: YES between 0.10 and 0.50)
  - Theory: markets overestimate tail-risk political events; NO resolves correctly most of the time

Why this works:
  - "Will China invade Taiwan?" YES @ 0.50 → resolves NO → YatSen banked +$77K on one position
  - Political extremes (invasion, regime collapse, alien contact, nuclear use) are consistently
    overpriced on Polymarket due to retail fear/excitement premium
  - Long hold times (months) mean compounding of near-certain outcomes
  - YatSen avg NO price ~0.70, holds for 30-180 days — not a quick flip

NOT a contrarian bet on politics — this is going WITH the market on structurally
near-certain outcomes that happen to SOUND dramatic.
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
from sniper.filters import kelly_size as _kelly_size  # shared with sports_no_value_v1
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
    "estimated_win_prob": 0.88,
    "max_concurrent_positions": 15,
    "max_exposure_per_market_usdc": 100.0,
    # YES price band: enter if YES is in [min_yes, max_yes]
    # YatSen enters at YES 0.15–0.50 (dramatic events still "possible" but won't happen)
    "min_yes_price": 0.10,   # ignore if YES < 10¢ (market already knows it won't happen)
    "max_yes_price": 0.50,   # ignore if YES > 50¢ (too uncertain, real chance it happens)
    # Minimum liquidity
    "min_volume_24h": 50.0,
    # Time range — political markets run months; don't enter if resolving today
    # Gamma backtest (n=67): ≤90d events resolve NO at 74%, 91-365d collapses to 46%
    # because long-horizon events DO eventually happen given enough time (e.g. Trump indicted)
    "max_hours_to_end": 2160,    # 90 days max — entry filter only (Gamma-validated)
    "min_hours_to_end": 24,      # must have at least 24h left — entry filter only
    # Max hold before forcing a staleness exit (separate from max_hours_to_end entry filter)
    "max_hold_hours": 720,       # close after 30 days if market hasn't resolved
    # Stop-loss: exit if NO price drops this fraction below entry
    # These are structural long-duration bets so give room (20%) vs. sports (7%)
    "stop_loss_pct": 0.20,
    # Keyword patterns — extreme political/geopolitical events
    # These are things that SOUND scary but statistically don't happen.
    #
    # 2026-03-24 audit (25 closed, 80% WR, +$68):
    #   - Crypto FDV/Launch: 4/0 100% WR, +$34 ← best subcategory
    #   - Geopolitical extremes: 3/2 60% WR, +$31
    #   - Musk tweets: 10/2 83% WR, +$0.54 ← 12 trades for 54 cents (REMOVED)
    #   - Crude oil: 3/0 100% WR, +$3 ← wrong category for political NO (REMOVED)
    #
    # Removed tweet/mention patterns: these are ranged outcomes, not tail-risk.
    # One $33 Musk loss nearly zeroed 12 trades of effort.
    # Removed crude oil: commodity price during active war is not structural NO.
    "title_patterns": [
        # --- Geopolitical extremes (original YatSen patterns) ---
        r"Will .+ invade",
        r"Will .+ attack",
        r"Will .+ use nuclear",
        r"Will .+ collapse",
        r"Will .+ resign",
        r"Will .+ be (removed|ousted|overthrown|impeached|assassinated)",
        r"Will .+ (coup|regime change)",
        r"Will .+ annex",
        r"Will there be (a war|an invasion|a nuclear|a coup|a revolution)",
        r"alien|extraterrestrial|UFO|UAP",
        r"World War",
        r"Will Russia",
        r"Will China",
        r"Will North Korea",
        r"Will Iran",
        r"Will Israel",
        r"Will (the US|US|America) (invade|attack|bomb|strike)",
        r"Will .+ default on",
        r"Will .+ leave (the|NATO|EU|UN)",
        r"Will .+ (declare war|declare independence)",
        r"pandemic|outbreak",
        r"asteroid|meteor",
        # --- Crypto token/FDV launch (best subcategory: 100% WR, +$34) ---
        r"Will .+ launch a token",
        r"Will .+ launch .+ token",
        r"FDV above \$",
        # NOTE: tweet/mention/announce patterns REMOVED (2026-03-24)
        # — 12 trades for $0.54 net, one $33 loss. Not tail-risk events.
        # NOTE: crude oil patterns REMOVED (2026-03-24)
        # — commodity price bets during active war are not structural NO.
    ],
    # Category allowlist (empty = all categories)
    "allowed_categories": [
        "politics", "world", "geopolitics", "elections", "international",
        "global", "war", "conflict", "science", "space",
        "crypto", "business", "technology",  # for token launch + FDV markets
    ],
    # Block list — categories we never want
    "blocked_categories": [
        "sports", "soccer", "football", "nba", "nfl", "nhl", "baseball",
        "entertainment", "culture",  # tweet/mention markets live here
    ],
    # Structural NO bet — news spike is noise, don't cascade exit
    "cascade_exit_mode": "hold_through",
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
    """Return True if the title matches any configured pattern."""
    for pat in patterns:
        try:
            if re.search(pat, title, re.IGNORECASE):
                return True
        except re.error:
            if pat.lower() in title.lower():
                return True
    return False


def _category_allowed(
    category: str,
    allowed: List[str],
    blocked: List[str],
) -> bool:
    """Return True if category passes the allow/block filter.

    Polymarket often returns empty category for political/geopolitical markets
    (the title patterns are already specific enough to filter correctly).
    Empty category → only blocklist applies; allowlist is skipped.
    """
    cat_lower = (category or "").lower()
    # Blocked takes priority (even for empty category)
    for b in blocked:
        if b.lower() in cat_lower:
            return False
    # If category is empty, pass (title pattern is the primary filter)
    if not cat_lower:
        return True
    # If allowlist is empty, pass all (unless blocked above)
    if not allowed:
        return True
    for a in allowed:
        if a.lower() in cat_lower:
            return True
    return False


# _kelly_size imported from filters.kelly_size above


def _fetch_all_prices_for_market(condition_id: str) -> Dict[str, float]:
    """Fetch live YES and NO prices from CLOB API."""
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


class PoliticalNoValueV1Strategy(StrategyPlugin):
    """Buy NO on political/geopolitical markets where YES (event probability) is 10-50%.

    Replicates YatSen strategy: systematically collect near-certain NO outcomes
    on political extreme-event markets ("Will X invade Y?", "Will X collapse?", etc.).
    """

    plugin_id = "political_no_value_v1"
    display_name = "Political NO Value v1"
    description = (
        "Buys NO on political/geopolitical 'extreme event' markets when YES price is 10–50%. "
        "Replicates YatSen strategy ($2.41M PnL, 582 predictions)."
    )
    strategy_family = "political_no_value"
    required_feeds = ("polymarket",)
    supported_execution_modes = ("paper",)
    default_loop_seconds = 600  # check every 10 min (slower cadence, long-duration markets)

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
        return None

    def discover(
        self,
        conn,
        strategy: Dict[str, Any],
    ) -> List[StrategySignal]:
        """Find political NO signals from active Polymarket markets."""
        import requests

        params = _parse_params(strategy)
        min_yes = _to_float(params.get("min_yes_price", DEFAULTS["min_yes_price"]))
        max_yes = _to_float(params.get("max_yes_price", DEFAULTS["max_yes_price"]))
        min_vol = _to_float(params.get("min_volume_24h", DEFAULTS["min_volume_24h"]))
        max_hours = _to_float(params.get("max_hours_to_end", DEFAULTS["max_hours_to_end"]))
        min_hours = _to_float(params.get("min_hours_to_end", DEFAULTS["min_hours_to_end"]))
        title_patterns = params.get("title_patterns", DEFAULTS["title_patterns"])
        allowed_cats = params.get("allowed_categories", DEFAULTS["allowed_categories"])
        blocked_cats = params.get("blocked_categories", DEFAULTS["blocked_categories"])

        signals: List[StrategySignal] = []
        now = datetime.now(timezone.utc)

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

            # Filter: must match extreme political/geopolitical pattern
            if not _title_matches(title, title_patterns):
                continue

            # Filter: category allow/block
            category = str(market.get("category") or market.get("tags") or "")
            if not _category_allowed(category, allowed_cats, blocked_cats):
                continue

            # Filter: binary only
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
            hours_to_end: Optional[float] = None
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

            # Get YES price
            yes_price = _to_float(market.get("outcomePrices", [None])[0])
            if yes_price <= 0:
                prices = _fetch_all_prices_for_market(condition_id)
                yes_price = prices.get("yes", 0.0)

            if yes_price <= 0:
                continue

            checked += 1

            # Determine effective max_yes based on market type.
            # FDV/launch markets: 100% WR at full [0.10, 0.50] band — keep wide.
            # Geopolitical: losses cluster at YES > 0.35 — cap tighter.
            title_lower = title.lower()
            is_fdv_launch = bool(re.search(r"fdv|launch.+token|token.+launch", title_lower))
            effective_max_yes = max_yes if is_fdv_launch else min(max_yes, 0.35)

            # Core gate: YES in [min_yes, effective_max_yes]
            if not (min_yes <= yes_price <= effective_max_yes):
                continue

            # Confirm with live CLOB price
            no_price = 1.0 - yes_price
            prices_live = _fetch_all_prices_for_market(condition_id)
            if prices_live:
                no_price = prices_live.get("no", no_price)
                yes_price = prices_live.get("yes", yes_price)
                if not (min_yes <= yes_price <= effective_max_yes):
                    continue

            matched += 1
            market_type = "fdv_launch" if is_fdv_launch else "geopolitical"
            signals.append(StrategySignal(
                wallet=f"pol_no_{condition_id[:8]}",
                condition_id=condition_id,
                outcome="No",
                title=title,
                current_price=no_price,
                position_value=0.0,
                analyzed_ts=_now_ts(),
                metadata={
                    "yes_price": round(yes_price, 4),
                    "no_price": round(no_price, 4),
                    "effective_max_yes": round(effective_max_yes, 4),
                    "market_type": market_type,
                    "volume_24h": vol,
                    "hours_to_end": round(hours_to_end, 2) if hours_to_end is not None else None,
                    "end_date_iso": end_date_str,
                    "category": category,
                    "strategy": "political_no_value_v1",
                    "source": "YatSen_replication",
                },
            ))

        logger.info(
            "political_no_value discover: %d markets scanned, %d matched pattern, %d signals",
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
        yes_price = _to_float(meta.get("yes_price", 0))
        _kelly_frac = _to_float(params.get("kelly_fraction", DEFAULTS["kelly_fraction"]))
        _win_prob = _to_float(params.get("estimated_win_prob", DEFAULTS["estimated_win_prob"]))
        _min_bet = _to_float(params.get("min_bet_size_usdc", DEFAULTS["min_bet_size_usdc"]))
        _max_bet = _to_float(params.get("max_bet_size_usdc", DEFAULTS["max_bet_size_usdc"]))
        _bankroll = PAPER_BANKROLL_USDC
        bet_size = _kelly_size(
            yes_price=yes_price,
            estimated_win_prob=_win_prob,
            bankroll=_bankroll,
            kelly_fraction=_kelly_frac,
            min_size=_min_bet,
            max_size=_max_bet,
        )
        if bet_size <= 0:
            return StrategyEntryDecision(should_enter=False, reason=f"Kelly size=0 (no edge at YES={yes_price:.3f})")

        if mkt_exp + bet_size > max_mkt:
            return StrategyEntryDecision(should_enter=False, reason=f"Market exposure would exceed ${max_mkt}")

        # Re-validate YES price at entry
        min_yes = _to_float(params.get("min_yes_price", DEFAULTS["min_yes_price"]))
        max_yes = _to_float(params.get("max_yes_price", DEFAULTS["max_yes_price"]))
        yes_price = _to_float(meta.get("yes_price", 0))
        if not (min_yes <= yes_price <= max_yes):
            return StrategyEntryDecision(
                should_enter=False,
                reason=f"YES price {yes_price:.3f} outside gate [{min_yes},{max_yes}]",
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
            logger.debug("political_no_value_v1 enter: paper_trades cross-dedup query failed", exc_info=True)

        no_price = signal.current_price
        vol = _to_float(meta.get("volume_24h", 0))
        hours = meta.get("hours_to_end")
        days_str = f"{hours/24:.0f}d" if hours else "?"

        return StrategyEntryDecision(
            should_enter=True,
            reason=f"NO@{no_price:.3f} (YES={yes_price:.3f}, vol=${vol:.0f}, {days_str} left) — extreme event NO",
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
        entry_price = _to_float(position.get("entry_price") or position.get("avg_entry_price"))

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

        # Cascade: hold_through — structural NO bet, news spike is noise
        if cascade_active and cascade_mode == "hold_through":
            logger.info(
                "political_no_value_v1: cascade_active, cascade_mode=hold_through — holding for %s",
                position.get("condition_id", ""),
            )
            return StrategyExitDecision(should_exit=False, reason="hold_cascade")

        # Stop-loss — entry_price and current_price are both NO token prices
        stop_loss_pct = _to_float(params.get("stop_loss_pct", DEFAULTS["stop_loss_pct"]))
        if stop_loss_pct > 0 and current_price is not None and entry_price > 0:
            threshold = entry_price * (1.0 - stop_loss_pct)
            if current_price < threshold:
                logger.info(
                    "political_no_value_v1: stop_loss triggered — NO %.4f < threshold %.4f "
                    "(entry=%.4f, stop_loss_pct=%.2f) for %s",
                    current_price, threshold, entry_price, stop_loss_pct,
                    position.get("condition_id", ""),
                )
                return StrategyExitDecision(
                    should_exit=True, reason="stop_loss",
                    exit_price=current_price, status="closed",
                )

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
