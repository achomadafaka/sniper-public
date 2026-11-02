"""News Intel v1 strategy plugin.

Bridges the trade_ideas table (populated by brain.py from news analysis)
into the strategy runner pipeline:
  - discover(): reads open HIGH-confidence trade_ideas → StrategySignal
  - MEDIUM-confidence ideas trigger a Telegram notification only
  - enter(): gates on max concurrent, cross-dedup with paper_trades, price sanity
  - manage(): fetches live YES price from CLOB API
  - exit(): resolution, stop-loss (25% adverse drift), staleness (90 days)
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List

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

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULTS: Dict[str, Any] = {
    "bet_size_usdc": 10.0,
    "max_concurrent_positions": 10,
    "max_exposure_per_market_usdc": 50.0,
    "stop_loss_pct": 0.25,
    "max_days_hold": 90,
    "resolution_stop_cutoff_days": 5,   # don't fire stop-loss within this many days of resolution
    "cascade_exit_mode": "follow",      # news-based position — cascade is relevant, use normal exit logic
    # Direction mode:
    #   "idea"     — use the direction from the trade_idea (default; bot chooses side)
    #   "momentum" — override direction using live vs stored price delta.
    #                Bot still identifies the market; price momentum determines YES/NO.
    #                Avoids flip-flopping when the bot re-analyzes and chases price moves.
    "direction_mode": "idea",
    # Minimum price move (vs idea's stored current_price) to confirm momentum direction.
    # Below this threshold in momentum mode the signal is skipped — no clear trend.
    # 3% is calibrated from paper trade data: chasing_up_3pct+ shows 72.7% WR.
    "momentum_threshold_pct": 0.03,
    # Category filter — applied against market_metadata.category (values: "politics",
    # "crypto", "sports", or empty string).
    # Empty category passes through (title pattern is the primary gate for world_analyst).
    # allowed_categories: [] = accept all; non-empty = allowlist (OR-match)
    # blocked_categories: [] = block none; non-empty = blocklist (any match = skip)
    "allowed_categories": [],
    "blocked_categories": [],
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


def _category_allowed(
    category: str,
    allowed: List[str],
    blocked: List[str],
) -> bool:
    """Return True if category passes the allow/block filter.

    Polymarket often returns empty category for political/world markets.
    Empty category → only blocklist applies; allowlist is skipped.
    Blocked takes priority over allowed.
    """
    cat_lower = (category or "").lower()
    for b in blocked:
        if b.lower() in cat_lower:
            return False
    if not cat_lower:
        return True
    if not allowed:
        return True
    for a in allowed:
        if a.lower() in cat_lower:
            return True
    return False


def _fetch_yes_price(condition_id: str) -> float | None:
    """Fetch live YES price from CLOB API.  Returns None on failure."""
    try:
        import requests

        r = requests.get(
            f"https://clob.polymarket.com/markets/{condition_id}",
            timeout=8,
        )
        if r.ok:
            tokens = r.json().get("tokens", [])
            for t in tokens:
                if str(t.get("outcome", "")).upper() == "YES" and t.get("price") is not None:
                    return max(0.0, min(1.0, float(t["price"])))
    except Exception:
        logger.debug("_fetch_yes_price failed for %s", condition_id, exc_info=True)
    return None


class NewsIntelV1Strategy(StrategyPlugin):
    """Auto-enter HIGH-confidence news trade ideas, alert on MEDIUM."""

    plugin_id = "news_intel_v1"
    display_name = "News Intel v1"
    description = (
        "Bridges brain.py trade_ideas into strategy positions. "
        "HIGH confidence → auto-enter, MEDIUM → Telegram alert."
    )
    strategy_family = "news_intel"
    required_feeds = ("polymarket",)
    supported_execution_modes = ("paper",)
    default_loop_seconds = 1800  # 30 min

    # -----------------------------------------------------------------
    # discover
    # -----------------------------------------------------------------

    def discover(
        self,
        conn,
        strategy: Dict[str, Any],
    ) -> List[StrategySignal]:
        """Read open trade_ideas, return HIGH as signals, alert MEDIUM."""
        signals: List[StrategySignal] = []
        seen_conditions: set = set()

        params = _parse_params(strategy)
        raw_sa = params.get("source_agent")
        source_agent_filter: str | None = raw_sa if raw_sa else None
        direction_mode = str(params.get("direction_mode", DEFAULTS["direction_mode"])).lower()
        momentum_threshold = _to_float(
            params.get("momentum_threshold_pct", DEFAULTS["momentum_threshold_pct"])
        )
        allowed_cats: List[str] = params.get("allowed_categories", DEFAULTS["allowed_categories"]) or []
        blocked_cats: List[str] = params.get("blocked_categories", DEFAULTS["blocked_categories"]) or []

        try:
            if source_agent_filter:
                # Exact match only — no NULL fallback.
                # When source_agent is set on a strategy, only that agent's ideas
                # should be traded. Including source_agent IS NULL caused strategy
                # #28 and #29 to compete for shared legacy ideas; #28 wins the
                # cross-strategy dedup every cycle, leaving #29 with 0 positions.
                rows = conn.execute(
                    """SELECT ti.*, mm.end_date_iso as market_end_date_iso,
                              mm.category as market_category
                       FROM trade_ideas ti
                       LEFT JOIN market_metadata mm ON mm.condition_id = ti.condition_id
                       WHERE ti.status='open'
                         AND (ti.expires_at IS NULL OR ti.expires_at > datetime('now'))
                         AND ti.source_agent = ?
                       ORDER BY ti.confidence DESC, ti.id DESC""",
                    (source_agent_filter,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT ti.*, mm.end_date_iso as market_end_date_iso,
                              mm.category as market_category
                       FROM trade_ideas ti
                       LEFT JOIN market_metadata mm ON mm.condition_id = ti.condition_id
                       WHERE ti.status='open'
                         AND (ti.expires_at IS NULL OR ti.expires_at > datetime('now'))
                       ORDER BY ti.confidence DESC, ti.id DESC"""
                ).fetchall()
        except Exception:
            logger.warning("news_intel discover: trade_ideas query failed", exc_info=True)
            return []

        for row in rows:
            condition_id = row["condition_id"]

            # Reject zero/null/fake condition IDs — they can never be priced or exited
            # Valid Polymarket condition_id: "0x" + exactly 64 lowercase hex chars
            _NULL_CID = "0x" + "0" * 64
            _valid_cid = bool(condition_id and re.fullmatch(r"0x[0-9a-f]{64}", condition_id or ""))
            if not _valid_cid or condition_id == _NULL_CID:
                logger.warning(
                    "news_intel discover: skipping idea id=%s — invalid condition_id %r",
                    row["id"], condition_id,
                )
                continue

            # Dedup within cycle
            if condition_id in seen_conditions:
                continue
            seen_conditions.add(condition_id)

            # Category filter (blocked_categories / allowed_categories)
            mkt_category = row["market_category"] or ""
            if (allowed_cats or blocked_cats) and not _category_allowed(mkt_category, allowed_cats, blocked_cats):
                logger.info(
                    "news_intel discover: skipping idea id=%s — category %r blocked (blocked=%s allowed=%s)",
                    row["id"], mkt_category, blocked_cats, allowed_cats,
                )
                continue

            confidence = str(row["confidence"]).upper()
            direction = str(row["direction"]).upper()  # BUY_YES or BUY_NO
            outcome = "Yes" if "YES" in direction else "No"
            # trade_ideas.current_price is already the tradeable-side price:
            # YES price for buy_yes, NO price for buy_no (set by news_intel.py line 503)
            price = _to_float(row["current_price"])

            # --- Momentum direction override ---
            # In "momentum" mode we ignore the bot's chosen direction entirely.
            # Instead: fetch the live YES price, compare to the price stored when
            # the idea was created.  If the move is >= momentum_threshold, ride that
            # direction.  If the move is below threshold, no clear signal — skip.
            if direction_mode == "momentum":
                idea_yes_price = _to_float(row["current_price"])   # stored at idea creation
                live_yes_price = _fetch_yes_price(condition_id)
                if live_yes_price is None:
                    logger.debug(
                        "news_intel momentum: skipping %s — live price unavailable",
                        condition_id[:16],
                    )
                    continue
                delta = live_yes_price - idea_yes_price
                if abs(delta) < momentum_threshold:
                    logger.debug(
                        "news_intel momentum: skipping %s — delta %.3f < threshold %.3f",
                        condition_id[:16], delta, momentum_threshold,
                    )
                    continue
                # Ride the momentum direction
                if delta > 0:
                    outcome = "Yes"
                    price = live_yes_price
                    direction = "BUY_YES"
                    logger.info(
                        "news_intel momentum: %s YES (delta +%.3f, live=%.3f, idea=%.3f)",
                        condition_id[:16], delta, live_yes_price, idea_yes_price,
                    )
                else:
                    outcome = "No"
                    price = round(1.0 - live_yes_price, 4)
                    direction = "BUY_NO"
                    logger.info(
                        "news_intel momentum: %s NO (delta %.3f, live=%.3f, idea=%.3f)",
                        condition_id[:16], delta, live_yes_price, idea_yes_price,
                    )

            # For non-momentum mode: verify market exists on CLOB before entering.
            # Format-valid CIDs (0x + 64 hex) can still point to non-existent markets.
            # If CLOB returns no price, the position can never be priced or exited.
            if direction_mode != "momentum":
                _live_yes = _fetch_yes_price(condition_id)
                if _live_yes is None:
                    logger.warning(
                        "news_intel discover: skipping idea id=%s — CLOB returned no price "
                        "for %r (market may not exist or is not live)",
                        row["id"], condition_id,
                    )
                    continue
                # Refresh price with live CLOB data
                if outcome == "Yes":
                    price = _live_yes
                else:
                    price = round(1.0 - _live_yes, 4)

            if confidence == "HIGH":
                signals.append(StrategySignal(
                    wallet=f"news_{condition_id[:8]}",
                    condition_id=condition_id,
                    outcome=outcome,
                    title=row["market_title"],
                    current_price=price,
                    position_value=0.0,
                    analyzed_ts=_now_ts(),
                    metadata={
                        "idea_id": row["id"],
                        "confidence": confidence,
                        "reasoning": row["reasoning"],
                        "direction": direction,
                        "news_source": row["news_source"],
                        "end_date_iso": row["market_end_date_iso"] or "",
                    },
                ))
            elif confidence == "MEDIUM":
                # Dedup: only alert once per idea (check alerted_at flag)
                if not row["alerted_at"]:
                    self._send_medium_alert(row)
                    try:
                        conn.execute(
                            "UPDATE trade_ideas SET alerted_at = CURRENT_TIMESTAMP WHERE id = ?",
                            (row["id"],),
                        )
                        conn.commit()
                    except Exception:
                        logger.debug("news_intel: failed to mark idea %s as alerted", row["id"], exc_info=True)

        logger.info(
            "news_intel discover: %d ideas scanned, %d HIGH signals",
            len(rows), len(signals),
        )
        return signals

    def _send_medium_alert(self, row) -> None:
        """Send a Telegram notification for a MEDIUM-confidence idea."""
        try:
            import requests

            bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
            chat_id = os.getenv("TELEGRAM_CHAT_ID")
            if not bot_token or not chat_id:
                logger.debug("news_intel: Telegram env vars not set, skipping MEDIUM alert")
                return

            direction = str(row["direction"]).upper()
            side = "BUY YES" if "YES" in direction else "BUY NO"
            price = _to_float(row["current_price"])
            reasoning = str(row["reasoning"] or "")[:200]
            source = row["news_source"] or "unknown"
            idea_id = row["id"]
            title = row["market_title"]

            text = (
                f"\U0001f4f0 News Trade Idea (MEDIUM)\n"
                f"<b>{title}</b> \u2192 {side} @ \u00a2{price * 100:.0f}\n"
                f"Reasoning: {reasoning}\n"
                f"Source: {source}\n"
                f"Idea #{idea_id}"
            )

            requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                },
                timeout=10,
            )
        except Exception:
            logger.debug("news_intel: MEDIUM alert send failed", exc_info=True)

    # -----------------------------------------------------------------
    # enter
    # -----------------------------------------------------------------

    def enter(
        self,
        conn,
        strategy: Dict[str, Any],
        signal: StrategySignal,
        open_positions: List[Dict[str, Any]],
    ) -> StrategyEntryDecision:
        params = _parse_params(strategy)
        bet_size = _to_float(params.get("bet_size_usdc", DEFAULTS["bet_size_usdc"]))
        max_conc = int(_to_float(params.get("max_concurrent_positions", DEFAULTS["max_concurrent_positions"])))

        # Gate 1: max concurrent
        if len(open_positions) >= max_conc:
            return StrategyEntryDecision(
                should_enter=False,
                reason=f"Max concurrent {max_conc} reached",
            )

        # Gate 2: duplicate in open_positions
        for pos in open_positions:
            if pos.get("condition_id") == signal.condition_id and pos.get("outcome") == signal.outcome:
                return StrategyEntryDecision(
                    should_enter=False,
                    reason="Duplicate open position",
                )

        # Gate 3: cross-dedup with paper_trades
        try:
            dup_row = conn.execute(
                """SELECT 1 FROM paper_trades
                   WHERE condition_id=?
                     AND LOWER(outcome)=LOWER(?)
                     AND status='open'
                     AND duplicate_of IS NULL""",
                (signal.condition_id, signal.outcome),
            ).fetchone()
            if dup_row:
                return StrategyEntryDecision(
                    should_enter=False,
                    reason="Duplicate in paper_trades",
                )
        except Exception:
            # paper_trades table may not exist in tests; log and continue
            logger.debug("news_intel enter: paper_trades cross-dedup query failed", exc_info=True)

        # Gate 4: price sanity — tradeable price must be in [0.05, 0.92]
        # (signal.current_price is already the tradeable side: NO price for BUY_NO)
        price = signal.current_price
        if price <= 0.05 or price >= 0.92:
            return StrategyEntryDecision(
                should_enter=False,
                reason=f"Price {price:.4f} outside sane range (0.05, 0.92)",
            )

        # Gate 5: market exposure cap
        max_mkt = _to_float(params.get("max_exposure_per_market_usdc", DEFAULTS["max_exposure_per_market_usdc"]))
        mkt_exposure = sum(
            _to_float(p.get("size_usdc", 0))
            for p in open_positions
            if p.get("condition_id") == signal.condition_id
        )
        if mkt_exposure + bet_size > max_mkt:
            return StrategyEntryDecision(
                should_enter=False,
                reason=f"Market exposure ${mkt_exposure:.0f}+${bet_size:.0f} exceeds cap ${max_mkt:.0f}",
            )

        # All gates passed
        return StrategyEntryDecision(
            should_enter=True,
            reason=f"News intel entry: {signal.outcome}@{price:.3f}",
            size_usdc=bet_size,
            entry_price=price,
            execution_mode="paper",
            metadata=signal.metadata or {},
        )

    # -----------------------------------------------------------------
    # manage
    # -----------------------------------------------------------------

    def manage(
        self,
        conn,
        strategy: Dict[str, Any],
        position: Dict[str, Any],
    ) -> StrategyManageState:
        cond_id = position.get("condition_id", "")
        entry_price = _to_float(position.get("entry_price") or position.get("avg_entry_price"))

        live_price = _fetch_yes_price(cond_id)
        # manage() must always return the YES price (exit() assumes this).
        # For NO positions, entry_price is the NO price — invert it as fallback.
        outcome = str(position.get("outcome", "Yes"))
        if live_price is not None:
            current_price = live_price
        elif outcome.upper() == "NO":
            current_price = round(1.0 - entry_price, 4) if entry_price > 0 else entry_price
        else:
            current_price = entry_price

        return StrategyManageState(
            current_price=current_price,
            signal_open=True,
            metadata={"live_price_available": live_price is not None},
        )

    # -----------------------------------------------------------------
    # exit
    # -----------------------------------------------------------------

    def exit(
        self,
        conn,
        strategy: Dict[str, Any],
        position: Dict[str, Any],
        state: StrategyManageState,
    ) -> StrategyExitDecision:
        params = _parse_params(strategy)
        stop_loss_pct = _to_float(params.get("stop_loss_pct", DEFAULTS["stop_loss_pct"]))
        max_days = int(_to_float(params.get("max_days_hold", DEFAULTS["max_days_hold"])))
        resolution_stop_cutoff = int(_to_float(params.get("resolution_stop_cutoff_days", DEFAULTS["resolution_stop_cutoff_days"])))
        cascade_active = bool(position.get("cascade_active", False))
        cascade_mode = params.get("cascade_exit_mode", DEFAULTS["cascade_exit_mode"])

        current_price = state.current_price
        entry_price = _to_float(position.get("entry_price") or position.get("avg_entry_price"))
        outcome = str(position.get("outcome", "Yes"))
        is_no = outcome.upper() == "NO"

        days_left = days_to_resolution(position.get("end_date_iso"))

        # For NO positions manage() returns the YES price; convert to NO price for all
        # P&L-bearing exit_price fields so _calc_pnl(shares, exit_price - entry_price) is correct.
        # entry_price was stored as the NO price (current_price at idea creation for buy_no ideas).
        def _exit_p(yes_price: float) -> float:
            return round(1.0 - yes_price, 4) if is_no else yes_price

        # --- Resolution (always fires regardless of cutoffs) ---
        if current_price is not None:
            if current_price >= RESOLUTION_WIN:
                return StrategyExitDecision(
                    should_exit=True,
                    reason="market_resolved_yes",
                    exit_price=_exit_p(current_price),
                    status="closed",
                )
            if current_price <= RESOLUTION_LOSS:
                return StrategyExitDecision(
                    should_exit=True,
                    reason="market_resolved_no",
                    exit_price=_exit_p(current_price),
                    status="closed",
                )

        # Cascade: hold_through suppresses price-based stops; follow uses normal logic
        if cascade_active and cascade_mode == "hold_through":
            logger.info(
                "news_intel_v1: cascade_active but cascade_mode=hold_through — skipping stop-loss for %s",
                position.get("condition_id", ""),
            )
            return StrategyExitDecision(should_exit=False, reason="hold_cascade")

        # --- Stop-loss (skip if close to resolution) ---
        if current_price is not None and entry_price > 0:
            near_resolution = days_left is not None and days_left < resolution_stop_cutoff
            if near_resolution:
                logger.debug(
                    "news_intel_v1: suppressing stop-loss — %dd to resolution < %dd cutoff",
                    days_left, resolution_stop_cutoff,
                )
            else:
                if not is_no:
                    # YES side: adverse = YES price dropping below entry
                    if current_price < entry_price * (1 - stop_loss_pct):
                        return StrategyExitDecision(
                            should_exit=True,
                            reason="stop_loss",
                            exit_price=current_price,
                            status="closed",
                        )
                else:
                    # NO side: entry_price stored as NO price; current NO = (1 - yes_price)
                    no_current = round(1.0 - current_price, 4)
                    if entry_price > 0 and no_current < entry_price * (1 - stop_loss_pct):
                        return StrategyExitDecision(
                            should_exit=True,
                            reason="stop_loss",
                            exit_price=no_current,
                            status="closed",
                        )

        # --- Staleness ---
        opened_ts = _to_float(position.get("opened_ts", 0))
        if opened_ts > 0:
            age_days = (time.time() - opened_ts) / 86400
            if age_days > max_days:
                return StrategyExitDecision(
                    should_exit=True,
                    reason="stale_position",
                    exit_price=_exit_p(current_price) if current_price is not None else entry_price,
                    status="closed",
                )

        return StrategyExitDecision(should_exit=False, reason="hold")
