#!/usr/bin/env python3
"""
Strategy Runner Service for Sniper.

Runs first-class strategy plugins alongside the older prototype strategy types.
The plugin path uses its own strategy ledger (`strategy_positions`,
`strategy_executions`, `strategy_runs`) and does not touch the frozen
paper-trader benchmark tables.
"""

import argparse
import dataclasses
import os
import sys
import json
import sqlite3
import logging
import signal
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List, Any
import requests
from pathlib import Path

from db import (
    get_connection as shared_get_connection,
    init_db,
    sync_followed_wallet_membership,
)
from sniper.filters import days_to_resolution  # re-exported here; defined in filters to avoid circular import
from sniper.strategies.registry import get_strategy_plugin
from sniper.settings.instance_profile import load_instance_profile, InstanceProfile
from sniper.feeds.registry import get_feed_adapter
from sniper.risk.policy import evaluate_entry_risk
from sniper.risk.models import RiskContext

try:
    from sniper.persistence.db_hyperliquid import (
        insert_signal_queue_entry,
        update_signal_queue_status,
    )
    _HAS_SIGNAL_QUEUE = True
except ImportError:
    _HAS_SIGNAL_QUEUE = False

try:
    from sniper.execution.hyperliquid_paper import (
        record_hl_entry,
        update_hl_position,
        close_hl_position,
    )
    _HAS_HL_EXECUTION = True
except ImportError:
    _HAS_HL_EXECUTION = False


def _is_hl_plugin(plugin) -> bool:
    """Check if a strategy plugin is Hyperliquid-based (uses HL feeds)."""
    feeds = getattr(plugin, "required_feeds", ())
    return any(f in ("hl_whale", "hyperliquid_ws") for f in feeds)


def _active_hl_positions(
    conn: sqlite3.Connection,
    strategy_id: int,
) -> List[Dict[str, Any]]:
    """Load open HL positions for a strategy (reads hl_positions, NOT strategy_positions)."""
    rows = conn.execute(
        """SELECT * FROM hl_positions
           WHERE strategy_id = ? AND state = 'open'
           ORDER BY opened_ts DESC""",
        (strategy_id,),
    ).fetchall()
    return [dict_from_row(row) for row in rows]


def _build_hl_risk_context(
    conn: sqlite3.Connection,
    profile: Optional[InstanceProfile],
    asset: str,
) -> Optional["RiskContext"]:
    """Build a RiskContext from hl_positions with venue=hyperliquid.

    Exposure is summed portfolio-wide (all HL strategies), not per-strategy.
    This is deliberate: risk limits should reflect total HL exposure regardless
    of which strategy opened the position.
    """
    from sniper.risk.models import RiskContext
    if profile is None:
        return None

    row = conn.execute(
        "SELECT COALESCE(SUM(margin_usdc), 0) FROM hl_positions WHERE state = 'open'"
    ).fetchone()
    total_exposure = float(row[0] if row else 0.0)

    row = conn.execute(
        "SELECT COALESCE(SUM(margin_usdc), 0) FROM hl_positions WHERE state = 'open' AND asset = ?",
        (asset,),
    ).fetchone()
    asset_exposure = float(row[0] if row else 0.0)

    row = conn.execute(
        "SELECT COUNT(*) FROM hl_positions WHERE state = 'open'"
    ).fetchone()
    concurrent = int(row[0] if row else 0)

    today_start = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT COALESCE(SUM(realized_pnl), 0) FROM hl_positions WHERE state = 'closed' AND closed_ts >= ?",
        (int(datetime.strptime(today_start, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()),),
    ).fetchone()
    daily_pnl = float(row[0] if row else 0.0)

    return RiskContext(
        bankroll_usdc=profile.bankroll_usdc,
        total_exposure_usdc=total_exposure,
        market_exposure_usdc=asset_exposure,
        concurrent_positions=concurrent,
        daily_realized_pnl_usdc=daily_pnl,
        max_total_exposure_usdc=profile.hl_max_exposure_usdc if profile.hl_max_exposure_usdc > 0 else profile.max_total_exposure_usdc,
        max_market_exposure_usdc=profile.max_market_exposure_usdc,
        max_concurrent_positions=profile.hl_max_concurrent if profile.hl_max_concurrent > 0 else profile.max_concurrent_positions,
        daily_loss_limit_usdc=profile.daily_loss_limit_usdc,
        paper_only=profile.paper_only,
        real_trading_enabled=profile.real_trading_enabled,
        venue="hyperliquid",
    )

# Conviction engine — probabilistic EV filter (optional; passthrough if unavailable)
try:
    from conviction_engine import ConvictionEngine as _ConvictionEngine
    _CONVICTION_AVAILABLE = True
except ImportError:
    _ConvictionEngine = None  # type: ignore[assignment,misc]
    _CONVICTION_AVAILABLE = False

# Third-party imports
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType
except ImportError:
    ClobClient = None
    OrderArgs = None
    OrderType = None


# ============================================================================
# CONFIGURATION & LOGGING
# ============================================================================

SERVICE_DIR = Path(__file__).parent
DB_PATH = Path(os.getenv("DB_PATH", str(SERVICE_DIR / "sniper_tracker.db")))
PID_FILE = SERVICE_DIR / "strategy_runner.pid"
LOG_FILE = SERVICE_DIR / "strategy_runner.log"
WALLET_KEY_FILE = SERVICE_DIR / ".wallet_key"

API_KEY = os.getenv("POLYMARKET_API_KEY", "")
CLOB_HOST = "https://clob.polymarket.com"
GAMMA_API_HOST = "https://gamma-api.polymarket.com"
POLYMARKET_CHAIN_ID = 137

# Circuit breaker limits
DAILY_LOSS_LIMIT_USDC = 50
MAX_OPEN_REAL_POSITIONS = 10
MIN_USDC_BALANCE = 5

# Service loop timing
LOOP_INTERVAL_SECONDS = 60

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

# Global state
running = True
_last_run_ts: Dict[int, float] = {}  # strategy_id → last run timestamp (cadence tracking)
_last_feed_tick_ts: Dict[str, float] = {}  # feed_id → last tick timestamp


# ============================================================================
# SIGNAL HANDLERS
# ============================================================================

def handle_sigterm(signum, frame):
    """Handle SIGTERM for graceful shutdown."""
    global running
    logger.info(f"Received signal {signum}, initiating graceful shutdown...")
    running = False


def handle_sigint(signum, frame):
    """Handle SIGINT for graceful shutdown."""
    global running
    logger.info(f"Received signal {signum}, initiating graceful shutdown...")
    running = False


# ============================================================================
# DATABASE UTILITIES
# ============================================================================

def get_db_connection() -> sqlite3.Connection:
    """Get a database connection with WAL mode and timeout."""
    conn = shared_get_connection(str(DB_PATH))
    init_db(conn)
    return conn


def dict_from_row(row: sqlite3.Row) -> Dict[str, Any]:
    """Convert sqlite3.Row to dictionary."""
    if row is None:
        return None
    return dict(row)


def _json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _load_strategy(strategy_row: Dict[str, Any]) -> Dict[str, Any]:
    strategy = dict(strategy_row)
    strategy["parameters"] = _json_object(strategy.get("parameters"))
    return strategy


def _adversarial_gate_allows_entry(conn: sqlite3.Connection, idea_id: Any) -> bool:
    if not idea_id:
        return True
    try:
        from brain import get_trade_idea_review_gate

        allowed, reason = get_trade_idea_review_gate(conn, int(idea_id))
        if not allowed:
            logger.info("Adversarial gate: skipping idea %s — %s", idea_id, reason)
        return allowed
    except Exception:
        logger.exception("Adversarial gate check failed for idea %s", idea_id)
        return False


def _active_strategy_positions(
    conn: sqlite3.Connection,
    strategy_id: int,
) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM strategy_positions
        WHERE strategy_id = ?
          AND state IN ('pending_entry', 'open', 'pending_exit')
        ORDER BY opened_ts DESC, position_id DESC
        """,
        (str(strategy_id),),
    ).fetchall()
    return [dict_from_row(row) for row in rows]


def _all_strategy_positions(
    conn: sqlite3.Connection,
    strategy_id: int,
) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM strategy_positions
        WHERE strategy_id = ?
        ORDER BY updated_ts DESC, position_id DESC
        """,
        (str(strategy_id),),
    ).fetchall()
    return [dict_from_row(row) for row in rows]


def _global_open_condition_ids(
    conn: sqlite3.Connection,
) -> set:
    """Return set of (condition_id, outcome) pairs already open across ACTIVE strategies.

    Used as a cross-strategy dedup gate to prevent two different strategies
    from entering the same market/outcome simultaneously.

    Excludes positions belonging to stopped strategies — stopped strategies have
    abandoned their positions and should not block new entries in active strategies.
    Paused strategies are included: they may resume and their positions are still live.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT sp.condition_id, sp.outcome
        FROM strategy_positions sp
        JOIN strategies s ON s.id = CAST(sp.strategy_id AS INTEGER)
        WHERE sp.state IN ('pending_entry', 'open', 'pending_exit')
          AND s.status != 'stopped'
        """
    ).fetchall()
    result = {(row[0], str(row[1]).lower()) for row in rows}

    # Include open HL positions in the dedup set
    if _HAS_HL_EXECUTION:
        hl_rows = conn.execute(
            """
            SELECT DISTINCT hp.asset, hp.side
            FROM hl_positions hp
            JOIN strategies s ON s.id = hp.strategy_id
            WHERE hp.state = 'open'
              AND s.status != 'stopped'
            """
        ).fetchall()
        result.update((row[0], str(row[1]).lower()) for row in hl_rows)

    return result


_CONFIDENCE_WEIGHT: dict = {"high": 1.0, "medium": 0.7, "low": 0.4}


def _load_world_boosts() -> dict:
    """Load market_boosts_current from Supabase.

    Returns dict keyed by condition_id → {boost: int, reason: str, direction: str, confidence: str}
    Returns empty dict if unavailable or Supabase unreachable.
    Respects per-entry expires_hours relative to the record's updated_at timestamp.
    Applies confidence weighting: high=1.0, medium=0.7, low=0.4.
    """
    try:
        from bot.supabase_helper import get_memory
        from sniper.markets.polymarket_api import _parse_iso_ts
        m = get_memory("market_boosts_current", type_filter="knowledge")
        if not m:
            return {}
        content = m.get("content", "{}")
        raw = json.loads(content) if isinstance(content, str) else (content or {})

        # Determine when the boost record was written so we can honour expires_hours
        record_ts = float(_parse_iso_ts(m.get("updated_at", "")))

        now_ts = time.time()
        age_hours = (now_ts - record_ts) / 3600 if record_ts > 0 else 0.0
        result: dict = {}
        for k, v in raw.items():
            if not isinstance(v, dict):
                continue
            expires_h = v.get("expires_hours")
            if expires_h and record_ts > 0:
                if age_hours > float(expires_h):
                    continue  # boost has expired
            # Apply confidence weighting to the boost value
            conf = str(v.get("confidence", "medium")).lower()
            weight = _CONFIDENCE_WEIGHT.get(conf, 0.7)
            raw_boost = int(v.get("boost", 0))
            effective_boost = int(round(raw_boost * weight))
            boosted_entry = dict(v)
            boosted_entry["boost"] = effective_boost
            boosted_entry["_raw_boost"] = raw_boost
            boosted_entry["_confidence_weight"] = weight
            result[k.lower()] = boosted_entry
        return result
    except Exception:
        return {}


def _load_conviction_engine() -> Optional["_ConvictionEngine"]:
    """Instantiate and load ConvictionEngine from Supabase brain.

    Called once per service cycle (same pattern as _load_world_boosts).
    Returns None if conviction_engine module not available or brain load fails —
    the caller treats None as passthrough (no filtering).
    """
    if not _CONVICTION_AVAILABLE or _ConvictionEngine is None:
        return None
    engine = _ConvictionEngine()
    engine.load_from_brain()
    # Even if load fails, return the engine (it will self-report passthrough)
    return engine


def _insert_strategy_run(
    conn: sqlite3.Connection,
    strategy_id: int,
    *,
    instance_tag: str = "",
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO strategy_runs (strategy_id, status, started_at, details_json, instance_tag)
        VALUES (?, 'running', ?, '{}', ?)
        """,
        (strategy_id, _utc_now_iso(), instance_tag),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def _finish_strategy_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    discovered_count: int,
    entered_count: int,
    exited_count: int,
    open_positions: int,
    realized_pnl: float,
    unrealized_pnl: float,
    error_msg: str = "",
    details: Dict[str, Any] | None = None,
) -> None:
    details_json = "{}"
    if details:
        try:
            details_json = json.dumps(details, sort_keys=True)
        except Exception:
            details_json = "{}"
    conn.execute(
        """
        UPDATE strategy_runs
        SET status = ?,
            finished_at = ?,
            discovered_count = ?,
            entered_count = ?,
            exited_count = ?,
            open_positions = ?,
            realized_pnl = ?,
            unrealized_pnl = ?,
            error_msg = ?,
            details_json = ?
        WHERE run_id = ?
        """,
        (
            status,
            _utc_now_iso(),
            int(discovered_count),
            int(entered_count),
            int(exited_count),
            int(open_positions),
            float(realized_pnl or 0.0),
            float(unrealized_pnl or 0.0),
            str(error_msg or ""),
            details_json,
            int(run_id),
        ),
    )
    conn.commit()


def _record_plugin_entry(
    conn: sqlite3.Connection,
    strategy: Dict[str, Any],
    signal,
    decision,
    *,
    instance_tag: str = "",
    max_concurrent: int = 0,
) -> bool:
    strategy_id = int(strategy["id"])
    strategy_key = str(strategy_id)
    wallet = str(signal.wallet or "").strip().lower()
    condition_id = str(signal.condition_id or "").strip().lower()
    outcome = str(signal.outcome or "").strip().upper()
    size_usdc = float(decision.size_usdc or 0.0)
    entry_price = float(decision.entry_price or signal.current_price or 0.0)
    if size_usdc <= 0 or entry_price <= 0:
        return False

    execution_mode = str(decision.execution_mode or "paper").strip().lower()
    if execution_mode != "paper":
        logger.warning(
            "Strategy %s requested unsupported execution_mode=%s; skipping entry",
            strategy_id,
            execution_mode,
        )
        return False

    shares = size_usdc / entry_price
    now_ts = int(time.time())
    now_iso = _utc_now_iso()
    # Extract end_date_iso from signal metadata (populated by discover() for all plugins)
    end_date_iso = str(signal.metadata.get("end_date_iso") or "") if signal.metadata else ""
    entry_insider_score = float(signal.metadata.get("position_insider_score") or 0.0) if signal.metadata else 0.0
    try:
        if max_concurrent > 0:
            # Atomic guard: the WHERE is evaluated under SQLite's write lock so the
            # COUNT check and INSERT are a single serialized operation — no TOCTOU race
            # even when two runner processes run simultaneously.
            cur = conn.execute(
                """
                INSERT INTO strategy_positions (
                    wallet,
                    condition_id,
                    outcome,
                    strategy_id,
                    state,
                    entry_price,
                    avg_entry_price,
                    entry_shares,
                    filled_entry_shares,
                    realized_pnl,
                    opened_ts,
                    closed_ts,
                    last_reconciled_ts,
                    version,
                    updated_ts,
                    instance_tag,
                    end_date_iso,
                    entry_insider_score,
                    size_usdc
                )
                SELECT ?, ?, ?, ?, 'open', ?, ?, ?, ?, 0, ?, 0, ?, 1, ?, ?, ?, ?, ?
                WHERE (
                    SELECT COUNT(*) FROM strategy_positions
                    WHERE strategy_id = ? AND state = 'open'
                ) < ?
                AND NOT EXISTS (
                    SELECT 1 FROM strategy_positions
                    WHERE strategy_id = ? AND condition_id = ? AND outcome = ? AND state = 'open'
                )
                """,
                (
                    wallet,
                    condition_id,
                    outcome,
                    strategy_key,
                    entry_price,
                    entry_price,
                    shares,
                    shares,
                    now_ts,
                    now_ts,
                    now_ts,
                    instance_tag,
                    end_date_iso,
                    entry_insider_score,
                    size_usdc,
                    strategy_key,
                    max_concurrent,
                    strategy_key,
                    condition_id,
                    outcome,
                ),
            )
            if cur.rowcount == 0:
                logger.debug(
                    "Atomic max_concurrent guard: INSERT skipped (race or at capacity) strategy=%s max=%d",
                    strategy_id,
                    max_concurrent,
                )
                return False
        else:
            cur = conn.execute(
                """
                INSERT INTO strategy_positions (
                    wallet,
                    condition_id,
                    outcome,
                    strategy_id,
                    state,
                    entry_price,
                    avg_entry_price,
                    entry_shares,
                    filled_entry_shares,
                    realized_pnl,
                    opened_ts,
                    closed_ts,
                    last_reconciled_ts,
                    version,
                    updated_ts,
                    instance_tag,
                    end_date_iso,
                    entry_insider_score,
                    size_usdc
                )
                SELECT ?, ?, ?, ?, 'open', ?, ?, ?, ?, 0, ?, 0, ?, 1, ?, ?, ?, ?, ?
                WHERE NOT EXISTS (
                    SELECT 1 FROM strategy_positions
                    WHERE strategy_id = ? AND condition_id = ? AND outcome = ? AND state = 'open'
                )
                """,
                (
                    wallet,
                    condition_id,
                    outcome,
                    strategy_key,
                    entry_price,
                    entry_price,
                    shares,
                    shares,
                    now_ts,
                    now_ts,
                    now_ts,
                    instance_tag,
                    end_date_iso,
                    entry_insider_score,
                    size_usdc,
                    strategy_key,
                    condition_id,
                    outcome,
                ),
            )
        if cur.rowcount == 0:
            logger.debug(
                "Duplicate open-position guard: INSERT skipped strategy=%s condition=%s outcome=%s",
                strategy_id, condition_id, outcome,
            )
            return False
        position_id = int(cur.lastrowid or 0)
    except sqlite3.IntegrityError:
        return False

    conn.execute(
        """
        INSERT INTO strategy_executions (
            strategy_id, wallet, condition_id, outcome, title,
            execution_type, size_usdc, entry_price, token_id, order_id,
            status, pnl_usdc, error_msg, created_at, updated_at,
            instance_tag
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', '', 'open', 0, ?, ?, ?, ?)
        """,
        (
            strategy_id,
            wallet,
            condition_id,
            outcome,
            str(signal.title or ""),
            execution_mode,
            size_usdc,
            entry_price,
            "",
            now_iso,
            now_iso,
            instance_tag,
        ),
    )
    conn.execute(
        """
        UPDATE strategies
        SET trades_triggered = trades_triggered + 1,
            updated_at = ?
        WHERE id = ?
        """,
        (now_iso, strategy_id),
    )
    conn.commit()
    logger.info(
        "Strategy %s entered %s %s %s @ %.3f size %.2f",
        strategy_id,
        wallet,
        condition_id,
        outcome,
        entry_price,
        size_usdc,
    )
    return position_id > 0


def _update_position_touch(
    conn: sqlite3.Connection,
    position_id: int,
    current_price: float | None = None,
    current_value: float | None = None,
    pnl_usdc: float | None = None,
) -> None:
    now_ts = int(time.time())
    if current_price is not None:
        conn.execute(
            """
            UPDATE strategy_positions
            SET last_reconciled_ts = ?,
                updated_ts = ?,
                current_price = ?,
                current_value = ?,
                pnl_usdc = ?,
                version = version + 1
            WHERE position_id = ?
            """,
            (now_ts, now_ts, current_price, current_value, pnl_usdc, int(position_id)),
        )
    else:
        conn.execute(
            """
            UPDATE strategy_positions
            SET last_reconciled_ts = ?,
                updated_ts = ?,
                version = version + 1
            WHERE position_id = ?
            """,
            (now_ts, now_ts, int(position_id)),
        )


def _calc_pnl(outcome: str, entry_price: float, exit_price: float, shares: float) -> float:
    """Calculate realized P&L for both YES and NO outcome positions.

    For YES positions: pnl = shares * (exit_price - entry_price)
    For NO positions: entry_price and exit_price are already stored as NO prices
      (e.g. bought NO at 0.82, resolved NO at 0.97), so the same formula applies.
    The formula is therefore uniform: pnl = shares * (exit_price - entry_price).

    NOTE: use `is not None` guards — do NOT use `or` here because 0.0 is a valid
    exit price (resolution loss) and `0.0 or x` evaluates to x, producing wrong PnL.
    """
    safe_exit = float(exit_price) if exit_price is not None else float(entry_price or 0.0)
    safe_entry = float(entry_price or 0.0)
    return shares * (safe_exit - safe_entry)


def _calc_unrealized_pnl(outcome: str, entry_price: float, current_price: float, shares: float) -> float:
    """Calculate unrealized P&L for an open position.

    For NO positions current_price is the live NO price (1 - yes_price).
    Formula is uniform: unrealized = shares * (current_price - entry_price).
    """
    return shares * (float(current_price or entry_price) - float(entry_price or 0.0))


def _close_plugin_position(
    conn: sqlite3.Connection,
    strategy_id: int,
    position: Dict[str, Any],
    exit_reason: str,
    exit_price: float,
) -> None:
    now_ts = int(time.time())
    now_iso = _utc_now_iso()
    avg_entry_price = float(position.get("avg_entry_price") or position.get("entry_price") or 0.0)
    shares = float(position.get("filled_entry_shares") or position.get("entry_shares") or 0.0)
    outcome = str(position.get("outcome") or "YES").upper()
    # Use `is not None` guard — 0.0 is a valid exit price (resolution loss) and
    # `0.0 or x` would wrongly fall through to avg_entry_price.
    actual_exit_price = float(exit_price) if exit_price is not None else float(avg_entry_price)
    realized_pnl = _calc_pnl(outcome, avg_entry_price, actual_exit_price, shares)
    position_id = int(position["position_id"])

    conn.execute(
        """
        UPDATE strategy_positions
        SET state = 'closed',
            avg_exit_price = ?,
            filled_exit_shares = ?,
            realized_pnl = ?,
            pnl_usdc = ?,
            current_price = ?,
            current_value = ?,
            closed_ts = ?,
            last_reconciled_ts = ?,
            updated_ts = ?,
            version = version + 1
        WHERE position_id = ?
        """,
        (
            actual_exit_price,
            shares,
            realized_pnl,
            realized_pnl,
            actual_exit_price,
            shares * actual_exit_price,
            now_ts,
            now_ts,
            now_ts,
            position_id,
        ),
    )
    conn.execute(
        """
        UPDATE strategy_executions
        SET status = ?,
            pnl_usdc = ?,
            error_msg = '',
            updated_at = ?
        WHERE id = (
            SELECT id
            FROM strategy_executions
            WHERE strategy_id = ?
              AND wallet = ?
              AND condition_id = ?
              AND outcome = ?
            ORDER BY id DESC
            LIMIT 1
        )
        """,
        (
            str(exit_reason or "closed"),
            realized_pnl,
            now_iso,
            int(strategy_id),
            str(position.get("wallet") or "").lower(),
            str(position.get("condition_id") or "").lower(),
            str(position.get("outcome") or "").upper(),
        ),
    )
    conn.commit()

    # Fire-and-forget brain update
    try:
        from brain import Brain
        b = Brain()
        closed_count = conn.execute(
            "SELECT COUNT(*) FROM strategy_positions WHERE strategy_id=? AND state='closed'",
            (str(strategy_id),)
        ).fetchone()[0]
        _st_row = conn.execute(
            "SELECT strategy_type FROM strategies WHERE id=?",
            (str(strategy_id),)
        ).fetchone()
        _strategy_type = (_st_row[0] if _st_row else None) or "unknown"
        b.maybe_update_brain(
            strategy_type=_strategy_type,
            market_category="unknown",
            outcome="win" if realized_pnl > 0 else "loss",
            pnl=float(realized_pnl or 0),
            closed_count=closed_count,
        )
    except Exception:
        logger.debug("Brain feedback failed (non-blocking)", exc_info=True)

    # Resolution feedback for intel trade ideas
    # Covers both legacy "news_intel_manual" wallet and news_intel_v1's "news_<condition_id[:8]>" wallets
    _wallet = str(position.get("wallet") or "")
    _new_status = str(exit_reason or "")
    _is_news_position = _wallet == "news_intel_manual" or _wallet.startswith("news_")
    if _is_news_position:
        try:
            from brain import Brain
            _b = Brain()
            _condition_id = str(position.get("condition_id") or "")
            _outcome_tok = str(position.get("outcome") or "YES").upper()
            _idea_row = _b._get_conn().execute(
                """SELECT id, direction FROM trade_ideas
                   WHERE condition_id=? AND status IN ('acted', 'open')
                   ORDER BY acted_at DESC LIMIT 1""",
                (_condition_id,)
            ).fetchone()
            if _idea_row:
                _direction = _idea_row["direction"]
                # All resolution reason strings used across the four strategy plugins:
                #   term_structure_v1 / news_intel_v1: "market_resolved_yes" / "market_resolved_no"
                #   political_no_value_v1 / sports_no_value_v1: "market_resolved_win" / "market_resolved_loss"
                _RESOLUTION_REASONS = {
                    "market_resolved_yes", "market_resolved_no",
                    "market_resolved_win", "market_resolved_loss",
                }
                if _new_status in _RESOLUTION_REASONS:
                    if _new_status == "market_resolved_win":
                        _idea_outcome = "win"   # NO position won
                    elif _new_status == "market_resolved_loss":
                        _idea_outcome = "loss"  # NO position lost
                    elif _new_status == "market_resolved_yes":
                        # YES resolved → buy_yes wins, buy_no loses
                        _idea_outcome = "win" if _direction == "buy_yes" else "loss"
                    else:  # market_resolved_no
                        # NO resolved → buy_no wins, buy_yes loses
                        _idea_outcome = "win" if _direction == "buy_no" else "loss"
                    _b.record_idea_resolution(_idea_row["id"], _idea_outcome, actual_exit_price)
                    logger.info(
                        "Intel idea %d resolved: %s (direction=%s, reason=%s)",
                        _idea_row["id"], _idea_outcome, _direction, _new_status,
                    )
                else:
                    # Non-resolution close (stop-loss, stale, etc.) — mark idea resolved so it isn't re-entered
                    _b.record_idea_resolution(_idea_row["id"], "closed", actual_exit_price)
                    logger.info(
                        "Intel idea %d closed (reason=%s) — marking resolved to prevent re-entry",
                        _idea_row["id"], _new_status
                    )
        except Exception as _e:
            logger.warning("Intel resolution feedback failed: %s", _e)


def _compute_strategy_unrealized(
    conn: sqlite3.Connection,
    strategy: Dict[str, Any],
    plugin,
    positions: List[Dict[str, Any]],
) -> float:
    total = 0.0
    for position in positions:
        manage_state = plugin.manage(conn, strategy, position)
        current_price = float(
            manage_state.current_price
            if manage_state.current_price is not None
            else (position.get("avg_entry_price") or position.get("entry_price") or 0.0)
        )
        shares = float(position.get("filled_entry_shares") or position.get("entry_shares") or 0.0)
        avg_entry = float(position.get("avg_entry_price") or position.get("entry_price") or 0.0)
        total += shares * (current_price - avg_entry)
    return total


def _strategy_realized_pnl(conn: sqlite3.Connection, strategy_id: int) -> float:
    row = conn.execute(
        """
        SELECT COALESCE(SUM(realized_pnl), 0)
        FROM strategy_positions
        WHERE strategy_id = ?
          AND state = 'closed'
        """,
        (str(strategy_id),),
    ).fetchone()
    return float((row[0] if row else 0.0) or 0.0)


def _refresh_strategy_totals(
    conn: sqlite3.Connection,
    strategy: Dict[str, Any],
    plugin,
) -> None:
    strategy_id = int(strategy["id"])
    open_positions = _active_strategy_positions(conn, strategy_id)
    realized_pnl = _strategy_realized_pnl(conn, strategy_id)
    unrealized_pnl = _compute_strategy_unrealized(conn, strategy, plugin, open_positions)
    trades_triggered = conn.execute(
        "SELECT COUNT(*) FROM strategy_executions WHERE strategy_id = ?",
        (strategy_id,),
    ).fetchone()[0]
    conn.execute(
        """
        UPDATE strategies
        SET trades_triggered = ?,
            pnl_usdc = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            int(trades_triggered or 0),
            realized_pnl + unrealized_pnl,
            _utc_now_iso(),
            strategy_id,
        ),
    )
    conn.commit()


def _enrich_signal_with_live_price(signal):
    """Fetch live CLOB price and update signal.current_price for accurate entry.

    Falls back to the cached price if the fetch fails. Logs divergence > 5%.
    """
    from sniper.markets.polymarket_api import fetch_live_clob_price
    cached = signal.current_price
    live = fetch_live_clob_price(signal.condition_id, signal.outcome)
    if live is not None and live > 0:
        if cached and cached > 0:
            gap = abs(live - cached) / cached * 100
            if gap > 5:
                logger.info(
                    f"CLOB divergence: live={live:.4f} vs cached={cached:.4f} "
                    f"({gap:.1f}%) for {signal.condition_id[:16]}..."
                )
        signal.current_price = live
    return signal


def _build_risk_context(
    conn: sqlite3.Connection,
    profile: Optional[InstanceProfile],
    condition_id: str,
) -> Optional[RiskContext]:
    """Build a RiskContext from the instance profile and live DB state.

    Returns None if no profile is provided (backward compat: skip risk checks).
    """
    if profile is None:
        return None

    # Total exposure = sum of all open strategy_positions sizes (entry_shares * avg_entry_price)
    row = conn.execute(
        """
        SELECT COALESCE(SUM(filled_entry_shares * avg_entry_price), 0)
        FROM strategy_positions
        WHERE state IN ('pending_entry', 'open', 'pending_exit')
        """
    ).fetchone()
    total_exposure_usdc = float(row[0] if row else 0.0)

    # Market exposure = sum of open positions for this specific condition_id
    row = conn.execute(
        """
        SELECT COALESCE(SUM(filled_entry_shares * avg_entry_price), 0)
        FROM strategy_positions
        WHERE state IN ('pending_entry', 'open', 'pending_exit')
          AND condition_id = ?
        """,
        (condition_id,),
    ).fetchone()
    market_exposure_usdc = float(row[0] if row else 0.0)

    # Concurrent positions = count of open strategy_positions
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM strategy_positions
        WHERE state IN ('pending_entry', 'open', 'pending_exit')
        """
    ).fetchone()
    concurrent_positions = int(row[0] if row else 0)

    # Daily realized PnL = sum of today's closed position PnL
    today_start = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = conn.execute(
        """
        SELECT COALESCE(SUM(realized_pnl), 0)
        FROM strategy_positions
        WHERE state = 'closed'
          AND closed_ts >= ?
        """,
        (int(datetime.strptime(today_start, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()),),
    ).fetchone()
    daily_realized_pnl_usdc = float(row[0] if row else 0.0)

    return RiskContext(
        bankroll_usdc=profile.bankroll_usdc,
        total_exposure_usdc=total_exposure_usdc,
        market_exposure_usdc=market_exposure_usdc,
        concurrent_positions=concurrent_positions,
        daily_realized_pnl_usdc=daily_realized_pnl_usdc,
        max_total_exposure_usdc=profile.max_total_exposure_usdc,
        max_market_exposure_usdc=profile.max_market_exposure_usdc,
        max_concurrent_positions=profile.max_concurrent_positions,
        daily_loss_limit_usdc=profile.daily_loss_limit_usdc,
        paper_only=profile.paper_only,
        real_trading_enabled=profile.real_trading_enabled,
    )


def _global_cascade_check(conn: sqlite3.Connection, window_minutes: int = 20, threshold: int = 4) -> bool:
    """
    Returns True if >= threshold positions have closed (loss or stop) across ALL strategies
    within the last window_minutes. Signals a possible correlated news event cascade.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=window_minutes)).strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.cursor()
    cur.execute(
        """SELECT COUNT(*) FROM strategy_executions
           WHERE status IN ('closed','stop_loss','resolution_loss')
           AND updated_at >= ?""",
        (cutoff,)
    )
    count = cur.fetchone()[0]
    return count >= threshold


def _check_circuit_breaker(
    conn: sqlite3.Connection,
    strategy_id: int,
    strategy_label: str,
    *,
    max_consecutive_losses: int = 5,
    max_drawdown_usdc: float = 50.0,
) -> bool:
    """Return True if the strategy should be auto-paused.

    Triggers:
      - N consecutive closed losses (no win in between), OR
      - cumulative closed P&L below -max_drawdown_usdc

    Only counts trades closed AFTER the last pause timestamp, so old losses
    don't re-trigger the breaker immediately after manual reactivation.
    """
    # Use paused_at as reset point — trades closed before this are "forgiven"
    # C7 fix: paused_at is ISO datetime TEXT; closed_ts is INTEGER epoch.
    # Convert paused_at to epoch for correct comparison.
    strat_row = conn.execute(
        "SELECT paused_at FROM strategies WHERE id = ?", (strategy_id,)
    ).fetchone()
    _raw_paused = strat_row[0] if strat_row and strat_row[0] else None
    reset_ts = None
    if _raw_paused:
        try:
            from datetime import datetime, timezone
            if isinstance(_raw_paused, str):
                # Parse ISO datetime to epoch
                dt = datetime.fromisoformat(_raw_paused.replace("Z", "+00:00"))
                reset_ts = int(dt.timestamp())
            else:
                reset_ts = int(_raw_paused)
        except (ValueError, TypeError):
            reset_ts = None

    if reset_ts:
        rows = conn.execute(
            """SELECT pnl_usdc FROM strategy_positions
               WHERE strategy_id = ? AND state = 'closed'
               AND closed_ts > ?
               ORDER BY closed_ts DESC LIMIT ?""",
            (str(strategy_id), reset_ts, max_consecutive_losses),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT pnl_usdc FROM strategy_positions
               WHERE strategy_id = ? AND state = 'closed'
               ORDER BY closed_ts DESC LIMIT ?""",
            (str(strategy_id), max_consecutive_losses),
        ).fetchall()
    if not rows:
        return False  # no closed trades since last reset — nothing to judge

    # Check consecutive losses (most recent N trades all negative)
    if len(rows) >= max_consecutive_losses and all((r[0] or 0) < 0 for r in rows):
        total_recent = sum(r[0] or 0 for r in rows)
        logger.warning(
            "CIRCUIT BREAKER: %s — %d consecutive losses (total $%.2f). Auto-pausing.",
            strategy_label, len(rows), total_recent,
        )
        conn.execute(
            """UPDATE strategies SET status='paused', paused_at=datetime('now'),
               pause_reason=? WHERE id=?""",
            (f"Circuit breaker: {len(rows)} consecutive losses, ${total_recent:.2f}", strategy_id),
        )
        conn.commit()
        send_telegram_notification(
            f"⚡ Circuit breaker tripped: {strategy_label}\n"
            f"{len(rows)} consecutive losses (${total_recent:.2f})\n"
            f"Strategy auto-paused. Review and manually un-pause when ready."
        )
        return True

    # Check cumulative drawdown (only since last reset/unpause — I21 fix)
    if reset_ts:
        total_pnl_row = conn.execute(
            "SELECT SUM(pnl_usdc) FROM strategy_positions WHERE strategy_id = ? AND state = 'closed' AND closed_ts > ?",
            (str(strategy_id), reset_ts),
        ).fetchone()
    else:
        total_pnl_row = conn.execute(
            "SELECT SUM(pnl_usdc) FROM strategy_positions WHERE strategy_id = ? AND state = 'closed'",
            (str(strategy_id),),
        ).fetchone()
    total_pnl = (total_pnl_row[0] or 0) if total_pnl_row else 0
    if total_pnl < -max_drawdown_usdc:
        logger.warning(
            "CIRCUIT BREAKER: %s — cumulative P&L $%.2f < -$%.0f. Auto-pausing.",
            strategy_label, total_pnl, max_drawdown_usdc,
        )
        conn.execute(
            """UPDATE strategies SET status='paused', paused_at=datetime('now'),
               pause_reason=? WHERE id=?""",
            (f"Circuit breaker: cumulative P&L ${total_pnl:.2f} < -${max_drawdown_usdc:.0f}", strategy_id),
        )
        conn.commit()
        send_telegram_notification(
            f"⚡ Circuit breaker tripped: {strategy_label}\n"
            f"Cumulative P&L: ${total_pnl:.2f} (limit: -${max_drawdown_usdc:.0f})\n"
            f"Strategy auto-paused. Review and manually un-pause when ready."
        )
        return True

    return False


def process_plugin_strategy(
    conn: sqlite3.Connection,
    strategy: Dict[str, Any],
    *,
    profile: Optional[InstanceProfile] = None,
    world_boosts: Optional[dict] = None,
    conviction_engine: Optional[Any] = None,
) -> None:
    strategy_id = int(strategy["id"])
    strategy_type = str(strategy.get("strategy_type") or "").strip().lower()
    plugin = get_strategy_plugin(strategy_type)
    if plugin is None:
        return

    # ── Circuit breaker: auto-pause strategies that are bleeding ──────────
    strategy_label = strategy.get("label") or strategy_type
    if _check_circuit_breaker(conn, strategy_id, strategy_label):
        logger.info("Strategy %s paused by circuit breaker — skipping this cycle.", strategy_label)
        return

    instance_tag = profile.instance_tag if profile else ""
    run_id = _insert_strategy_run(conn, strategy_id, instance_tag=instance_tag)
    discovered_count = 0
    entered_count = 0
    exited_count = 0
    status = "ok"
    error_msg = ""

    _hl_mode = _is_hl_plugin(plugin) and _HAS_HL_EXECUTION

    try:
        open_positions = _active_hl_positions(conn, strategy_id) if _hl_mode else _active_strategy_positions(conn, strategy_id)
        signals = plugin.discover(conn, strategy)
        discovered_count = len(signals)

        # world_boosts passed in from the outer loop — one Supabase call per cycle, not per strategy
        if world_boosts is None:
            world_boosts = {}

        # Global cross-strategy dedup: don't enter a market already held by ANY strategy
        global_open = _global_open_condition_ids(conn)

        for signal in signals:
            # -- Signal queue: record discovery --
            _sq_id = None
            if _HAS_SIGNAL_QUEUE:
                try:
                    _sq_venue = "hyperliquid" if _hl_mode else "polymarket"
                    _sq_meta = signal.metadata or {}
                    if _hl_mode:
                        # HL signals: use liquidation confidence + full metadata for bucket analysis
                        _sq_confidence = float(_sq_meta.get("signal_confidence", 0))
                        _sq_meta_json = json.dumps({
                            "wallet": signal.wallet,
                            "title": signal.title[:100] if signal.title else "",
                            "distance_pct": _sq_meta.get("distance_pct"),
                            "prev_distance_pct": _sq_meta.get("prev_distance_pct"),
                            "whale_side": _sq_meta.get("whale_side"),
                            "whale_leverage": _sq_meta.get("whale_leverage"),
                            "whale_wallet": _sq_meta.get("whale_wallet"),
                            "margin_type": _sq_meta.get("margin_type"),
                            "whale_liq_price": _sq_meta.get("whale_liq_price"),
                            "distance_bucket": _sq_meta.get("distance_bucket"),
                            "threshold_bucket": _sq_meta.get("threshold_bucket"),
                        })
                    else:
                        # PM signals: use insider score + basic metadata
                        _sq_confidence = float(_sq_meta.get("position_insider_score", 0))
                        _sq_meta_json = json.dumps({"wallet": signal.wallet, "title": signal.title[:100]}) if signal.title else "{}"
                    _sq_id = insert_signal_queue_entry(
                        conn,
                        source=strategy_type,
                        venue=_sq_venue,
                        asset_id=signal.condition_id,
                        direction=str(signal.outcome or "yes").lower(),
                        confidence=_sq_confidence,
                        signal_ts=signal.analyzed_ts if signal.analyzed_ts > 0 else int(time.time()),
                        metadata_json=_sq_meta_json,
                    )
                except Exception:
                    logger.debug("signal_queue insert failed (non-blocking)", exc_info=True)

            # Cross-strategy dedup gate: skip if any strategy already holds this condition+outcome
            sig_key = (signal.condition_id, str(signal.outcome or "").lower())
            if sig_key in global_open:
                logger.debug(
                    "Cross-strategy dedup: skipping %s %s — already held by another strategy",
                    signal.condition_id[:16],
                    signal.outcome,
                )
                if _sq_id and _HAS_SIGNAL_QUEUE:
                    try:
                        update_signal_queue_status(conn, _sq_id, status="rejected", reject_reason="cross_strategy_dedup")
                    except Exception:
                        pass
                continue

            # Enrich signal with live CLOB price for accurate entry + drift.
            # Skip for HL plugins — they don't use Polymarket CLOB prices.
            if not _hl_mode:
                signal = _enrich_signal_with_live_price(signal)
            decision = plugin.enter(conn, strategy, signal, open_positions)
            if not decision.should_enter:
                if _sq_id and _HAS_SIGNAL_QUEUE:
                    try:
                        update_signal_queue_status(conn, _sq_id, status="rejected", reject_reason=decision.reason[:120])
                    except Exception:
                        pass
                continue
            _entry_idea_id = (decision.metadata or {}).get("idea_id") or (signal.metadata or {}).get("idea_id")
            if not _adversarial_gate_allows_entry(conn, _entry_idea_id):
                if _sq_id and _HAS_SIGNAL_QUEUE:
                    try:
                        update_signal_queue_status(conn, _sq_id, status="rejected", reject_reason="adversarial_gate")
                    except Exception:
                        pass
                continue

            # --- Probabilistic conviction gate ---
            # Evaluates whether we have genuine EV vs. the market price given our
            # empirical win rates in this (price_bucket × score_bucket) cell.
            # Skips entry when EV ≤ 0.  Passthrough when brain unavailable.
            if conviction_engine is not None:
                _entry_price_for_conv = float(
                    decision.entry_price or signal.current_price or 0.0
                )
                _score_for_conv = float(
                    (signal.metadata or {}).get("position_insider_score") or 0.0
                )
                _conv = conviction_engine.evaluate(
                    entry_price=_entry_price_for_conv,
                    insider_score=_score_for_conv,
                    outcome=str(signal.outcome or "YES"),
                    condition_id=signal.condition_id,
                    world_boosts=world_boosts,
                )
                if _conv.get("skip"):
                    logger.info(
                        "Conviction gate: skipping %s — %s",
                        signal.condition_id[:16],
                        _conv.get("reason", ""),
                    )
                    if _sq_id and _HAS_SIGNAL_QUEUE:
                        try:
                            update_signal_queue_status(conn, _sq_id, status="rejected", reject_reason="conviction_gate")
                        except Exception:
                            pass
                    continue
                logger.debug(
                    "Conviction gate: allowing %s — %s",
                    signal.condition_id[:16],
                    _conv.get("reason", ""),
                )

            # --- World context boost gate ---
            # Applies market_boosts_current from world_analyst.py to adjust size or skip
            _cid_lower = signal.condition_id.lower()
            if world_boosts and _cid_lower in world_boosts:
                _boost_info = world_boosts[_cid_lower]
                _boost = int(_boost_info.get("boost", 0))
                _boost_reason = str(_boost_info.get("reason", ""))[:80]
                _raw_b = _boost_info.get("_raw_boost", _boost)
                _conf = _boost_info.get("confidence", "medium")
                _weight = _boost_info.get("_confidence_weight", 1.0)
                if _boost <= -20:
                    logger.info(
                        "World context: skipping %s (boost=%d raw=%d conf=%s w=%.1f: %s)",
                        signal.condition_id[:16], _boost, _raw_b, _conf, _weight, _boost_reason,
                    )
                    if _sq_id and _HAS_SIGNAL_QUEUE:
                        try:
                            update_signal_queue_status(conn, _sq_id, status="rejected", reject_reason="world_boost_negative")
                        except Exception:
                            pass
                    continue
                elif _boost <= -10:
                    decision = dataclasses.replace(decision, size_usdc=round(decision.size_usdc * 0.5, 2))
                    logger.info(
                        "World context: halving size for %s (boost=%d raw=%d conf=%s w=%.1f: %s)",
                        signal.condition_id[:16], _boost, _raw_b, _conf, _weight, _boost_reason,
                    )
                elif _boost >= 20:
                    decision = dataclasses.replace(decision, size_usdc=round(decision.size_usdc * 1.5, 2))
                    logger.info(
                        "World context: boosting size 1.5x for %s (boost=%d raw=%d conf=%s w=%.1f: %s)",
                        signal.condition_id[:16], _boost, _raw_b, _conf, _weight, _boost_reason,
                    )

            # --- Risk policy gate (additive, before recording entry) ---
            if profile is None:
                logger.debug(
                    "Risk profile not loaded — skipping risk gate for strategy %s signal %s",
                    strategy_id, signal.condition_id[:16],
                )
            else:
                # Build venue-appropriate risk context and evaluate
                if _hl_mode:
                    risk_ctx = _build_hl_risk_context(conn, profile, signal.condition_id)
                    if risk_ctx is not None:
                        _hl_leverage = float((decision.metadata or {}).get("leverage", 1.5))
                        _hl_asset_exposure = float(conn.execute(
                            "SELECT COALESCE(SUM(margin_usdc), 0) FROM hl_positions WHERE state = 'open' AND asset = ?",
                            (signal.condition_id,),
                        ).fetchone()[0])
                        from sniper.risk.models import VenueRiskLimits
                        risk_decision = evaluate_entry_risk(
                            risk_ctx,
                            decision.size_usdc,
                            signal.condition_id,
                            execution_mode=str(decision.execution_mode or "paper"),
                            venue_limits=VenueRiskLimits(
                                max_exposure_usdc=profile.hl_max_exposure_usdc,
                                max_leverage=profile.hl_max_leverage,
                                max_concurrent=profile.hl_max_concurrent,
                            ),
                            proposed_leverage=_hl_leverage,
                            asset_exposure_usdc=_hl_asset_exposure,
                        )
                    else:
                        risk_decision = None
                else:
                    risk_ctx = _build_risk_context(conn, profile, signal.condition_id)
                    risk_decision = evaluate_entry_risk(
                        risk_ctx,
                        decision.size_usdc,
                        signal.condition_id,
                        execution_mode=str(decision.execution_mode or "paper"),
                    ) if risk_ctx is not None else None
                if risk_decision is not None and not risk_decision.allowed:
                        logger.info(
                            "Risk policy rejected entry for strategy %s market %s: %s",
                            strategy_id,
                            signal.condition_id[:16],
                            risk_decision.reason,
                        )
                        if _sq_id and _HAS_SIGNAL_QUEUE:
                            try:
                                update_signal_queue_status(conn, _sq_id, status="rejected", reject_reason=f"risk:{risk_decision.reason[:100]}")
                            except Exception:
                                pass
                        continue
                if risk_decision is not None and risk_decision.clamped:
                        logger.info(
                            "Risk policy clamped size for strategy %s market %s: %.2f → %.2f (%s)",
                            strategy_id,
                            signal.condition_id[:16],
                            decision.size_usdc,
                            risk_decision.approved_size_usdc,
                            risk_decision.reason,
                        )
                        decision.size_usdc = risk_decision.approved_size_usdc

            _params = _json_object(strategy.get("parameters"))
            _max_concurrent = int(_params.get("max_concurrent_positions") or 0)

            # Branch entry recording: HL uses hl_positions, PM uses strategy_positions
            _entry_ok = False
            if _hl_mode:
                _hl_leverage = float((decision.metadata or {}).get("leverage", 1.5))
                _hl_size_contracts = float((decision.metadata or {}).get("size_contracts", 0))
                _hl_entry_price = float(decision.entry_price or signal.current_price or 0.0)
                if _hl_size_contracts > 0 and _hl_entry_price > 0:
                    # Store whale_wallet in instance_tag so manage/exit can filter precisely
                    _whale_wallet = str((decision.metadata or {}).get("whale_wallet", "")).strip().lower()
                    _hl_tag = f"whale:{_whale_wallet}" if _whale_wallet else instance_tag
                    _pos_id = record_hl_entry(
                        conn,
                        strategy_id=strategy_id,
                        asset=signal.condition_id,
                        side=str(signal.outcome or "long").lower(),
                        entry_price=_hl_entry_price,
                        size=_hl_size_contracts,
                        leverage=_hl_leverage,
                        margin_usdc=float(decision.size_usdc or 0.0),
                        instance_tag=_hl_tag,
                    )
                    _entry_ok = _pos_id is not None
            else:
                _entry_ok = _record_plugin_entry(
                    conn, strategy, signal, decision,
                    instance_tag=instance_tag,
                    max_concurrent=_max_concurrent,
                )

            if _entry_ok:
                entered_count += 1
                # -- Signal queue: mark entered --
                if _sq_id and _HAS_SIGNAL_QUEUE:
                    try:
                        _now_ts = int(time.time())
                        update_signal_queue_status(
                            conn, _sq_id,
                            status="entered",
                            entry_ts=_now_ts,
                            fill_ts=_now_ts,
                            latency_ms=0,  # same-tick for paper mode
                        )
                    except Exception:
                        logger.debug("signal_queue update failed (non-blocking)", exc_info=True)
                # Update global dedup set so subsequent signals in this run don't double-enter
                global_open.add(sig_key)
                # Mark the source trade idea as acted so it isn't re-surfaced until resolved
                if _entry_idea_id:
                    try:
                        from brain import Brain as _Brain
                        _Brain().mark_idea_acted(int(_entry_idea_id))
                    except Exception:
                        logger.debug("mark_idea_acted(%s) failed (non-blocking)", _entry_idea_id, exc_info=True)
                # Append the new position to the in-memory list
                if _hl_mode:
                    _new = conn.execute(
                        "SELECT * FROM hl_positions WHERE strategy_id = ? AND state = 'open' ORDER BY id DESC LIMIT 1",
                        (strategy_id,),
                    ).fetchone()
                else:
                    _new = conn.execute(
                        """SELECT * FROM strategy_positions
                           WHERE wallet=? AND condition_id=? AND outcome=?
                           ORDER BY position_id DESC LIMIT 1""",
                        (signal.wallet, signal.condition_id, signal.outcome),
                    ).fetchone()
                if _new:
                    open_positions.append(dict_from_row(_new))

        # ── Manage / Exit loop ──────────────────────────────────────
        if _hl_mode:
            # HL venue: positions live in hl_positions, use HL execution functions
            open_positions = _active_hl_positions(conn, strategy_id)
            for position in open_positions:
                manage_state = plugin.manage(conn, strategy, position)
                # Mark-to-market via HL execution engine
                _mark = float(manage_state.current_price or position.get("current_price") or 0.0)
                if _mark > 0:
                    update_hl_position(conn, int(position["id"]), _mark)

                exit_decision = plugin.exit(conn, strategy, position, manage_state)
                if exit_decision.should_exit:
                    _ep = exit_decision.exit_price
                    if _ep is None or _ep == 0:
                        _ep = _mark or float(position.get("entry_price") or 0.0)
                    close_hl_position(conn, int(position["id"]), float(_ep), reason=exit_decision.reason)
                    exited_count += 1
        else:
            # PM venue: positions live in strategy_positions
            open_positions = _active_strategy_positions(conn, strategy_id)
            cascade_active = _global_cascade_check(conn)
            for position in open_positions:
                position["cascade_active"] = cascade_active
                manage_state = plugin.manage(conn, strategy, position)
                exit_decision = plugin.exit(conn, strategy, position, manage_state)
                if exit_decision.should_exit:
                    _ep = exit_decision.exit_price
                    if _ep is None:
                        _ep = manage_state.current_price
                    if _ep is None:
                        _ep = float(position.get("avg_entry_price") or 0.0)
                    _close_plugin_position(
                        conn,
                        strategy_id,
                        position,
                        exit_decision.reason,
                        float(_ep),
                    )
                    exited_count += 1
                else:
                    _avg_entry = float(position.get("avg_entry_price") or position.get("entry_price") or 0.0)
                    _shares = float(position.get("filled_entry_shares") or position.get("entry_shares") or 0.0)
                    _outcome = str(position.get("outcome") or "YES").upper()
                    _cur_price = float(
                        manage_state.current_price if manage_state.current_price is not None
                        else _avg_entry
                    )
                    _unrealized = _calc_unrealized_pnl(_outcome, _avg_entry, _cur_price, _shares)
                    _update_position_touch(
                        conn,
                        int(position["position_id"]),
                        current_price=_cur_price,
                        current_value=_shares * _cur_price,
                        pnl_usdc=_unrealized,
                    )

        conn.commit()

        # ── Totals refresh ────────────────────────────────────────
        if _hl_mode:
            open_positions = _active_hl_positions(conn, strategy_id)
            _hl_realized = conn.execute(
                "SELECT COALESCE(SUM(realized_pnl), 0) FROM hl_positions WHERE strategy_id = ? AND state = 'closed'",
                (strategy_id,),
            ).fetchone()
            realized_pnl = float(_hl_realized[0] if _hl_realized else 0.0)
            unrealized_pnl = sum(float(p.get("unrealized_pnl") or 0.0) for p in open_positions)
            _all_hl = conn.execute(
                "SELECT * FROM hl_positions WHERE strategy_id = ? ORDER BY opened_ts DESC",
                (strategy_id,),
            ).fetchall()
            report = plugin.report(conn, strategy, [dict_from_row(r) for r in _all_hl])
            # Update strategy totals
            _hl_trades_count = len(_all_hl)
            conn.execute(
                "UPDATE strategies SET trades_triggered = ?, pnl_usdc = ?, updated_at = ? WHERE id = ?",
                (_hl_trades_count, realized_pnl + unrealized_pnl, _utc_now_iso(), strategy_id),
            )
            conn.commit()
        else:
            open_positions = _active_strategy_positions(conn, strategy_id)
            realized_pnl = _strategy_realized_pnl(conn, strategy_id)
            unrealized_pnl = _compute_strategy_unrealized(conn, strategy, plugin, open_positions)
            report = plugin.report(conn, strategy, _all_strategy_positions(conn, strategy_id))
            _refresh_strategy_totals(conn, strategy, plugin)

        # Phase 4B: Signal quality metrics from deterministic run counts
        _run_details = dict(getattr(report, "summary", {}) or {})
        _run_details["signal_quality"] = {
            "discovery_hit_rate": round(entered_count / max(discovered_count, 1), 4),
            "rejection_rate": round((discovered_count - entered_count) / max(discovered_count, 1), 4),
        }

        _finish_strategy_run(
            conn,
            run_id,
            status=status,
            discovered_count=discovered_count,
            entered_count=entered_count,
            exited_count=exited_count,
            open_positions=len(open_positions),
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl,
            error_msg=error_msg,
            details=_run_details,
        )
    except Exception as exc:
        conn.rollback()
        status = "error"
        error_msg = str(exc)
        logger.error(
            "Plugin strategy %s failed: %s\n%s",
            strategy_id,
            exc,
            traceback.format_exc(),
        )
        if _hl_mode:
            open_positions = _active_hl_positions(conn, strategy_id)
            _hl_r = conn.execute(
                "SELECT COALESCE(SUM(realized_pnl), 0) FROM hl_positions WHERE strategy_id = ? AND state = 'closed'",
                (strategy_id,),
            ).fetchone()
            realized_pnl = float(_hl_r[0] if _hl_r else 0.0)
            unrealized_pnl = sum(float(p.get("unrealized_pnl") or 0.0) for p in open_positions)
        else:
            open_positions = _active_strategy_positions(conn, strategy_id)
            plugin = get_strategy_plugin(strategy_type)
            unrealized_pnl = _compute_strategy_unrealized(conn, strategy, plugin, open_positions) if plugin else 0.0
            realized_pnl = _strategy_realized_pnl(conn, strategy_id)
        _finish_strategy_run(
            conn,
            run_id,
            status=status,
            discovered_count=discovered_count,
            entered_count=entered_count,
            exited_count=exited_count,
            open_positions=len(open_positions),
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl,
            error_msg=error_msg,
            details={},
        )


# ============================================================================
# TELEGRAM NOTIFICATIONS
# ============================================================================

def send_telegram_notification(message: str) -> bool:
    """Send a Telegram notification."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram credentials not configured, skipping notification")
        return False

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
        }
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Failed to send Telegram notification: {e}")
        return False


# ============================================================================
# CLOB CLIENT & EXECUTION
# ============================================================================

def load_private_key() -> Optional[str]:
    """Load private key from wallet key file."""
    try:
        if WALLET_KEY_FILE.exists():
            with open(WALLET_KEY_FILE, 'r') as f:
                return f.read().strip()
    except Exception as e:
        logger.error(f"Failed to load private key: {e}")
    return None


def get_clob_client() -> Optional[ClobClient]:
    """Create and initialize CLOB client."""
    if ClobClient is None:
        logger.warning("py_clob_client not installed, real execution unavailable")
        return None

    try:
        private_key = load_private_key()
        if not private_key:
            logger.error("No private key found, cannot initialize CLOB client")
            return None

        client = ClobClient(
            host=CLOB_HOST,
            key=private_key,
            chain_id=POLYMARKET_CHAIN_ID,
            signature_type=0,
        )
        client.set_api_creds(client.create_or_derive_api_creds())
        return client
    except Exception as e:
        logger.error(f"Failed to initialize CLOB client: {e}")
        return None


def should_initialize_clob_client(profile: Optional[InstanceProfile]) -> bool:
    """Return True only when this instance is allowed to place real trades."""
    return bool(profile and profile.real_trading_enabled)


def get_token_id_for_market(condition_id: str, outcome: str) -> Optional[str]:
    """
    Get token_id from Gamma API for a given condition_id and outcome.
    outcome should be 'yes' or 'no' (case-insensitive).
    """
    try:
        url = f"{GAMMA_API_HOST}/markets"
        params = {"condition_id": condition_id}
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        
        markets = response.json()
        if not markets or not isinstance(markets, list) or len(markets) == 0:
            logger.warning(f"No markets found for condition_id {condition_id}")
            return None

        market = markets[0]
        if "clobTokenIds" not in market or not market["clobTokenIds"]:
            logger.warning(f"No clobTokenIds for condition_id {condition_id}")
            return None

        token_ids = market["clobTokenIds"]
        # Index 0 = YES, Index 1 = NO
        outcome_lower = outcome.lower().strip()
        if outcome_lower == "yes":
            return token_ids[0] if len(token_ids) > 0 else None
        elif outcome_lower == "no":
            return token_ids[1] if len(token_ids) > 1 else None
        else:
            logger.warning(f"Unknown outcome: {outcome}")
            return None

    except Exception as e:
        logger.error(f"Failed to fetch token_id for {condition_id}: {e}")
        return None


def place_market_order(
    client: ClobClient,
    token_id: str,
    price: float,
    size_usdc: float,
) -> Optional[str]:
    """
    Place a market order on CLOB.
    Returns order_id on success, None on failure.
    """
    try:
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size_usdc,
        )
        response = client.create_market_order(order_args)
        
        if response and "orderId" in response:
            order_id = response["orderId"]
            logger.info(f"Market order placed: {order_id}")
            return order_id
        else:
            logger.warning(f"No orderId in response: {response}")
            return None

    except Exception as e:
        logger.error(f"Failed to place market order: {e}\n{traceback.format_exc()}")
        return None


# ============================================================================
# CIRCUIT BREAKERS
# ============================================================================

def check_daily_loss_limit(conn: sqlite3.Connection) -> bool:
    """Check if daily loss limit has been exceeded for real executions."""
    try:
        cursor = conn.cursor()
        today = datetime.now(timezone.utc).date()
        
        cursor.execute("""
            SELECT SUM(pnl_usdc) as total_loss
            FROM strategy_executions
            WHERE execution_type = 'real'
              AND DATE(created_at) = ?
              AND pnl_usdc < 0
        """, (today,))
        
        row = cursor.fetchone()
        total_loss = row[0] if row and row[0] else 0
        
        if abs(total_loss) >= DAILY_LOSS_LIMIT_USDC:
            logger.warning(f"Daily loss limit exceeded: ${abs(total_loss)} >= ${DAILY_LOSS_LIMIT_USDC}")
            return False
        
        return True
    except Exception as e:
        logger.error(f"Error checking daily loss limit: {e}")
        return False


def check_max_open_positions(conn: sqlite3.Connection) -> bool:
    """Check if max open real positions limit has been exceeded."""
    try:
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT COUNT(*) as open_count
            FROM strategy_executions
            WHERE execution_type = 'real'
              AND status IN ('pending', 'open')
        """)
        
        row = cursor.fetchone()
        open_count = row[0] if row else 0
        
        if open_count >= MAX_OPEN_REAL_POSITIONS:
            logger.warning(f"Max open positions reached: {open_count} >= {MAX_OPEN_REAL_POSITIONS}")
            return False
        
        return True
    except Exception as e:
        logger.error(f"Error checking max open positions: {e}")
        return False


def check_min_balance(client: Optional[ClobClient]) -> bool:
    """Check if minimum USDC balance is available.

    D1 audit note: This is a stub that always returns True. It is called
    in the real-execution path (line ~2017) but real trading is disabled
    system-wide. Must be properly implemented before enabling real trading.
    """
    return True


# ============================================================================
# STRATEGY EXECUTION LOGIC
# ============================================================================

def evaluate_strategy(
    strategy: Dict[str, Any],
    position: Dict[str, Any],
) -> tuple[bool, str]:
    """D2 audit note: Legacy prototype code. Only reachable for strategies where
    get_strategy_plugin() returns None (no registered plugin). All active strategies
    use the plugin path. This code is effectively dead but kept as a fallback for
    unregistered strategy types. Consider removing when legacy string-type strategies
    are fully deprecated.

    Evaluate if a position matches the strategy rules.
    Returns (should_execute, reason_text).
    """
    strategy_type = strategy.get("strategy_type", "").lower()
    parameters = strategy.get("parameters", {})
    
    if isinstance(parameters, str):
        try:
            parameters = json.loads(parameters)
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.warning("Failed to parse strategy parameters: %s", parameters[:200] if isinstance(parameters, str) else parameters)
            parameters = {}

    title = position.get("title", "")
    price = position.get("price", 0)
    notional = position.get("notional", 0)

    # mirror_all: copy if notional >= min_size_usdc
    if strategy_type == "mirror_all":
        min_size = parameters.get("min_size_usdc", 100)
        if notional >= min_size:
            return True, f"mirror_all: notional ${notional} >= min ${min_size}"
        return False, f"mirror_all: notional ${notional} < min ${min_size}"

    # selective: copy if price in range AND title matches categories
    elif strategy_type == "selective":
        min_entry = parameters.get("min_entry", 0)
        max_entry = parameters.get("max_entry", 1)
        categories = parameters.get("categories", [])

        price_match = min_entry <= price <= max_entry
        category_match = False
        
        if categories:
            title_lower = title.lower()
            category_match = any(cat.lower() in title_lower for cat in categories)
        else:
            category_match = True  # No categories = match all

        if price_match and category_match:
            return True, f"selective: price in range [{min_entry}, {max_entry}] and category matched"
        return False, f"selective: price in range={price_match}, category_match={category_match}"

    # signal_only: never execute, just log
    elif strategy_type == "signal_only":
        return False, "signal_only: does not execute, only signals"

    # fade: copy opposite (always execute if we match)
    elif strategy_type == "fade":
        min_size = parameters.get("min_size_usdc", 100)
        if notional >= min_size:
            return True, f"fade: notional ${notional} >= min ${min_size} (will fade)"
        return False, f"fade: notional ${notional} < min ${min_size}"

    return False, f"unknown strategy type: {strategy_type}"


def determine_execution_outcome(
    strategy_type: str,
    original_outcome: str,
) -> str:
    """Determine the outcome to execute based on strategy type."""
    if strategy_type.lower() == "fade":
        # Fade = opposite outcome
        if original_outcome.lower() == "yes":
            return "NO"
        else:
            return "YES"
    # mirror_all, selective, signal_only: use same outcome
    return original_outcome.upper()


def execute_strategy_position(
    conn: sqlite3.Connection,
    strategy: Dict[str, Any],
    position: Dict[str, Any],
    clob_client: Optional[ClobClient],
) -> bool:
    """
    Execute a strategy position. Returns True if execution was successful.
    """
    strategy_id = strategy.get("id")
    wallet = strategy.get("wallet")
    strategy_type = strategy.get("strategy_type", "").lower()
    parameters = strategy.get("parameters", {})
    
    if isinstance(parameters, str):
        try:
            parameters = json.loads(parameters)
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.warning("Failed to parse strategy parameters: %s", parameters[:200] if isinstance(parameters, str) else parameters)
            parameters = {}

    condition_id = position.get("condition_id")
    outcome = position.get("outcome")
    title = position.get("title")
    price = position.get("price", 0)
    notional = position.get("notional", 0)

    # Determine execution type
    execution_type = parameters.get("execution_type", "paper").lower()
    bet_size = parameters.get("bet_size_usdc", 100)

    # Determine outcome to execute
    exec_outcome = determine_execution_outcome(strategy_type, outcome)

    try:
        cursor = conn.cursor()
        order_id = None
        error_msg = None
        pnl_usdc = 0

        # Paper trade execution
        if execution_type == "paper":
            try:
                wallet = position.get("wallet", "")
                cursor.execute("""
                    INSERT INTO paper_trades
                    (wallet, condition_id, outcome, direction, signal_type, market_title,
                     entry_price, current_price, exit_price, size_usdc, status,
                     pnl_usdc, roi_percent, opened_at, closed_at, exit_reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    wallet,
                    condition_id,
                    exec_outcome,
                    exec_outcome,       # direction mirrors outcome for copy trades
                    "strategy_copy",
                    title,
                    price,
                    price,              # current_price = entry_price initially
                    None,
                    bet_size,
                    "open",
                    0,
                    0,
                    datetime.now(timezone.utc).isoformat(),
                    None,
                    None,
                ))
                conn.commit()
                logger.info(f"Paper trade created for strategy {strategy_id}: {title} {exec_outcome} @ ${price}")
            except Exception as e:
                logger.error(f"Failed to insert paper trade: {e}")
                error_msg = f"Paper trade insert failed: {str(e)}"

        # Real execution via CLOB
        elif execution_type == "real":
            # Check circuit breakers
            if not check_daily_loss_limit(conn):
                error_msg = "Daily loss limit exceeded"
                logger.warning(f"Skipping real execution: {error_msg}")
            elif not check_max_open_positions(conn):
                error_msg = "Max open positions reached"
                logger.warning(f"Skipping real execution: {error_msg}")
            elif not check_min_balance(clob_client):
                error_msg = "Insufficient USDC balance"
                logger.warning(f"Skipping real execution: {error_msg}")
            else:
                # Proceed with real execution
                if not clob_client:
                    error_msg = "CLOB client not available, falling back to paper"
                    logger.warning(error_msg)
                    execution_type = "paper"
                    # Fall back to paper execution — must use same schema as primary paper path
                    try:
                        fallback_wallet = position.get("wallet", "")
                        cursor.execute("""
                            INSERT INTO paper_trades
                            (wallet, condition_id, outcome, direction, signal_type, market_title,
                             entry_price, current_price, exit_price, size_usdc, status,
                             pnl_usdc, roi_percent, opened_at, closed_at, exit_reason)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            fallback_wallet,
                            condition_id,
                            exec_outcome,
                            exec_outcome,       # direction mirrors outcome
                            "strategy_copy_fallback",
                            title,
                            price,
                            price,
                            None,
                            bet_size,
                            "open",
                            0,
                            0,
                            datetime.now(timezone.utc).isoformat(),
                            None,
                            None,
                        ))
                        conn.commit()
                        error_msg = None  # fallback succeeded — clear error so watermark can advance
                        logger.info(f"Paper fallback trade created for strategy {strategy_id}: {title} {exec_outcome} @ ${price}")
                    except Exception as e:
                        logger.error(f"Fallback paper trade failed: {e}")
                else:
                    # Get token_id
                    token_id = get_token_id_for_market(condition_id, exec_outcome)
                    if not token_id:
                        error_msg = f"Could not get token_id for {condition_id} {exec_outcome}"
                        logger.error(error_msg)
                    else:
                        # Place market order
                        order_id = place_market_order(clob_client, token_id, price, bet_size)
                        if not order_id:
                            error_msg = "Failed to place market order on CLOB"
                            logger.error(error_msg)
                        else:
                            logger.info(f"Real order executed: {order_id}")

        # Log execution to strategy_executions table
        cursor.execute("""
            INSERT INTO strategy_executions
            (strategy_id, wallet, condition_id, outcome, title, execution_type, 
             size_usdc, entry_price, token_id, order_id, status, pnl_usdc, error_msg, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            strategy_id,
            wallet,
            condition_id,
            exec_outcome,
            title,
            execution_type,
            bet_size,
            price,
            None,  # token_id stored separately
            order_id,
            "open" if order_id else ("pending" if strategy_type == "signal_only" else "failed"),
            pnl_usdc,
            error_msg,
            datetime.now(timezone.utc).isoformat(),
            datetime.now(timezone.utc).isoformat(),
        ))
        conn.commit()

        # Send Telegram notification for signal_only or real executions
        if strategy_type == "signal_only" or execution_type == "real":
            action_text = "Signal logged" if strategy_type == "signal_only" else f"Order: {order_id}" if order_id else "Order failed"
            message = (
                f"🎯 Strategy Alert\n"
                f"<b>{wallet}</b>: {title}\n"
                f"Outcome: {exec_outcome} @ ${price}\n"
                f"Action: {action_text}\n"
                f"Size: ${bet_size}"
            )
            send_telegram_notification(message)

        # Update strategy stats
        cursor.execute("""
            UPDATE strategies
            SET trades_triggered = trades_triggered + 1,
                updated_at = ?
            WHERE id = ?
        """, (datetime.now(timezone.utc).isoformat(), strategy_id))
        conn.commit()

        # Return True only when execution genuinely succeeded with no error.
        # Paper mode: succeeded if insert completed without setting error_msg.
        # Real mode: succeeded if order_id was obtained.
        if error_msg:
            return False
        return order_id is not None or execution_type == "paper"

    except Exception as e:
        logger.error(f"Exception in execute_strategy_position: {e}\n{traceback.format_exc()}")
        return False


# ============================================================================
# MAIN SERVICE LOOP
# ============================================================================

def _is_strategy_due(strategy_id: int, plugin) -> bool:
    """Check if a strategy is due to run based on its cadence.

    Uses the module-level ``_last_run_ts`` dict to track when each strategy_id
    last ran.  If it ran less than ``plugin.default_loop_seconds`` ago, it is
    not yet due.  Returns True if it should run now.
    """
    loop_secs = getattr(plugin, "default_loop_seconds", 60)
    last_ts = _last_run_ts.get(strategy_id, 0.0)
    now = time.time()
    if now - last_ts < loop_secs:
        return False
    return True


def _mark_strategy_ran(strategy_id: int) -> None:
    """Record that *strategy_id* just completed a run."""
    _last_run_ts[strategy_id] = time.time()


def _is_feed_due(feed_id: str, adapter: Any) -> bool:
    """Check whether *feed_id* should tick based on adapter cadence."""
    poll_secs = max(1, int(getattr(adapter, "default_poll_seconds", 60) or 60))
    last_ts = _last_feed_tick_ts.get(feed_id, 0.0)
    now = time.time()
    return (now - last_ts) >= poll_secs


def _mark_feed_ticked(feed_id: str) -> None:
    """Record that *feed_id* just completed a tick."""
    _last_feed_tick_ts[feed_id] = time.time()


def _tick_initialized_feeds(conn: sqlite3.Connection, initialized_feeds: Optional[Dict[str, Any]]) -> None:
    """Tick all due feed adapters for the current cycle."""
    if not initialized_feeds:
        return
    for feed_id, adapter in initialized_feeds.items():
        if not _is_feed_due(feed_id, adapter):
            continue
        try:
            adapter.tick(conn)
            _mark_feed_ticked(feed_id)
        except Exception as exc:
            logger.warning("Feed '%s' tick failed: %s", feed_id, exc)


def _start_initialized_feeds(initialized_feeds: Optional[Dict[str, Any]]) -> None:
    """Start long-lived feed adapters after successful warmup."""
    if not initialized_feeds:
        return
    for feed_id, adapter in initialized_feeds.items():
        try:
            adapter.start()
        except Exception as exc:
            logger.warning("Feed '%s' start failed: %s", feed_id, exc)


def _stop_initialized_feeds(initialized_feeds: Optional[Dict[str, Any]]) -> None:
    """Stop long-lived feed adapters during runner shutdown."""
    if not initialized_feeds:
        return
    for feed_id, adapter in initialized_feeds.items():
        try:
            adapter.stop()
        except Exception as exc:
            logger.warning("Feed '%s' stop failed: %s", feed_id, exc)


def _run_auto_resume_checks(conn: sqlite3.Connection) -> None:
    """Check paused strategies for wallet score recovery and auto-resume any
    that have recovered above threshold for N consecutive runs.

    Only strategies with pause_reason = 'wallet_degradation' are eligible —
    manually paused strategies are never touched.

    I22 fix: no longer hard-coded to insider_copy_v1 — dynamically loads
    the plugin for each paused strategy and calls _check_wallet_recovery
    if the plugin supports it.
    """
    from sniper.settings.paper import PAPER_ENTRY_MIN_INSIDER_SCORE

    try:
        rows = conn.execute(
            """SELECT id, wallet, label, strategy_type, parameters, status,
                      trades_triggered, pnl_usdc, created_at, updated_at,
                      paused_at, pause_reason
               FROM strategies
               WHERE status = 'paused'
                 AND pause_reason = 'wallet_degradation'"""
        ).fetchall()

        if not rows:
            return

        # No longer hard-coded to one strategy type
        for row in rows:
            strategy = dict_from_row(row)
            try:
                strategy_type = str(strategy.get("strategy_type") or "").strip().lower()
                plugin = get_strategy_plugin(strategy_type)
                if plugin is None or not hasattr(plugin, "_check_wallet_recovery"):
                    continue
                params = strategy.get("parameters") or {}
                if isinstance(params, str):
                    import json as _json
                    try:
                        params = _json.loads(params)
                    except Exception:
                        params = {}
                min_score = float(params.get("min_insider_score") or PAPER_ENTRY_MIN_INSIDER_SCORE)
                n_consecutive = int(params.get("degradation_consecutive_runs") or 3)
                plugin._check_wallet_recovery(conn, strategy, min_score, n_consecutive)
            except Exception:
                logger.exception(
                    "Auto-resume check failed for strategy %s", strategy.get("id")
                )
    except Exception:
        logger.exception("_run_auto_resume_checks failed")


def _manage_paused_strategy_positions(conn: sqlite3.Connection) -> None:
    """
    For every *paused or stopped* plugin strategy that still has open positions,
    run manage() + exit() so that stop-losses, resolution exits, and P&L
    updates still fire even while the strategy is not accepting new entries.

    This closes the orphaned-position gap: when a strategy is paused (circuit
    breaker) or stopped (manually), its open positions were previously invisible
    to the main active-strategy loop.  Stopped strategies won't enter new
    positions after this exits them — that's correct behaviour.
    """
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT s.id, s.wallet, s.label, s.strategy_type,
                            s.parameters, s.status,
                            s.trades_triggered, s.pnl_usdc,
                            s.created_at, s.updated_at
            FROM strategies s
            JOIN strategy_positions sp ON sp.strategy_id = s.id
            WHERE s.status IN ('paused', 'stopped')
              AND sp.state IN ('pending_entry', 'open', 'pending_exit')
            """
        ).fetchall()

        # C8 fix: also check HL positions for paused/stopped HL strategies
        if _HAS_HL_EXECUTION:
            hl_rows = conn.execute(
                """
                SELECT DISTINCT s.id, s.wallet, s.label, s.strategy_type,
                                s.parameters, s.status,
                                s.trades_triggered, s.pnl_usdc,
                                s.created_at, s.updated_at
                FROM strategies s
                JOIN hl_positions hp ON hp.strategy_id = s.id
                WHERE s.status IN ('paused', 'stopped')
                  AND hp.state = 'open'
                """
            ).fetchall()
            # Merge without duplicates
            seen_ids = {dict_from_row(r)["id"] for r in rows}
            for hr in hl_rows:
                if dict_from_row(hr)["id"] not in seen_ids:
                    rows.append(hr)

        if not rows:
            return

        paused_strategies = [dict_from_row(r) for r in rows]
        logger.info(
            "Managing open positions for %d paused strategy/strategies: %s",
            len(paused_strategies),
            [s["id"] for s in paused_strategies],
        )

        cascade_active = _global_cascade_check(conn)

        for strategy in paused_strategies:
            strategy = _load_strategy(strategy)
            strategy_id = int(strategy["id"])
            strategy_type = str(strategy.get("strategy_type") or "").strip().lower()
            plugin = get_strategy_plugin(strategy_type)
            if plugin is None:
                continue

            try:
                _paused_hl = _is_hl_plugin(plugin) and _HAS_HL_EXECUTION
                if _paused_hl:
                    open_positions = _active_hl_positions(conn, strategy_id)
                else:
                    open_positions = _active_strategy_positions(conn, strategy_id)
                exited_count = 0

                for position in open_positions:
                    if not _paused_hl:
                        position["cascade_active"] = cascade_active
                    try:
                        manage_state = plugin.manage(conn, strategy, position)

                        if _paused_hl:
                            # HL: mark-to-market
                            _mark = float(manage_state.current_price or position.get("current_price") or 0.0)
                            if _mark > 0:
                                update_hl_position(conn, int(position["id"]), _mark)

                        exit_decision = plugin.exit(conn, strategy, position, manage_state)

                        if exit_decision.should_exit:
                            _ep = exit_decision.exit_price
                            if _ep is None:
                                _ep = manage_state.current_price
                            if _paused_hl:
                                if _ep is None or _ep == 0:
                                    _ep = float(position.get("entry_price") or 0.0)
                                close_hl_position(conn, int(position["id"]), float(_ep), reason=exit_decision.reason)
                            else:
                                if _ep is None:
                                    _ep = float(position.get("avg_entry_price") or 0.0)
                                _close_plugin_position(
                                    conn,
                                    strategy_id,
                                    position,
                                    exit_decision.reason,
                                    float(_ep),
                                )
                            exited_count += 1
                            logger.info(
                                "Paused strategy %s: closed position %s — %s",
                                strategy_id,
                                position.get("position_id"),
                                exit_decision.reason,
                            )
                        else:
                            # Keep P&L and current_price fresh
                            _avg_entry = float(
                                position.get("avg_entry_price") or position.get("entry_price") or 0.0
                            )
                            _shares = float(
                                position.get("filled_entry_shares") or position.get("entry_shares") or 0.0
                            )
                            _outcome = str(position.get("outcome") or "YES").upper()
                            _cur_price = float(
                                manage_state.current_price
                                if manage_state.current_price is not None
                                else _avg_entry
                            )
                            _unrealized = _calc_unrealized_pnl(_outcome, _avg_entry, _cur_price, _shares)
                            _update_position_touch(
                                conn,
                                int(position["position_id"]),
                                current_price=_cur_price,
                                current_value=_shares * _cur_price,
                                pnl_usdc=_unrealized,
                            )
                    except Exception:
                        logger.exception(
                            "Error managing paused position %s for strategy %s",
                            position.get("position_id"),
                            strategy_id,
                        )

                conn.commit()
                logger.debug(
                    "Paused strategy %s: %d open, %d exited this cycle",
                    strategy_id,
                    len(open_positions) - exited_count,
                    exited_count,
                )

            except Exception:
                logger.exception(
                    "Failed to manage paused positions for strategy %s", strategy_id
                )

    except Exception:
        logger.exception("_manage_paused_strategy_positions failed")


def process_strategies(
    conn: sqlite3.Connection,
    clob_client: Optional[ClobClient],
    *,
    profile: Optional[InstanceProfile] = None,
    initialized_feeds: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Main processing loop: load active strategies and process new positions.
    """
    try:
        cursor = conn.cursor()

        # --- Auto-resume check for paused insider_copy strategies ---
        # Runs before the main active loop so that a recovered strategy can
        # participate in the current cycle immediately.
        _run_auto_resume_checks(conn)

        # Load all active strategies eligible for processing.
        # lifecycle_stage filter: only paper and promoted strategies run.
        # research and retired strategies are excluded from the runner loop.
        # COALESCE handles legacy rows where lifecycle_stage is NULL (treated as paper).
        cursor.execute("""
            SELECT id, wallet, label, strategy_type, parameters, status,
                   trades_triggered, pnl_usdc, created_at, updated_at
            FROM strategies
            WHERE status = 'active'
              AND COALESCE(lifecycle_stage, 'paper') IN ('paper', 'promoted')
        """)

        strategies = [dict_from_row(row) for row in cursor.fetchall()]
        logger.info(f"Processing {len(strategies)} active strategies")

        enabled_strategies = profile.enabled_strategies if profile else ()
        enabled_feeds = profile.enabled_feeds if profile else ()

        # Load world boosts once per cycle — shared across all strategies, not fetched per strategy
        cycle_world_boosts = _load_world_boosts()

        # Load conviction engine once per cycle — reads 2D Beta-Binomial cell posteriors
        # from brain.  Returns None if module/brain unavailable (passthrough mode).
        cycle_conviction_engine = _load_conviction_engine()

        for strategy in strategies:
            try:
                strategy_id = strategy["id"]
                strategy = _load_strategy(strategy)
                wallet = strategy["wallet"]
                label = strategy.get("label", "N/A")
                strategy_type = strategy.get("strategy_type", "")

                if get_strategy_plugin(strategy_type) is not None:
                    # --- Filter by enabled_strategies ---
                    if enabled_strategies and strategy_type.lower() not in enabled_strategies:
                        logger.debug(
                            "Skipping strategy %s (%s): not in enabled_strategies",
                            strategy_id,
                            strategy_type,
                        )
                        continue

                    plugin = get_strategy_plugin(strategy_type)

                    # --- Feed requirement check ---
                    if enabled_feeds and plugin is not None:
                        missing_feeds = [
                            f for f in getattr(plugin, "required_feeds", ())
                            if f not in enabled_feeds
                        ]
                        if missing_feeds:
                            logger.warning(
                                "Skipping strategy %s (%s): requires feeds %s not in enabled_feeds",
                                strategy_id,
                                strategy_type,
                                missing_feeds,
                            )
                            continue

                    # --- Cadence check ---
                    if plugin is not None and not _is_strategy_due(strategy_id, plugin):
                        logger.debug(
                            "Skipping strategy %s (%s): not yet due (cadence)",
                            strategy_id,
                            strategy_type,
                        )
                        continue

                    logger.info(
                        "Processing plugin strategy %s (%s) for wallet %s",
                        strategy_id,
                        strategy_type,
                        wallet,
                    )
                    process_plugin_strategy(
                        conn, strategy,
                        profile=profile,
                        world_boosts=cycle_world_boosts,
                        conviction_engine=cycle_conviction_engine,
                    )
                    _mark_strategy_ran(strategy_id)
                    continue

                # Get last_copied_at from followed_wallets
                cursor.execute("""
                    SELECT last_copied_at FROM followed_wallets WHERE wallet = ?
                """, (wallet,))
                
                fw_row = cursor.fetchone()
                last_copied_at = None
                if fw_row and fw_row[0]:
                    try:
                        last_copied_at = datetime.fromisoformat(fw_row[0])
                    except (ValueError, TypeError):
                        logger.warning("Invalid last_copied_at timestamp: %s", fw_row[0])

                # Query new positions from watchlist_trades
                if last_copied_at:
                    cursor.execute("""
                        SELECT wallet, condition_id, outcome, title, side, price, size, notional, timestamp
                        FROM watchlist_trades
                        WHERE wallet = ?
                          AND side = 'BUY'
                          AND timestamp > ?
                        ORDER BY timestamp DESC
                    """, (wallet, int(last_copied_at.timestamp())))
                else:
                    # If no last_copied_at, get recent positions (last 1 hour)
                    one_hour_ago = int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp())
                    cursor.execute("""
                        SELECT wallet, condition_id, outcome, title, side, price, size, notional, timestamp
                        FROM watchlist_trades
                        WHERE wallet = ?
                          AND side = 'BUY'
                          AND timestamp > ?
                        ORDER BY timestamp DESC
                    """, (wallet, one_hour_ago))

                new_positions = [dict_from_row(row) for row in cursor.fetchall()]
                logger.info(f"Strategy {strategy_id} ({label}): found {len(new_positions)} new positions")

                for position in new_positions:
                    try:
                        condition_id = position.get("condition_id")
                        outcome = position.get("outcome")
                        title = position.get("title")
                        price = position.get("price", 0)
                        
                        # Evaluate strategy
                        should_execute, reason = evaluate_strategy(strategy, position)
                        logger.info(
                            f"Strategy {strategy_id} ({strategy_type}), wallet {wallet}, "
                            f"condition {condition_id}: {reason}"
                        )

                        executed = False
                        if should_execute and strategy_type != "signal_only":
                            # Execute — only advance watermark when execution returns True
                            executed = execute_strategy_position(conn, strategy, position, clob_client)
                        elif strategy_type == "signal_only":
                            # Always log signal_only as pending
                            executed = execute_strategy_position(conn, strategy, position, clob_client)

                        # Only advance watermark when execution actually succeeded
                        if executed:
                            cursor.execute("""
                                UPDATE followed_wallets
                                SET last_copied_at = ?,
                                    updated_at = ?
                                WHERE wallet = ?
                            """, (datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat(), wallet))
                            sync_followed_wallet_membership(conn, wallet)
                            conn.commit()

                    except Exception as e:
                        logger.error(
                            f"Error processing position {condition_id} for strategy {strategy_id}: {e}\n"
                            f"{traceback.format_exc()}"
                        )
                        continue

            except Exception as e:
                logger.error(f"Error processing strategy {strategy_id}: {e}\n{traceback.format_exc()}")
                continue

        # --- Manage open positions for PAUSED strategies ---
        # Paused strategies don't enter new positions but their open positions
        # still need stop-loss checks, resolution exits, and P&L updates every cycle.
        _manage_paused_strategy_positions(conn)

    except Exception as e:
        logger.error(f"Exception in process_strategies: {e}\n{traceback.format_exc()}")


def main():
    """Main service entry point."""
    global running

    parser = argparse.ArgumentParser(description="Run Sniper strategy service")
    parser.add_argument(
        "--interval",
        type=int,
        default=LOOP_INTERVAL_SECONDS,
        help="Loop interval in seconds (default: 60)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one strategy-processing cycle and exit",
    )
    args = parser.parse_args()

    logger.info("=" * 80)
    logger.info("Strategy Runner Service Starting")
    logger.info("=" * 80)

    # --- Load instance profile ---
    try:
        profile = load_instance_profile()
    except Exception as exc:
        logger.exception("Failed to load instance profile, using safe defaults: %s", exc)
        profile = load_instance_profile(env={})  # safe defaults
    logger.info(
        "Instance profile: tag=%s role=%s enabled_strategies=%s enabled_feeds=%s "
        "bankroll=%.2f loop=%ds",
        profile.instance_tag,
        profile.role,
        profile.enabled_strategies or "(all)",
        profile.enabled_feeds or "(all)",
        profile.bankroll_usdc,
        profile.default_loop_seconds,
    )

    # --- Initialize required feeds ---
    initialized_feeds: Dict[str, Any] = {}
    if profile.enabled_feeds:
        for feed_id in profile.enabled_feeds:
            adapter = get_feed_adapter(feed_id)
            if adapter is None:
                logger.warning("Feed '%s' not found in registry, skipping", feed_id)
                continue
            if not adapter.validate_config():
                logger.warning("Feed '%s' failed validate_config(), skipping", feed_id)
                continue
            try:
                tmp_conn = get_db_connection()
                adapter.warmup(tmp_conn)
                tmp_conn.close()
            except Exception as exc:
                logger.warning("Feed '%s' warmup failed: %s", feed_id, exc)
                continue
            initialized_feeds[feed_id] = adapter
            logger.info("Feed '%s' initialized successfully", feed_id)
    else:
        logger.info("No enabled_feeds specified; feed requirement checks disabled")

    _start_initialized_feeds(initialized_feeds)

    # Use profile's loop interval if --interval was not explicitly set
    loop_interval = args.interval
    if loop_interval == LOOP_INTERVAL_SECONDS and profile.default_loop_seconds != LOOP_INTERVAL_SECONDS:
        loop_interval = profile.default_loop_seconds

    # Write PID file
    try:
        with open(PID_FILE, 'w') as f:
            f.write(str(os.getpid()))
        logger.info(f"PID file written to {PID_FILE}")
    except Exception as e:
        logger.error(f"Failed to write PID file: {e}")

    # Register signal handlers
    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.signal(signal.SIGINT, handle_sigint)

    # Only real-trading instances need a CLOB client. Feed-only and paper-only
    # profiles should stay quiet here instead of logging a fake missing-key error.
    clob_client = None
    if should_initialize_clob_client(profile):
        clob_client = get_clob_client()
        if clob_client:
            logger.info("CLOB client initialized successfully")
        else:
            logger.warning("CLOB client initialization failed, real execution unavailable")
    else:
        logger.info(
            "Skipping CLOB client initialization for instance %s (real trading disabled)",
            profile.instance_tag,
        )

    # Main loop
    try:
        while running:
            try:
                conn = get_db_connection()
                _tick_initialized_feeds(conn, initialized_feeds)
                process_strategies(conn, clob_client, profile=profile, initialized_feeds=initialized_feeds)
                conn.close()
            except Exception as e:
                logger.error(f"Exception in main loop: {e}\n{traceback.format_exc()}")
            if args.once:
                break
            # Sleep before next iteration
            time.sleep(max(1, int(loop_interval)))

    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received, shutting down...")
    except Exception as e:
        logger.error(f"Unexpected exception in main loop: {e}\n{traceback.format_exc()}")
    finally:
        # Cleanup
        logger.info("Cleaning up...")
        _stop_initialized_feeds(initialized_feeds)
        try:
            if PID_FILE.exists():
                PID_FILE.unlink()
                logger.info("PID file removed")
        except Exception as e:
            logger.error(f"Error removing PID file: {e}")

        logger.info("=" * 80)
        logger.info("Strategy Runner Service Stopped")
        logger.info("=" * 80)


if __name__ == "__main__":
    main()
