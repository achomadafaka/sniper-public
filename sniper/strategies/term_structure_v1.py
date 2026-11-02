"""Term Structure v1 strategy plugin.

Scans Polymarket for markets on the same topic across different time horizons
and identifies mispriced curves. When near-term dates are overpriced relative
to long-term dates, buys NO on the overpriced short-term market.

Why this works:
  - "Iran ceasefire by March 31?" at 13.5% and "by December 31?" at 73.5%
    → if March passes without ceasefire, the March contract resolves NO (profit)
    → the Dec contract barely moves (it was already pricing the long horizon)
  - Front-loaded curves (high marginal probability in the nearest window)
    overestimate imminent events due to recency bias and news hype
  - Each day that passes without the event, short-date NO becomes more valuable

Approach:
  1. Fetch open markets from Gamma API
  2. Group by topic (strip date suffixes from titles)
  3. Compute the implied probability curve
  4. Identify near-term overpricing: where YES > expected given the curve shape
  5. Buy NO on the overpriced near-term date
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sniper.filters import days_to_resolution
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

DEFAULTS: Dict[str, Any] = {
    "bet_size_usdc": 10.0,
    "max_concurrent_positions": 10,
    "max_exposure_per_market_usdc": 50.0,
    # Only enter NO when YES price is in this band
    # Higher YES = more overpriced = better NO entry
    "min_yes_price": 0.05,   # ignore if YES < 5% (already near-certain NO)
    "max_yes_price": 0.40,   # ignore if YES > 40% (event might actually happen)
    # Minimum spread between this date and the next-longer date
    # If marginal probability per day is > this threshold, the near-term is overpriced
    "min_marginal_daily_pct": 0.003,  # 0.3% per day marginal probability
    # Minimum group size (need at least 3 date variants to detect curve shape)
    "min_group_size": 3,
    # Volume filter
    "min_volume_24h": 500.0,
    # Time range
    "min_hours_to_end": 12,
    "max_hours_to_end": 2160,  # 90 days
    # Gamma API fetch limit
    "gamma_fetch_limit": 2000,
    "resolution_stop_cutoff_days": 3,    # don't fire stop-loss within this many days of resolution
    "cascade_exit_mode": "hold_through", # structural arb, not news-sensitive
}

# Date patterns to strip from titles for grouping
DATE_STRIP_PATTERNS = [
    r"\b(by |before |on |in )(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2}(,?\s*\d{4})?\??",
    r"\b(by |before |on |in )(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+\d{1,2}\??",
    r"\b(by |before |in )(march|april|may|june|july|august|september|october|november|december)\s+\d{4}\??",
    r"\b(by |before )(end of )?(march|april|may|june|2026|2027)\??",
    r"\b(before |by )\d{4}\??",
    r"\b(by |before )(q[1-4])\s+\d{4}\??",
    r"\bon 2026-\d{2}-\d{2}\??",
]


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


def _strip_date(title: str) -> str:
    """Strip date suffix from title to get the core topic."""
    result = title.lower().strip().rstrip("?").strip()
    for pat in DATE_STRIP_PATTERNS:
        result = re.sub(pat, "", result, flags=re.IGNORECASE).strip()
    # Clean up trailing whitespace and punctuation
    result = re.sub(r"\s+", " ", result).strip().rstrip(",").strip()
    return result


def _fetch_yes_price(condition_id: str) -> Optional[float]:
    """Fetch live YES price from CLOB API."""
    try:
        import requests
        r = requests.get(
            f"https://clob.polymarket.com/markets/{condition_id}",
            timeout=8,
        )
        if r.ok:
            for t in r.json().get("tokens", []):
                if t.get("outcome", "").upper() == "YES":
                    return max(0.0, min(1.0, float(t.get("price", 0))))
    except Exception:
        pass
    return None


def _fetch_gamma_markets(limit: int = 2000) -> List[Dict]:
    """Fetch open markets from Gamma API."""
    import requests
    all_markets = []
    for offset in range(0, limit, 100):
        try:
            r = requests.get(
                f"https://gamma-api.polymarket.com/markets?closed=false&limit=100&offset={offset}",
                timeout=10,
            )
            data = r.json()
            if not data:
                break
            all_markets.extend(data)
        except Exception:
            break
    return all_markets


class TermStructureV1Strategy(StrategyPlugin):
    """Buy NO on overpriced near-term dates in multi-date market groups.

    Scans for same-topic markets across different time horizons, computes
    the implied probability curve, and enters NO when near-term YES is
    overpriced relative to the curve.
    """

    plugin_id = "term_structure_v1"
    display_name = "Term Structure v1"
    description = (
        "Buys NO on overpriced near-term dates when the same market exists "
        "across multiple time horizons. Exploits front-loaded probability curves."
    )
    strategy_family = "term_structure"
    required_feeds = ("polymarket",)
    supported_execution_modes = ("paper",)
    default_loop_seconds = 3600  # 1 hour (markets move slowly)

    def discover(self, conn, strategy) -> List[StrategySignal]:
        params = _parse_params(strategy)
        min_yes = float(params.get("min_yes_price", DEFAULTS["min_yes_price"]))
        max_yes = float(params.get("max_yes_price", DEFAULTS["max_yes_price"]))
        min_marginal = float(params.get("min_marginal_daily_pct", DEFAULTS["min_marginal_daily_pct"]))
        min_group = int(params.get("min_group_size", DEFAULTS["min_group_size"]))
        min_vol = float(params.get("min_volume_24h", DEFAULTS["min_volume_24h"]))
        min_hours = float(params.get("min_hours_to_end", DEFAULTS["min_hours_to_end"]))
        max_hours = float(params.get("max_hours_to_end", DEFAULTS["max_hours_to_end"]))

        # 1. Use LOCAL market_metadata (has correct per-market end dates)
        #    Gamma API endDate is the event container date, not per-market resolution.
        from collections import defaultdict
        rows = conn.execute("""
            SELECT condition_id, title, end_date_iso, volume_24h
            FROM market_metadata
            WHERE is_open = 1 AND end_date_iso > date('now')
              AND volume_24h >= ?
        """, (min_vol,)).fetchall()

        logger.info("term_structure discover: %d open markets in local DB (vol >= %.0f)", len(rows), min_vol)

        # 2. Group by topic (strip dates from titles)
        groups: Dict[str, List[Dict]] = defaultdict(list)
        for r in rows:
            title = r["title"] or ""
            core = _strip_date(title)
            if len(core) > 15 and core != title.lower().strip().rstrip("?").strip():
                groups[core].append({
                    "condition_id": r["condition_id"],
                    "title": title,
                    "end_date": (r["end_date_iso"] or "")[:10],
                    "volume_24h": float(r["volume_24h"] or 0),
                })

        valid_groups = {k: v for k, v in groups.items() if len(v) >= min_group}
        logger.info("term_structure discover: %d groups with %d+ date variants", len(valid_groups), min_group)

        # 3. Build curves and find signals
        signals: List[StrategySignal] = []
        now = datetime.now(timezone.utc)

        for topic, mkts in valid_groups.items():
            sorted_mkts = sorted(mkts, key=lambda x: x["end_date"])

            # Build curve with live YES prices
            curve = []
            for m in sorted_mkts:
                end_str = m["end_date"]
                if not end_str:
                    continue
                try:
                    end_dt = datetime.fromisoformat(end_str + "T23:59:59+00:00")
                except (ValueError, TypeError):
                    continue

                hours_left = (end_dt - now).total_seconds() / 3600
                if hours_left < min_hours or hours_left > max_hours:
                    continue

                # Get live YES price
                yes_price = _fetch_yes_price(m["condition_id"])
                if yes_price is None or yes_price <= 0:
                    continue

                curve.append({
                    "end_date": end_str,
                    "end_dt": end_dt,
                    "hours_left": hours_left,
                    "yes_price": yes_price,
                    "volume_24h": m["volume_24h"],
                    "condition_id": m["condition_id"],
                    "title": m["title"],
                })

            if len(curve) < min_group:
                continue

            # 4. Signal generation: find overpriced near-term dates
            for i, point in enumerate(curve[:-1]):
                next_point = curve[i + 1]
                yes = point["yes_price"]

                if yes < min_yes or yes > max_yes:
                    continue

                days_between = max((next_point["end_dt"] - point["end_dt"]).total_seconds() / 86400, 1)
                marginal = next_point["yes_price"] - yes
                marginal_per_day = marginal / days_between

                days_left = max(point["hours_left"] / 24, 1)
                implied_daily = yes / days_left

                if implied_daily >= min_marginal:
                    # Build end_date_iso as full ISO string for resolution-aware plugin decisions
                    _end_date = point["end_date"]
                    _end_date_iso = f"{_end_date}T23:59:59+00:00" if _end_date else ""
                    signals.append(StrategySignal(
                        wallet="autonomous",
                        condition_id=point["condition_id"],
                        outcome="NO",
                        title=point["title"],
                        current_price=1.0 - yes,
                        position_value=float(params.get("bet_size_usdc", DEFAULTS["bet_size_usdc"])),
                        analyzed_ts=_now_ts(),
                        metadata={
                            "topic": topic,
                            "yes_price": yes,
                            "no_price": 1.0 - yes,
                            "end_date": _end_date,
                            "end_date_iso": _end_date_iso,
                            "hours_left": round(point["hours_left"], 1),
                            "curve_length": len(curve),
                            "next_date_yes": next_point["yes_price"],
                            "marginal_per_day": round(marginal_per_day, 4),
                            "volume_24h": point["volume_24h"],
                        },
                    ))

        logger.info(
            "term_structure discover: %d groups analyzed, %d signals generated",
            len(valid_groups), len(signals),
        )
        return signals

    def enter(self, conn, strategy, signal, open_positions) -> StrategyEntryDecision:
        params = _parse_params(strategy)
        max_concurrent = int(params.get("max_concurrent_positions", DEFAULTS["max_concurrent_positions"]))
        bet_size = float(params.get("bet_size_usdc", DEFAULTS["bet_size_usdc"]))
        max_exposure = float(params.get("max_exposure_per_market_usdc", DEFAULTS["max_exposure_per_market_usdc"]))
        cid = signal.condition_id
        outcome = signal.outcome

        # Gate 1: max concurrent
        if len(open_positions) >= max_concurrent:
            return StrategyEntryDecision(
                should_enter=False,
                reason=f"Max concurrent ({max_concurrent}) reached",
            )

        # Gate 2: duplicate check
        for pos in open_positions:
            if isinstance(pos, dict):
                if pos.get("condition_id") == cid and (pos.get("outcome") or "").lower() == outcome.lower():
                    return StrategyEntryDecision(
                        should_enter=False,
                        reason="Already holding this market",
                    )

        # Gate 3: cross-dedup with paper_trades
        held = conn.execute(
            """SELECT 1 FROM paper_trades
               WHERE condition_id = ? AND LOWER(outcome) = LOWER(?)
                 AND status = 'open' AND duplicate_of IS NULL LIMIT 1""",
            (cid, outcome),
        ).fetchone()
        if held:
            return StrategyEntryDecision(
                should_enter=False,
                reason="Already held in paper_trades",
            )

        # Gate 4: market exposure cap
        existing_exposure = sum(
            float(p.get("entry_price", 0) or 0) * float(p.get("entry_shares", 0) or 0)
            for p in open_positions
            if isinstance(p, dict) and p.get("condition_id") == cid
        )
        if existing_exposure + bet_size > max_exposure:
            return StrategyEntryDecision(
                should_enter=False,
                reason=f"Market exposure cap (${max_exposure}) would be exceeded",
            )

        # Gate 5: verify live price hasn't moved
        live_yes = _fetch_yes_price(cid)
        if live_yes is not None:
            no_price = 1.0 - live_yes
        else:
            no_price = signal.current_price

        meta = signal.metadata or {}
        topic = meta.get("topic", "?")
        curve_len = meta.get("curve_length", 0)
        end_date = meta.get("end_date", "?")

        return StrategyEntryDecision(
            should_enter=True,
            reason=f"Term structure: NO on {end_date} ({curve_len}-point curve, topic: {topic[:40]})",
            size_usdc=bet_size,
            entry_price=no_price,
            execution_mode="paper",
            metadata=meta,
        )

    def manage(self, conn, strategy, position) -> StrategyManageState:
        cid = position.get("condition_id", "")
        live_yes = _fetch_yes_price(cid)
        if live_yes is not None:
            current_price = live_yes  # manage uses YES price for resolution checks
        else:
            current_price = float(position.get("entry_price", 0.5) or 0.5)

        return StrategyManageState(
            current_price=current_price,
            signal_open=True,
        )

    def exit(self, conn, strategy, position, state) -> StrategyExitDecision:
        params = _parse_params(strategy)
        price = state.current_price if state.current_price is not None else 0.5
        entry_price = float(position.get("entry_price", 0) or 0)
        outcome = (position.get("outcome") or "").upper()
        max_hours = float(params.get("max_hours_to_end", DEFAULTS["max_hours_to_end"]))
        resolution_stop_cutoff = int(float(params.get("resolution_stop_cutoff_days", DEFAULTS["resolution_stop_cutoff_days"])))
        cascade_active = bool(position.get("cascade_active", False))
        cascade_mode = params.get("cascade_exit_mode", DEFAULTS["cascade_exit_mode"])

        days_left = days_to_resolution(position.get("end_date_iso"))

        # price is the YES price from manage(). All positions are NO, so the
        # actual NO-side market price is (1 - YES).  Use no_price everywhere
        # so exit_price always reflects what the NO share is worth at exit.
        no_price = round(1.0 - price, 4)

        # Resolution exits always fire (Task 3: only resolution exits pass through during cascade)
        if price >= RESOLUTION_WIN:
            # YES resolved to ~1.0 → NO is worth ~0.0 → loss for NO holder
            return StrategyExitDecision(
                should_exit=True,
                reason="market_resolved_yes",
                exit_price=no_price,
                status="closed",
            )
        if price <= RESOLUTION_LOSS:
            # YES resolved to ~0.0 → NO is worth ~1.0 → win for NO holder
            return StrategyExitDecision(
                should_exit=True,
                reason="market_resolved_no",
                exit_price=no_price,
                status="closed",
            )

        # Cascade: hold_through suppresses price-based stops (structural arb, not news-sensitive)
        if cascade_active and cascade_mode == "hold_through":
            logger.info(
                "term_structure_v1: cascade_active, cascade_mode=hold_through — skipping stop-loss for %s",
                position.get("condition_id", ""),
            )
            return StrategyExitDecision(should_exit=False, reason="hold_cascade", exit_price=no_price)

        # Stop-loss: skip if close to resolution (Task 3)
        if outcome == "NO" and entry_price > 0:
            near_resolution = days_left is not None and days_left < resolution_stop_cutoff
            if near_resolution:
                logger.debug(
                    "term_structure_v1: suppressing stop-loss — %dd to resolution < %dd cutoff",
                    days_left, resolution_stop_cutoff,
                )
            else:
                no_entry = entry_price
                no_current = no_price  # 1.0 - YES price
                if no_current < no_entry * 0.70:  # 30% adverse drift
                    return StrategyExitDecision(
                        should_exit=True,
                        reason=f"stop_loss: NO dropped from {no_entry:.3f} to {no_current:.3f}",
                        exit_price=no_current,
                        status="closed",
                    )

        # Staleness
        opened_ts = int(position.get("opened_ts", 0) or 0)
        if opened_ts > 0:
            age_hours = (_now_ts() - opened_ts) / 3600
            if age_hours > max_hours:
                return StrategyExitDecision(
                    should_exit=True,
                    reason=f"stale: held {age_hours:.0f}h > {max_hours:.0f}h max",
                    exit_price=no_price,
                    status="closed",
                )

        return StrategyExitDecision(should_exit=False, reason="hold", exit_price=no_price)
