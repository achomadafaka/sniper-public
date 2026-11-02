"""Shadow copy v1 — paper-only mirror of target-wallet entries.

Paper-only strategy plugin that consumes pre-populated wallet-trade
observations from ``shadow_copy_pending_signals`` and copies them into
``paper_trades`` when the trade matches a configured grain filter
(market_category, market_type, outcome_role, entry_price_range).

Decoupled from data source by design: a separate process / cron /
operator script writes new wallet-trade observations into
``shadow_copy_pending_signals``. This plugin reads + filters + decides
on entry; it never makes API calls.

Real-money execution is OUT OF SCOPE for v1. The plugin asserts
``execution_mode == "paper"`` in its entry decision.

Spec doctrine: per CLAUDE.md, this is the Path B (shadow-copy) path —
distinct from Path A (rule extraction via wallet_parameter_extractor).
v1 hard-codes the safety gates; later versions can read from
``web_settings`` once the surface is proven.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
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


PLUGIN_ID = "shadow_copy_v1"
SURFACE_VERSION = "shadow_copy_v1"

DEFAULTS: Dict[str, Any] = {
    "target_wallets": [],
    "market_filter": {
        "categories": [],
        "market_types": [],
        "outcome_roles": [],
        "entry_price_range": None,
    },
    "max_position_size_usdc": 25.0,
    "min_position_size_usdc": 5.0,
    "max_concurrent": 5,
    "min_wallet_position_size_usdc": 100.0,
    "max_observation_age_minutes": 30,
    "max_hold_days": 90,
}


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def ensure_shadow_copy_pending_signals_table(conn: sqlite3.Connection) -> None:
    """Create the pending-signals queue and processed-watermark.

    `shadow_copy_pending_signals` holds operator-fed wallet-trade
    observations. The plugin consumes rows where status='pending';
    status='processed' indicates the plugin has emitted a signal (or
    rejected the row), so the same observation isn't re-emitted.

    Schema:
      id              INTEGER PRIMARY KEY AUTOINCREMENT
      wallet          TEXT NOT NULL          (lowercase)
      condition_id    TEXT NOT NULL
      asset_id        TEXT NOT NULL
      outcome         TEXT NOT NULL          (YES / NO / named)
      side            TEXT NOT NULL          (BUY / SELL)
      price           REAL NOT NULL          (entry price observed)
      size_usdc       REAL NOT NULL          (target wallet's notional)
      market_category TEXT NOT NULL          (sports / elections / finance / ...)
      market_type     TEXT NOT NULL          (binary / multi_outcome / unknown)
      outcome_role    TEXT NOT NULL          (binary_yes / binary_no / binary_named_outcome / multi_outcome_selected)
      title           TEXT NOT NULL
      end_date_iso    TEXT                    (optional market resolution time)
      observed_ts     INTEGER NOT NULL       (unix epoch — when the target wallet's fill happened)
      created_ts      INTEGER NOT NULL       (unix epoch — when the row was inserted)
      status          TEXT NOT NULL          (pending / processed / rejected)
      reject_reason   TEXT                    (filled when status='rejected')
      strategy_id     INTEGER                  (filled when status='processed' and signal emitted)
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS shadow_copy_pending_signals (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet          TEXT NOT NULL,
            condition_id    TEXT NOT NULL,
            asset_id        TEXT NOT NULL,
            outcome         TEXT NOT NULL,
            side            TEXT NOT NULL,
            price           REAL NOT NULL,
            size_usdc       REAL NOT NULL,
            market_category TEXT NOT NULL,
            market_type     TEXT NOT NULL,
            outcome_role    TEXT NOT NULL,
            title           TEXT NOT NULL,
            end_date_iso    TEXT,
            observed_ts     INTEGER NOT NULL,
            created_ts      INTEGER NOT NULL,
            status          TEXT NOT NULL,
            reject_reason   TEXT,
            strategy_id     INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_shadow_copy_pending_status
          ON shadow_copy_pending_signals(status, created_ts)
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_shadow_copy_pending_obs
          ON shadow_copy_pending_signals(wallet, condition_id, asset_id, observed_ts, side)
        """
    )
    conn.commit()


def insert_pending_signal(
    conn: sqlite3.Connection,
    *,
    wallet: str,
    condition_id: str,
    asset_id: str,
    outcome: str,
    side: str,
    price: float,
    size_usdc: float,
    market_category: str,
    market_type: str,
    outcome_role: str,
    title: str,
    observed_ts: int,
    end_date_iso: str | None = None,
) -> int | None:
    """Insert a pending observation. Returns rowid or None on dedup conflict."""
    ensure_shadow_copy_pending_signals_table(conn)
    try:
        cur = conn.execute(
            """
            INSERT INTO shadow_copy_pending_signals
              (wallet, condition_id, asset_id, outcome, side, price, size_usdc,
               market_category, market_type, outcome_role, title, end_date_iso,
               observed_ts, created_ts, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                wallet.lower().strip(),
                str(condition_id),
                str(asset_id),
                str(outcome).upper(),
                str(side).upper(),
                float(price),
                float(size_usdc),
                str(market_category),
                str(market_type),
                str(outcome_role),
                str(title),
                end_date_iso,
                int(observed_ts),
                int(time.time()),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    except sqlite3.IntegrityError:
        return None


def mark_pending_processed(
    conn: sqlite3.Connection,
    pending_id: int,
    *,
    strategy_id: int,
) -> None:
    conn.execute(
        """
        UPDATE shadow_copy_pending_signals
        SET status='processed', strategy_id=?
        WHERE id=?
        """,
        (int(strategy_id), int(pending_id)),
    )
    conn.commit()


def mark_pending_rejected(
    conn: sqlite3.Connection,
    pending_id: int,
    *,
    reason: str,
) -> None:
    conn.execute(
        """
        UPDATE shadow_copy_pending_signals
        SET status='rejected', reject_reason=?
        WHERE id=?
        """,
        (str(reason)[:200], int(pending_id)),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_params(strategy: Dict[str, Any]) -> Dict[str, Any]:
    raw = strategy.get("params") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    merged = {**DEFAULTS, **raw}
    # Deep-merge market_filter so partial params don't clobber defaults
    mf_defaults = dict(DEFAULTS["market_filter"])
    mf_raw = dict(raw.get("market_filter") or {})
    mf_defaults.update(mf_raw)
    merged["market_filter"] = mf_defaults
    return merged


def _matches_market_filter(row: Dict[str, Any], market_filter: Dict[str, Any]) -> tuple[bool, str]:
    """Returns (passes, reason_if_fail)."""
    categories = market_filter.get("categories") or []
    if categories and str(row.get("market_category") or "") not in categories:
        return False, f"market_category={row.get('market_category')} not in {categories}"

    market_types = market_filter.get("market_types") or []
    if market_types and str(row.get("market_type") or "") not in market_types:
        return False, f"market_type={row.get('market_type')} not in {market_types}"

    outcome_roles = market_filter.get("outcome_roles") or []
    if outcome_roles and str(row.get("outcome_role") or "") not in outcome_roles:
        return False, f"outcome_role={row.get('outcome_role')} not in {outcome_roles}"

    price_range = market_filter.get("entry_price_range")
    if price_range and len(price_range) == 2:
        price = float(row.get("price") or 0.0)
        lo, hi = float(price_range[0]), float(price_range[1])
        if not (lo <= price < hi):
            return False, f"price={price} outside [{lo},{hi})"

    return True, ""


def _already_copied(
    conn: sqlite3.Connection,
    *,
    strategy_id: int,
    wallet: str,
    condition_id: str,
    asset_id: str,
) -> bool:
    """Return True iff an earlier pending row for this (wallet, condition, asset)
    was already emitted by this strategy (status='processed')."""
    row = conn.execute(
        """
        SELECT 1 FROM shadow_copy_pending_signals
        WHERE wallet=? AND condition_id=? AND asset_id=?
          AND strategy_id=? AND status='processed'
        LIMIT 1
        """,
        (wallet.lower(), str(condition_id), str(asset_id), int(strategy_id)),
    ).fetchone()
    return row is not None


def _open_position_count(conn: sqlite3.Connection, strategy_id: int) -> int:
    """Count open strategy_positions owned by this strategy. Falls back to 0
    if the table doesn't exist (e.g. minimal test DB)."""
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM strategy_positions WHERE strategy_id=? AND state='open'",
            (int(strategy_id),),
        ).fetchone()
        return int(row[0]) if row else 0
    except sqlite3.OperationalError:
        return 0


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------


class ShadowCopyV1Strategy(StrategyPlugin):
    plugin_id = PLUGIN_ID

    def discover(
        self,
        conn: sqlite3.Connection,
        strategy: Dict[str, Any],
    ) -> List[StrategySignal]:
        ensure_shadow_copy_pending_signals_table(conn)
        params = _parse_params(strategy)
        strategy_id = int(strategy.get("id") or 0)

        target_wallets = {
            str(w).lower().strip()
            for w in (params.get("target_wallets") or [])
            if w
        }
        if not target_wallets:
            return []

        max_age_seconds = int(params.get("max_observation_age_minutes") or 30) * 60
        min_wallet_size = float(params.get("min_wallet_position_size_usdc") or 0.0)
        market_filter = params.get("market_filter") or {}
        now = int(time.time())

        placeholders = ",".join("?" for _ in target_wallets)
        rows = conn.execute(
            f"""
            SELECT id, wallet, condition_id, asset_id, outcome, side, price,
                   size_usdc, market_category, market_type, outcome_role, title,
                   end_date_iso, observed_ts
            FROM shadow_copy_pending_signals
            WHERE status='pending'
              AND wallet IN ({placeholders})
              AND side='BUY'
            ORDER BY observed_ts DESC
            """,
            tuple(target_wallets),
        ).fetchall()

        signals: List[StrategySignal] = []
        for row in rows:
            row_d = dict(row) if hasattr(row, "keys") else {
                "id": row[0], "wallet": row[1], "condition_id": row[2], "asset_id": row[3],
                "outcome": row[4], "side": row[5], "price": row[6], "size_usdc": row[7],
                "market_category": row[8], "market_type": row[9], "outcome_role": row[10],
                "title": row[11], "end_date_iso": row[12], "observed_ts": row[13],
            }

            age = now - int(row_d["observed_ts"])
            if age > max_age_seconds:
                mark_pending_rejected(conn, row_d["id"], reason=f"observation_too_old:{age}s")
                continue

            if float(row_d["size_usdc"] or 0.0) < min_wallet_size:
                mark_pending_rejected(
                    conn, row_d["id"],
                    reason=f"wallet_position_below_min:{row_d['size_usdc']}",
                )
                continue

            passes, reason = _matches_market_filter(row_d, market_filter)
            if not passes:
                mark_pending_rejected(conn, row_d["id"], reason=f"filter:{reason}")
                continue

            if strategy_id and _already_copied(
                conn,
                strategy_id=strategy_id,
                wallet=row_d["wallet"],
                condition_id=row_d["condition_id"],
                asset_id=row_d["asset_id"],
            ):
                mark_pending_rejected(conn, row_d["id"], reason="already_copied")
                continue

            signals.append(
                StrategySignal(
                    wallet=str(row_d["wallet"]),
                    condition_id=str(row_d["condition_id"]),
                    outcome=str(row_d["outcome"]),
                    title=str(row_d["title"]),
                    current_price=float(row_d["price"] or 0.0),
                    position_value=float(row_d["size_usdc"] or 0.0),
                    analyzed_ts=int(row_d["observed_ts"] or 0),
                    metadata={
                        "pending_id": row_d["id"],
                        "asset_id": str(row_d["asset_id"] or ""),
                        "market_category": str(row_d["market_category"] or ""),
                        "market_type": str(row_d["market_type"] or ""),
                        "outcome_role": str(row_d["outcome_role"] or ""),
                        "end_date_iso": str(row_d["end_date_iso"] or ""),
                        "target_wallet_size_usdc": float(row_d["size_usdc"] or 0.0),
                    },
                )
            )

        return signals

    def enter(
        self,
        conn: sqlite3.Connection,
        strategy: Dict[str, Any],
        signal: StrategySignal,
        open_positions: List[Dict[str, Any]],
    ) -> StrategyEntryDecision:
        # Signature matches the StrategyPlugin ABC and the strategy_runner call
        # site enter(conn, strategy, signal, open_positions). `open_positions`
        # is accepted for ABC compliance; v1 reads the live open count from
        # strategy_positions (DB) for the concurrency gate.
        params = _parse_params(strategy)
        strategy_id = int(strategy.get("id") or 0)

        max_concurrent = int(params.get("max_concurrent") or 5)
        max_size = float(params.get("max_position_size_usdc") or 25.0)
        min_size = float(params.get("min_position_size_usdc") or 5.0)

        if strategy_id and _open_position_count(conn, strategy_id) >= max_concurrent:
            return StrategyEntryDecision(
                should_enter=False,
                reason=f"max_concurrent={max_concurrent}_reached",
            )

        # v1 sizing: fixed clamp. Future versions can scale by target wallet size.
        size = max(min_size, min(max_size, max_size))

        # Mark the source pending row processed so the dedup guard
        # (_already_copied → status='processed') fires on later cycles. Without
        # this a re-buy by the target wallet produces a duplicate paper trade.
        pending_id = signal.metadata.get("pending_id")
        if strategy_id and pending_id is not None:
            mark_pending_processed(conn, int(pending_id), strategy_id=strategy_id)

        return StrategyEntryDecision(
            should_enter=True,
            reason="shadow_copy_grain_match",
            size_usdc=float(size),
            entry_price=float(signal.current_price),
            execution_mode="paper",
            metadata={
                "pending_id": pending_id,
                "target_wallet": signal.wallet,
                "market_category": signal.metadata.get("market_category"),
                "source_size_usdc": signal.metadata.get("target_wallet_size_usdc"),
            },
        )

    def manage(
        self,
        conn: sqlite3.Connection,
        strategy: Dict[str, Any],
        position: Dict[str, Any],
    ) -> StrategyManageState:
        current_price = position.get("current_price")
        return StrategyManageState(
            current_price=float(current_price) if current_price is not None else None,
            signal_open=True,
        )

    def exit(
        self,
        conn: sqlite3.Connection,
        strategy: Dict[str, Any],
        position: Dict[str, Any],
        state: StrategyManageState | None = None,
    ) -> StrategyExitDecision:
        """Hold to resolution by default; bail on max_hold_days.

        `state` matches the StrategyPlugin.exit ABC + strategy_runner call site
        exit(conn, strategy, position, manage_state); v1 decides from `position`.
        """
        params = _parse_params(strategy)
        current_price = float(position.get("current_price") or 0.0)

        if current_price >= RESOLUTION_WIN:
            return StrategyExitDecision(
                should_exit=True,
                reason="resolution_win",
                exit_price=current_price,
                status="resolution_win",
            )
        if current_price <= RESOLUTION_LOSS:
            return StrategyExitDecision(
                should_exit=True,
                reason="resolution_loss",
                exit_price=current_price,
                status="resolution_loss",
            )

        # strategy_positions stores `opened_ts`; accept the legacy keys too.
        opened_at = (
            position.get("opened_ts")
            or position.get("opened_at_ts")
            or position.get("opened_at")
        )
        max_days = float(params.get("max_hold_days") or 90)
        if opened_at:
            try:
                opened_ts = int(opened_at)
            except (TypeError, ValueError):
                opened_ts = 0
            if opened_ts and (time.time() - opened_ts) > max_days * 86400:
                return StrategyExitDecision(
                    should_exit=True,
                    reason="max_hold_days_exceeded",
                    exit_price=current_price,
                    status="early_exit",
                )

        return StrategyExitDecision(should_exit=False, reason="holding")
