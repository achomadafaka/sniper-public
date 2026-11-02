"""Paper Limit Engine — core order lifecycle for stink-bid paper limit orders.

Manages: place, reprice, cancel, expire, fill-check with dual-label evaluation
(strict / loose), and position bridge (creates a strategy_positions row on
strict fill).

Design decisions:
- Receives a sqlite3.Connection from the caller — no pooling or async here.
- All DB writes are committed immediately after each mutation.
- ``TradeTick`` comes from ``feeds.polymarket_trade_tap`` (Task 3).
- ``strategy_positions`` wallet uses the synthetic prefix
  ``paper:stink_bid:<strategy_id>`` to avoid colliding with real wallets on the
  unique partial index ``uq_strategy_positions_active_owner``.
- ``current_price`` is set to ``fill_price`` (NOT 0) on position open so the
  existing resolution detector does not immediately flag a resolution_loss.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from sniper.feeds.polymarket_trade_tap import TradeTick

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def compute_bid_price(
    favorite_price: float,
    dip_pct: float = 0.30,
    min_bid: float = 0.05,
) -> float:
    """Compute the resting bid price from the favorite's current price.

    Args:
        favorite_price: The current market price of the favorite outcome token
                        (in [0.0, 1.0]).
        dip_pct: Fractional discount from the favorite price.  Default 0.30
                 means we bid 30% below market.
        min_bid: Floor for the computed bid.  Default 0.05.

    Returns:
        The bid price rounded to 2 decimal places, floored at ``min_bid``.

    Examples:
        >>> compute_bid_price(0.70)   # 0.70 * 0.70 = 0.49
        0.49
        >>> compute_bid_price(0.55)   # 0.55 * 0.70 = 0.385 → 0.39
        0.39
    """
    raw = favorite_price * (1.0 - dip_pct)
    return max(round(raw, 2), min_bid)


def _now_iso() -> str:
    """Return the current UTC time in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


def _now_ts() -> int:
    """Return the current UTC Unix timestamp as an integer."""
    return int(datetime.now(timezone.utc).timestamp())


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class PaperLimitEngine:
    """Paper limit order lifecycle manager.

    All methods operate synchronously on the provided ``conn``.  The caller
    is responsible for connection management and should not share the
    connection across threads.

    Args:
        conn: An open ``sqlite3.Connection`` with ``row_factory = sqlite3.Row``
              set (or raw tuple access is also supported — the engine uses
              column-name access on ``SELECT *`` results).
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        # Ensure row_factory supports column-name access.
        if self._conn.row_factory is None:
            self._conn.row_factory = sqlite3.Row

    # ------------------------------------------------------------------
    # Public API — placement
    # ------------------------------------------------------------------

    def place_order(
        self,
        strategy_id: int,
        condition_id: str,
        market_title: str,
        favorite_token_id: str,
        favorite_token_label: str,
        favorite_price: float,
        min_fill_size: float = 50.0,
        dip_pct: float = 0.30,
        min_bid: float = 0.05,
        event_slug: str = "",
        espn_event_id: str = "",
        tour: str = "",
        player1: str = "",
        player2: str = "",
        max_resting: int = 3,
        max_daily_fills: int = 5,
    ) -> int | None:
        """Place a new resting paper limit order.

        Safety rails are checked before insertion:
        1. ``count_resting(strategy_id) < max_resting``
        2. ``count_daily_fills(strategy_id) < max_daily_fills``

        A unique index on ``(strategy_id, condition_id)`` prevents duplicates.
        If the index fires an ``IntegrityError`` the method returns ``None``
        and logs a skip event (reason ``prior_order_exists``).

        Args:
            strategy_id: ID of the owning strategy.
            condition_id: Polymarket condition/market ID.
            market_title: Human-readable market title (for logging/reporting).
            favorite_token_id: Token ID of the favorite outcome.
            favorite_token_label: Human label (e.g. "Djokovic").
            favorite_price: Current market price of the favorite in [0, 1].
            min_fill_size: Minimum trade size (USDC) for a strict fill.
            dip_pct: Discount applied to compute the bid.
            min_bid: Bid floor.
            event_slug: Optional ESPN/Polymarket event slug.
            espn_event_id: Cached ESPN event ID.
            tour: "atp" or "wta".
            player1: Player 1 surname.
            player2: Player 2 surname.
            max_resting: Max simultaneous resting orders.
            max_daily_fills: Max strict fills in a UTC calendar day.

        Returns:
            The new ``order_id`` on success, or ``None`` if blocked/duplicate.
        """
        # --- Safety rail: max resting ---
        resting = self.count_resting(strategy_id)
        if resting >= max_resting:
            logger.info(
                "place_order skipped: max_resting reached strategy_id=%s condition_id=%s "
                "resting=%d max=%d",
                strategy_id, condition_id, resting, max_resting,
            )
            self.log_skip(
                strategy_id=strategy_id,
                condition_id=condition_id,
                reason="max_resting_reached",
                details={"max_resting": max_resting, "current": resting},
            )
            return None

        # --- Safety rail: max daily fills ---
        daily = self.count_daily_fills(strategy_id)
        if daily >= max_daily_fills:
            logger.info(
                "place_order skipped: max_daily_fills reached strategy_id=%s "
                "condition_id=%s fills_today=%d max=%d",
                strategy_id, condition_id, daily, max_daily_fills,
            )
            self.log_skip(
                strategy_id=strategy_id,
                condition_id=condition_id,
                reason="max_daily_fills_reached",
                details={"max_daily_fills": max_daily_fills, "fills_today": daily},
            )
            return None

        bid_price = compute_bid_price(favorite_price, dip_pct=dip_pct, min_bid=min_bid)
        now = _now_iso()

        try:
            cur = self._conn.execute(
                """
                INSERT INTO paper_limit_orders (
                    strategy_id, condition_id, market_title, event_slug,
                    favorite_token_id, favorite_token_label,
                    favorite_price_at_entry, bid_price, dip_pct, min_fill_size,
                    state, placed_at, updated_at,
                    espn_event_id, tour, player1, player2
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'resting', ?, ?, ?, ?, ?, ?)
                """,
                (
                    strategy_id, condition_id, market_title, event_slug,
                    favorite_token_id, favorite_token_label,
                    favorite_price, bid_price, dip_pct, min_fill_size,
                    now, now,
                    espn_event_id, tour, player1, player2,
                ),
            )
            self._conn.commit()
            order_id = int(cur.lastrowid or 0)
        except sqlite3.IntegrityError:
            # Duplicate (strategy_id, condition_id) — unique index fired.
            logger.info(
                "place_order duplicate rejected strategy_id=%s condition_id=%s",
                strategy_id, condition_id,
            )
            self.log_skip(
                strategy_id=strategy_id,
                condition_id=condition_id,
                reason="prior_order_exists",
            )
            return None

        self._log_event(
            order_id=order_id,
            event_type="order_placed",
            state_before="",
            state_after="resting",
            details={
                "market_title": market_title,
                "condition_id": condition_id,
                "favorite_token_id": favorite_token_id,
                "favorite_token_label": favorite_token_label,
                "favorite_price": favorite_price,
                "bid_price": bid_price,
                "dip_pct": dip_pct,
                "min_fill_size": min_fill_size,
                "event_slug": event_slug,
                "tour": tour,
                "player1": player1,
                "player2": player2,
            },
        )
        logger.info(
            "order_placed order_id=%d strategy_id=%s condition_id=%s "
            "favorite_price=%.4f bid_price=%.4f",
            order_id, strategy_id, condition_id, favorite_price, bid_price,
        )
        return order_id

    # ------------------------------------------------------------------
    # Public API — state transitions
    # ------------------------------------------------------------------

    def cancel_order(self, order_id: int, reason: str) -> bool:
        """Transition a resting order to 'cancelled'. Returns False if not resting.

        Args:
            order_id: The ID of the resting order.
            reason: Human-readable reason string stored in ``cancel_reason``.
        """
        now = _now_iso()
        cur = self._conn.execute(
            """
            UPDATE paper_limit_orders
            SET state = 'cancelled', cancel_reason = ?, updated_at = ?, closed_at = ?
            WHERE order_id = ? AND state = 'resting'
            """,
            (reason, now, now, order_id),
        )
        self._conn.commit()
        if cur.rowcount == 0:
            return False
        self._log_event(
            order_id=order_id,
            event_type="order_cancelled",
            state_before="resting",
            state_after="cancelled",
            details={"reason": reason},
        )
        logger.info("order_cancelled order_id=%d reason=%s", order_id, reason)
        return True

    def expire_order(self, order_id: int, reason: str) -> bool:
        """Transition a resting order to 'expired'. Returns False if not resting.

        Args:
            order_id: The ID of the resting order.
            reason: Human-readable reason (e.g. ``"market_resolved"``).
        """
        now = _now_iso()
        cur = self._conn.execute(
            """
            UPDATE paper_limit_orders
            SET state = 'expired', cancel_reason = ?, updated_at = ?, closed_at = ?
            WHERE order_id = ? AND state = 'resting'
            """,
            (reason, now, now, order_id),
        )
        self._conn.commit()
        if cur.rowcount == 0:
            return False
        self._log_event(
            order_id=order_id,
            event_type="order_expired",
            state_before="resting",
            state_after="expired",
            details={"reason": reason},
        )
        logger.info("order_expired order_id=%d reason=%s", order_id, reason)
        return True

    def reprice_order(
        self,
        order_id: int,
        new_favorite_price: float,
        threshold: float = 0.02,
        dip_pct: float = 0.30,
        min_bid: float = 0.05,
    ) -> bool:
        """Recompute the bid from the new favorite price and update if meaningful.

        The reprice only happens if the absolute bid diff is >= the *smaller*
        of ``threshold`` and ``old_bid * 0.05`` (5% relative threshold).

        Args:
            order_id: The resting order to reprice.
            new_favorite_price: Current market price of the favorite.
            threshold: Absolute threshold (default 0.02).
            dip_pct: Discount fraction — should match the original order's dip_pct.
            min_bid: Bid floor.

        Returns:
            ``True`` if the bid was updated, ``False`` if the diff was too small.
        """
        row = self._conn.execute(
            "SELECT bid_price, dip_pct, state FROM paper_limit_orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        if row is None:
            logger.warning("reprice_order: order_id=%d not found", order_id)
            return False
        if row["state"] != "resting":
            return False

        # Use the order's stored dip_pct if no override was explicitly different
        # from the default — callers that stored dip_pct in the row should pass
        # it through; for simplicity we accept an explicit parameter.
        old_bid = float(row["bid_price"])
        new_bid = compute_bid_price(new_favorite_price, dip_pct=dip_pct, min_bid=min_bid)

        effective_threshold = min(threshold, old_bid * 0.05)
        diff = abs(new_bid - old_bid)

        if diff < effective_threshold:
            logger.debug(
                "reprice_order skipped order_id=%d old_bid=%.4f new_bid=%.4f diff=%.4f "
                "threshold=%.4f",
                order_id, old_bid, new_bid, diff, effective_threshold,
            )
            return False

        now = _now_iso()
        self._conn.execute(
            """
            UPDATE paper_limit_orders
            SET bid_price = ?, updated_at = ?
            WHERE order_id = ? AND state = 'resting'
            """,
            (new_bid, now, order_id),
        )
        self._conn.commit()
        self._log_event(
            order_id=order_id,
            event_type="order_repriced",
            state_before="resting",
            state_after="resting",
            details={
                "old_bid": old_bid,
                "new_bid": new_bid,
                "favorite_price_now": new_favorite_price,
                "diff": diff,
                "effective_threshold": effective_threshold,
            },
        )
        logger.info(
            "order_repriced order_id=%d old_bid=%.4f new_bid=%.4f",
            order_id, old_bid, new_bid,
        )
        return True

    # ------------------------------------------------------------------
    # Public API — fill detection
    # ------------------------------------------------------------------

    def check_fill(
        self,
        order_id: int,
        tick: TradeTick,
        position_size_usdc: float = 25.0,
    ) -> str | None:
        """Evaluate a trade tick against a resting paper limit order.

        Fill conditions:
        - Order must be in state ``'resting'``.
        - ``tick.token_id`` must match ``favorite_token_id``.
        - ``tick.price <= bid_price`` (taker dumped into our bid).

        If the price condition is met:
        - ``tick.size >= min_fill_size`` → **strict fill**: log both strict +
          loose fill rows, transition order to 'filled', create
          ``strategy_positions`` row via ``_create_position_from_fill()``.
          Returns ``"strict"``.
        - ``tick.size < min_fill_size`` → **loose fill only**: log loose row,
          order stays resting.  Returns ``"loose"``.

        Returns:
            ``"strict"``, ``"loose"``, or ``None`` (no fill).
        """
        row = self._conn.execute(
            "SELECT * FROM paper_limit_orders WHERE order_id = ?", (order_id,)
        ).fetchone()
        if row is None:
            return None

        if row["state"] != "resting":
            return None

        if tick.token_id != row["favorite_token_id"]:
            return None

        bid_price = float(row["bid_price"])
        if tick.price > bid_price:
            return None

        # Price is at or below bid — evaluate size
        fill_price = tick.price
        slippage = round(bid_price - fill_price, 6)
        now = _now_iso()
        min_fill_size = float(row["min_fill_size"])
        strategy_id = int(row["strategy_id"])
        condition_id = str(row["condition_id"])

        if tick.size >= min_fill_size:
            # --- Strict fill ---
            for label in ("strict", "loose"):
                self._insert_fill(
                    order_id=order_id,
                    label=label,
                    fill_price=fill_price,
                    trade_size=tick.size,
                    trade_side=tick.side,
                    token_id=tick.token_id,
                    bid_price=bid_price,
                    slippage=slippage,
                    trade_ts=str(tick.timestamp_ms),
                    now=now,
                )

            # Transition order to 'filled' — atomic guard prevents race
            # with concurrent cancel/expire that may have already transitioned
            # the order out of 'resting'.
            cur = self._conn.execute(
                """
                UPDATE paper_limit_orders
                SET state = 'filled', updated_at = ?, closed_at = ?
                WHERE order_id = ? AND state = 'resting'
                """,
                (now, now, order_id),
            )
            self._conn.commit()
            if cur.rowcount == 0:
                logger.warning(
                    "fill_skipped order_id=%d — no longer resting (race with cancel/expire)",
                    order_id,
                )
                return None

            # Create strategy position
            self._create_position_from_fill(
                order_id=order_id,
                strategy_id=strategy_id,
                condition_id=condition_id,
                token_id=tick.token_id,
                outcome=str(row["favorite_token_label"]),
                fill_price=fill_price,
                size_usdc=position_size_usdc,
            )

            fill_details = {
                "trade_price": fill_price,
                "trade_size": tick.size,
                "trade_side": tick.side,
                "trade_ts": str(tick.timestamp_ms),
                "bid_price": bid_price,
                "slippage": slippage,
            }
            self._log_event(
                order_id=order_id,
                event_type="fill_strict",
                state_before="resting",
                state_after="filled",
                details=fill_details,
            )
            logger.info(
                "fill_strict order_id=%d condition_id=%s fill_price=%.4f size=%.2f slippage=%.4f",
                order_id, condition_id, fill_price, tick.size, slippage,
            )
            return "strict"

        else:
            # --- Loose fill only ---
            self._insert_fill(
                order_id=order_id,
                label="loose",
                fill_price=fill_price,
                trade_size=tick.size,
                trade_side=tick.side,
                token_id=tick.token_id,
                bid_price=bid_price,
                slippage=slippage,
                trade_ts=str(tick.timestamp_ms),
                now=now,
            )

            fill_details = {
                "trade_price": fill_price,
                "trade_size": tick.size,
                "trade_side": tick.side,
                "trade_ts": str(tick.timestamp_ms),
                "bid_price": bid_price,
                "slippage": slippage,
                "note": "sensitivity_gap: size below min_fill_size",
            }
            self._log_event(
                order_id=order_id,
                event_type="fill_loose",
                state_before="resting",
                state_after="resting",
                details=fill_details,
            )
            logger.info(
                "fill_loose order_id=%d condition_id=%s fill_price=%.4f size=%.2f "
                "(below min_fill_size=%.2f)",
                order_id, condition_id, fill_price, tick.size, min_fill_size,
            )
            return "loose"

    # ------------------------------------------------------------------
    # Public API — helpers
    # ------------------------------------------------------------------

    def count_resting(self, strategy_id: int) -> int:
        """Return the number of resting orders for a strategy."""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM paper_limit_orders WHERE strategy_id = ? AND state = 'resting'",
            (strategy_id,),
        ).fetchone()
        return int(row[0])

    def count_daily_fills(self, strategy_id: int) -> int:
        """Return the number of strict fills for a strategy today (UTC calendar day)."""
        today = datetime.now(timezone.utc).date().isoformat()  # e.g. "2026-04-06"
        row = self._conn.execute(
            """
            SELECT COUNT(*) FROM paper_limit_fills pf
            JOIN paper_limit_orders po ON pf.order_id = po.order_id
            WHERE po.strategy_id = ?
              AND pf.fill_label = 'strict'
              AND pf.created_at >= ?
            """,
            (strategy_id, today),
        ).fetchone()
        return int(row[0])

    def get_resting_orders(self, strategy_id: int | None = None) -> list[dict]:
        """Return all resting orders, optionally filtered by strategy.

        Args:
            strategy_id: If provided, only return orders for this strategy.

        Returns:
            List of order dicts (column-name keyed).
        """
        if strategy_id is not None:
            rows = self._conn.execute(
                "SELECT * FROM paper_limit_orders WHERE state = 'resting' AND strategy_id = ?",
                (strategy_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM paper_limit_orders WHERE state = 'resting'"
            ).fetchall()
        return [dict(r) for r in rows]

    def has_order_for_match(self, strategy_id: int, condition_id: str) -> bool:
        """Return True if any order (any state) exists for this match."""
        row = self._conn.execute(
            "SELECT 1 FROM paper_limit_orders WHERE strategy_id = ? AND condition_id = ? LIMIT 1",
            (strategy_id, condition_id),
        ).fetchone()
        return row is not None

    def log_skip(
        self,
        strategy_id: int,
        condition_id: str,
        reason: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Log an ``order_skipped`` event (no order_id — placement was never attempted).

        Args:
            strategy_id: ID of the owning strategy.
            condition_id: Market/condition that was skipped.
            reason: Reason code (see spec: ``max_resting_reached``, etc.).
            details: Optional extra context dict merged into the event details.
        """
        payload: dict[str, Any] = {
            "reason": reason,
            "strategy_id": strategy_id,
            "condition_id": condition_id,
        }
        if details:
            payload.update(details)
        self._log_event(
            order_id=None,
            event_type="order_skipped",
            state_before="",
            state_after="",
            details=payload,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _create_position_from_fill(
        self,
        order_id: int,
        strategy_id: int,
        condition_id: str,
        token_id: str,
        outcome: str,
        fill_price: float,
        size_usdc: float,
    ) -> int | None:
        """Insert a row into ``strategy_positions`` to represent the paper fill.

        The wallet uses the synthetic prefix ``paper:stink_bid:<strategy_id>``
        to avoid colliding with real wallets on the unique partial index
        ``uq_strategy_positions_active_owner`` (wallet, condition_id, outcome)
        WHERE state IN ('pending_entry', 'open', 'pending_exit').

        ``current_price`` is set to ``fill_price`` (not 0) so the existing
        resolution detector does not immediately fire a ``resolution_loss``.

        Args:
            order_id: The paper limit order that triggered this fill.
            strategy_id: Strategy ID for labelling.
            condition_id: Market condition ID.
            token_id: Token ID of the purchased outcome.
            outcome: Human label (e.g. "Player A").
            fill_price: Actual fill price.
            size_usdc: USDC notional of the paper position.

        Returns:
            The new ``position_id``, or ``None`` on error.
        """
        if fill_price <= 0:
            logger.error(
                "_create_position_from_fill: fill_price must be > 0 order_id=%d condition_id=%s",
                order_id, condition_id,
            )
            return None

        wallet = f"paper:stink_bid:{strategy_id}"
        entry_shares = size_usdc / fill_price
        now_ts = _now_ts()
        now_iso = _now_iso()

        try:
            cur = self._conn.execute(
                """
                INSERT INTO strategy_positions (
                    wallet, condition_id, outcome, strategy_id, state,
                    token_id, entry_price, avg_entry_price,
                    entry_shares, filled_entry_shares,
                    current_price, size_usdc,
                    opened_ts, updated_ts,
                    instance_tag, version
                ) VALUES (
                    ?, ?, ?, ?, 'open',
                    ?, ?, ?,
                    ?, ?,
                    ?, ?,
                    ?, ?,
                    'stink-bid-paper', 1
                )
                """,
                (
                    wallet, condition_id, outcome, str(strategy_id),
                    token_id, fill_price, fill_price,
                    entry_shares, entry_shares,
                    fill_price, size_usdc,
                    now_ts, now_ts,
                ),
            )
            self._conn.commit()
            position_id = int(cur.lastrowid or 0)
        except sqlite3.Error:
            logger.exception(
                "_create_position_from_fill failed order_id=%d condition_id=%s",
                order_id, condition_id,
            )
            return None

        self._log_event(
            order_id=order_id,
            event_type="position_opened",
            state_before="",
            state_after="open",
            details={
                "position_id": position_id,
                "wallet": wallet,
                "condition_id": condition_id,
                "entry_price": fill_price,
                "size_usdc": size_usdc,
                "entry_shares": entry_shares,
            },
        )
        logger.info(
            "position_opened position_id=%d order_id=%d condition_id=%s "
            "entry_price=%.4f size_usdc=%.2f",
            position_id, order_id, condition_id, fill_price, size_usdc,
        )
        return position_id

    def _insert_fill(
        self,
        order_id: int,
        label: str,
        fill_price: float,
        trade_size: float,
        trade_side: str,
        token_id: str,
        bid_price: float,
        slippage: float,
        trade_ts: str,
        now: str,
    ) -> None:
        """Insert a single row into ``paper_limit_fills``."""
        self._conn.execute(
            """
            INSERT INTO paper_limit_fills (
                order_id, fill_label, trade_price, trade_size,
                trade_side, trade_token_id, trade_ts,
                bid_price, slippage, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id, label, fill_price, trade_size,
                trade_side, token_id, trade_ts,
                bid_price, slippage, now,
            ),
        )
        self._conn.commit()

    def _log_event(
        self,
        order_id: int | None,
        event_type: str,
        state_before: str,
        state_after: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Append a row to ``paper_limit_events``."""
        try:
            self._conn.execute(
                """
                INSERT INTO paper_limit_events (
                    order_id, event_type, state_before, state_after,
                    details_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    event_type,
                    state_before,
                    state_after,
                    json.dumps(details or {}),
                    _now_iso(),
                ),
            )
            self._conn.commit()
        except sqlite3.OperationalError as exc:
            logger.warning(
                "paper_limit_events insert failed event_type=%s order_id=%s error=%s",
                event_type,
                order_id,
                exc,
            )
