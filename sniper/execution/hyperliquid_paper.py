"""Minimal Hyperliquid paper execution engine.

Records paper trades in hl_positions + hl_fills. Computes PnL from
entry/exit prices. Handles forced liquidation.

PnL model: size is contracts/coins. PnL = size * price_delta * direction.
No leverage multiplier on PnL — size already reflects leveraged exposure.
Fee: 0.045% taker per fill (entry + exit), matching HL base taker rate.

NOT in v1: funding accumulation, VWAP averaging, partial fills.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Optional

logger = logging.getLogger(__name__)


def record_hl_entry(
    conn: sqlite3.Connection,
    *,
    strategy_id: int,
    asset: str,
    side: str,
    entry_price: float,
    size: float,
    leverage: float,
    margin_usdc: float,
    instance_tag: str = "",
) -> Optional[int]:
    """Record a new HL paper position.

    Returns position_id on success, None on failure.
    """
    if entry_price <= 0 or size <= 0:
        return None

    # Compute liquidation price
    if side == "long":
        liquidation_price = entry_price * (1 - 1 / leverage)
    else:
        liquidation_price = entry_price * (1 + 1 / leverage)

    now_ts = int(time.time())

    try:
        cur = conn.execute(
            """INSERT INTO hl_positions
               (strategy_id, asset, side, entry_price, avg_entry_price,
                current_price, size, leverage, margin_usdc,
                liquidation_price, state, opened_ts, updated_ts, instance_tag)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)""",
            (strategy_id, asset, side, entry_price, entry_price,
             entry_price, size, leverage, margin_usdc,
             liquidation_price, now_ts, now_ts, instance_tag),
        )
        position_id = cur.lastrowid

        # Record opening fill (I28: record entry fee for accurate per-fill accounting)
        entry_fee = round(entry_price * size * 0.00045, 4)  # 0.045% taker on entry notional
        conn.execute(
            """INSERT INTO hl_fills
               (position_id, fill_type, asset, side, price, size, fee_usdc, time_ms)
               VALUES (?, 'open', ?, ?, ?, ?, ?, ?)""",
            (position_id, asset, side, entry_price, size, entry_fee, now_ts * 1000),
        )

        conn.commit()
        logger.info(
            "HL paper entry: pos=%d %s %s size=%.6f entry=$%.2f lev=%.1fx liq=$%.2f",
            position_id, asset, side, size, entry_price, leverage, liquidation_price,
        )
        return position_id
    except Exception:
        logger.exception("record_hl_entry failed")
        conn.rollback()
        return None


def update_hl_position(
    conn: sqlite3.Connection,
    position_id: int,
    mark_price: float,
) -> dict:
    """Mark-to-market an open HL position.

    Returns dict with {unrealized_pnl, is_liquidated, current_price}.
    """
    row = conn.execute(
        "SELECT * FROM hl_positions WHERE id = ? AND state = 'open'",
        (position_id,),
    ).fetchone()
    if not row:
        return {"unrealized_pnl": 0, "is_liquidated": False, "current_price": 0}

    entry_price = float(row["entry_price"])
    size = float(row["size"])
    side = row["side"]
    liquidation_price = float(row["liquidation_price"])

    direction = 1 if side == "long" else -1
    unrealized_pnl = (mark_price - entry_price) * size * direction

    # Check forced liquidation
    is_liquidated = False
    if side == "long" and mark_price <= liquidation_price:
        is_liquidated = True
    elif side == "short" and mark_price >= liquidation_price:
        is_liquidated = True

    conn.execute(
        """UPDATE hl_positions
           SET current_price = ?, unrealized_pnl = ?, updated_ts = ?
           WHERE id = ?""",
        (mark_price, round(unrealized_pnl, 4), int(time.time()), position_id),
    )
    conn.commit()

    return {
        "unrealized_pnl": round(unrealized_pnl, 4),
        "is_liquidated": is_liquidated,
        "current_price": mark_price,
    }


def close_hl_position(
    conn: sqlite3.Connection,
    position_id: int,
    exit_price: float,
    reason: str = "",
) -> dict:
    """Close an HL paper position and compute realized PnL.

    Fee: 0.045% taker on entry + exit notional (HL base rate).
    Returns {realized_pnl, fee_usdc, exit_price}.
    """
    row = conn.execute(
        "SELECT * FROM hl_positions WHERE id = ? AND state = 'open'",
        (position_id,),
    ).fetchone()
    if not row:
        return {"realized_pnl": 0, "fee_usdc": 0, "exit_price": 0}

    entry_price = float(row["entry_price"])
    size = float(row["size"])
    side = row["side"]
    asset = row["asset"]

    direction = 1 if side == "long" else -1
    raw_pnl = (exit_price - entry_price) * size * direction

    # Fee: 0.045% taker on both entry and exit notional (HL base rate)
    entry_notional = size * entry_price
    exit_notional = size * exit_price
    fee_usdc = (entry_notional + exit_notional) * 0.00045
    realized_pnl = round(raw_pnl - fee_usdc, 4)

    now_ts = int(time.time())

    conn.execute(
        """UPDATE hl_positions
           SET state = 'closed', current_price = ?, realized_pnl = ?,
               closed_ts = ?, updated_ts = ?, close_reason = ?
           WHERE id = ?""",
        (exit_price, realized_pnl, now_ts, now_ts, reason, position_id),
    )

    # Record closing fill
    conn.execute(
        """INSERT INTO hl_fills
           (position_id, fill_type, asset, side, price, size, fee_usdc, time_ms)
           VALUES (?, 'close', ?, ?, ?, ?, ?, ?)""",
        (position_id, asset, side, exit_price, size, round(fee_usdc, 4), now_ts * 1000),
    )

    conn.commit()
    logger.info(
        "HL paper close: pos=%d %s %s exit=$%.2f pnl=$%.4f fee=$%.4f reason=%s",
        position_id, asset, side, exit_price, realized_pnl, fee_usdc, reason,
    )
    return {"realized_pnl": realized_pnl, "fee_usdc": round(fee_usdc, 4), "exit_price": exit_price}
