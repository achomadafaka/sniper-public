"""
Market metadata, outcomes, redemption log, and legacy position/low-buy writes.
"""
import json
import sqlite3
import time
from typing import Any, Dict, Iterable, List

from sniper.persistence.db_utils import _normalize_tx_hash, _normalize_wallet


def _lazy_is_trade_deleted(conn: sqlite3.Connection, tx_hash: str) -> bool:
    from db import is_trade_deleted
    return is_trade_deleted(conn, tx_hash)


def redemption_success_exists(
    conn: sqlite3.Connection,
    *,
    wallet: str,
    condition_id: str,
    outcome: str,
) -> bool:
    wallet_key = _normalize_wallet(wallet)
    condition_key = str(condition_id or "").strip().lower()
    outcome_key = str(outcome or "").strip().lower()
    if not wallet_key or not condition_key or not outcome_key:
        return False
    row = conn.execute(
        """
        SELECT 1
        FROM redemption_log
        WHERE wallet = ?
          AND condition_id = ?
          AND outcome = ?
          AND status = 'success'
        LIMIT 1
        """,
        (wallet_key, condition_key, outcome_key),
    ).fetchone()
    return row is not None


def latest_redemption_success(
    conn: sqlite3.Connection,
    *,
    wallet: str,
    condition_id: str,
    outcome: str,
) -> Dict[str, Any] | None:
    """Return metadata for the most recent successful redemption, or None."""
    wallet_key = _normalize_wallet(wallet)
    condition_key = str(condition_id or "").strip().lower()
    outcome_key = str(outcome or "").strip().lower()
    if not wallet_key or not condition_key or not outcome_key:
        return None
    row = conn.execute(
        """
        SELECT tx_hash, amount_claimed, resolved_price, threshold_price,
               attempted_ts, updated_at
        FROM redemption_log
        WHERE wallet = ?
          AND condition_id = ?
          AND outcome = ?
          AND status = 'success'
        ORDER BY attempted_ts DESC
        LIMIT 1
        """,
        (wallet_key, condition_key, outcome_key),
    ).fetchone()
    if row is None:
        return None
    return {
        "tx_hash": str(row["tx_hash"] or ""),
        "amount_claimed": float(row["amount_claimed"]) if row["amount_claimed"] is not None else None,
        "resolved_price": float(row["resolved_price"]) if row["resolved_price"] is not None else None,
        "threshold_price": float(row["threshold_price"]) if row["threshold_price"] is not None else None,
        "attempted_ts": int(row["attempted_ts"] or 0),
        "updated_at": str(row["updated_at"] or ""),
    }


def record_redemption_attempt(
    conn: sqlite3.Connection,
    *,
    wallet: str,
    condition_id: str,
    outcome: str,
    attempt_mode: str,
    status: str,
    resolved_price: float | None = None,
    threshold_price: float | None = None,
    tx_hash: str = "",
    amount_claimed: float | None = None,
    error: str = "",
    metadata: Dict[str, Any] | None = None,
    attempted_ts: int = 0,
) -> int:
    wallet_key = _normalize_wallet(wallet)
    condition_key = str(condition_id or "").strip().lower()
    outcome_key = str(outcome or "").strip().lower()
    if not wallet_key or not condition_key or not outcome_key:
        return 0

    metadata_json = "{}"
    if metadata:
        try:
            metadata_json = json.dumps(metadata, sort_keys=True)
        except (TypeError, ValueError):
            metadata_json = "{}"

    now_ts = int(attempted_ts or time.time())
    cursor = conn.execute(
        """
        INSERT INTO redemption_log (
            wallet, condition_id, outcome,
            attempt_mode, status,
            resolved_price, threshold_price,
            tx_hash, amount_claimed, error,
            metadata_json, attempted_ts, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """,
        (
            wallet_key,
            condition_key,
            outcome_key,
            str(attempt_mode or "").strip().lower() or "dry_run",
            str(status or "").strip().lower() or "dry_run",
            resolved_price,
            threshold_price,
            str(tx_hash or "").strip(),
            amount_claimed,
            str(error or "").strip(),
            metadata_json,
            now_ts,
        ),
    )
    return int(cursor.lastrowid or 0)


def upsert_market_metadata(
    conn: sqlite3.Connection,
    condition_id: str,
    *,
    title: str = "",
    slug: str = "",
    event_slug: str = "",
    end_date_iso: str = "",
    category: str = "",
    category_source: str = "",
    volume_24h: float = 0.0,
    is_open: int = 1,
) -> None:
    """Upsert a single market's metadata into the market_metadata table.

    Called by the stream's metadata refresh loop after fetching the market index.
    Thread-safe: runs inside asyncio.to_thread in stream_low_buys.py.
    """
    now_ts = int(time.time())
    conn.execute(
        """
        INSERT INTO market_metadata
            (condition_id, title, slug, event_slug, end_date_iso, category, category_source,
             volume_24h, is_open, last_synced_ts, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(condition_id) DO UPDATE SET
            title          = excluded.title,
            slug           = excluded.slug,
            event_slug     = excluded.event_slug,
            end_date_iso   = excluded.end_date_iso,
            category       = excluded.category,
            category_source = excluded.category_source,
            volume_24h     = excluded.volume_24h,
            is_open        = excluded.is_open,
            last_synced_ts = excluded.last_synced_ts,
            updated_at     = CURRENT_TIMESTAMP
        """,
        (condition_id, title, slug, event_slug, end_date_iso, category, category_source,
         volume_24h, is_open, now_ts),
    )


def get_market_metadata(
    conn: sqlite3.Connection,
    condition_id: str,
) -> sqlite3.Row | None:
    """Fetch a single market's metadata row, or None if not yet synced."""
    return conn.execute(
        "SELECT * FROM market_metadata WHERE condition_id = ?",
        (condition_id,),
    ).fetchone()


def bulk_upsert_market_metadata(
    conn: sqlite3.Connection,
    markets: list[dict],
    commit: bool = True,
) -> int:
    """Upsert a batch of market metadata dicts.

    Each dict must have at minimum a 'condition_id' key.  Optional keys:
    title, slug, event_slug, end_date_iso, category, category_source, volume_24h, is_open.

    Returns the number of rows upserted.
    """
    now_ts = int(time.time())
    rows = [
        (
            str(m.get("condition_id", "")),
            str(m.get("title", "")),
            str(m.get("slug", "")),
            str(m.get("event_slug", "")),
            str(m.get("end_date_iso", "")),
            str(m.get("category", "")),
            str(m.get("category_source", "")),
            float(m.get("volume_24h", 0.0)),
            int(m.get("is_open", 1)),
            now_ts,
        )
        for m in markets
        if m.get("condition_id")
    ]
    if not rows:
        return 0
    conn.executemany(
        """
        INSERT INTO market_metadata
            (condition_id, title, slug, event_slug, end_date_iso, category, category_source,
             volume_24h, is_open, last_synced_ts, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(condition_id) DO UPDATE SET
            title          = excluded.title,
            slug           = excluded.slug,
            event_slug     = excluded.event_slug,
            end_date_iso   = excluded.end_date_iso,
            category       = excluded.category,
            category_source = excluded.category_source,
            volume_24h     = excluded.volume_24h,
            is_open        = excluded.is_open,
            last_synced_ts = excluded.last_synced_ts,
            updated_at     = CURRENT_TIMESTAMP
        """,
        rows,
    )
    if commit:
        conn.commit()
    return len(rows)


def bulk_upsert_market_tags(
    conn: sqlite3.Connection,
    tags_by_condition: dict,
    commit: bool = True,
) -> int:
    """Upsert `(condition_id, tag, tag_label)` rows into market_tags.

    `tags_by_condition` maps `condition_id -> list[(slug, label)]`.
    Idempotent via INSERT OR IGNORE; existing rows are untouched (slugs
    are opaque identifiers and shouldn't change).

    Returns the number of new rows inserted.
    """
    rows = []
    for cid_raw, pairs in (tags_by_condition or {}).items():
        cid = str(cid_raw or "").strip().lower()
        if not cid:
            continue
        for pair in (pairs or []):
            if not isinstance(pair, (tuple, list)) or len(pair) < 2:
                continue
            slug = str(pair[0] or "").strip().lower()
            if not slug:
                continue
            label = str(pair[1] or "").strip() or None
            rows.append((cid, slug, label))
    if not rows:
        return 0
    cur = conn.executemany(
        "INSERT OR IGNORE INTO market_tags (condition_id, tag, tag_label) VALUES (?, ?, ?)",
        rows,
    )
    if commit:
        conn.commit()
    return max(cur.rowcount, 0)


def fetch_market_tags_for_cids(
    conn: sqlite3.Connection,
    condition_ids: Iterable[str],
) -> dict[str, list[str]]:
    """Return `{condition_id → [leaf_tag, ...]}` from market_tags.

    Used by the wallet-analysis protocol §8 tag-grain helper. Returns
    leaf tag slugs only (the `tag` column); callers apply the §4.6 skip
    list themselves. Empty list when no rows match.
    """
    cids = [str(c or "").strip().lower() for c in (condition_ids or [])]
    cids = [c for c in cids if c]
    if not cids:
        return {}
    placeholders = ",".join("?" * len(cids))
    cur = conn.execute(
        f"""
        SELECT condition_id, tag
        FROM market_tags
        WHERE condition_id IN ({placeholders})
        ORDER BY condition_id, tag
        """,
        cids,
    )
    out: dict[str, list[str]] = {}
    for cid, tag in cur.fetchall():
        out.setdefault(cid, []).append(tag)
    return out


def fetch_tag_nav_category_map(
    conn: sqlite3.Connection,
) -> dict[str, str]:
    """Return `{leaf_tag → nav_category}` from tag_nav_category.

    Empty dict when the table is unseeded — callers must check this and
    fall back to `hierarchy_source='none'` per spec §8.1.
    """
    try:
        cur = conn.execute("SELECT tag, nav_category FROM tag_nav_category")
    except sqlite3.OperationalError as exc:
        if "no such table: tag_nav_category" in str(exc):
            return {}
        raise
    return {tag: nav for tag, nav in cur.fetchall()}


def bulk_mark_markets_closed(
    conn: sqlite3.Connection,
    condition_ids: Iterable[str],
    *,
    chunk_size: int = 500,
    commit: bool = True,
) -> int:
    """Mark a batch of markets as `is_open = 0` in market_metadata.

    Used by the streamer's meta_refresh loop when the Polymarket index stops
    returning a condition_id — the market has closed/expired and should no
    longer be advertised to downstream strategies scanning `is_open = 1`.

    Without this writeback, market_metadata accumulates stale "open" rows
    forever (evicted from the in-memory dict but never persisted). That was
    the upstream cause of the 2026-04-20 stink-bid-tennis incident where
    strategy 41 kept rediscovering dead tennis rows every 15 minutes and
    wasted Gamma calls on them.

    Returns the number of rows that actually changed state (previously
    is_open=1, now is_open=0). Rows already closed are idempotently skipped.
    Chunked to avoid building a giant SQL expression on huge eviction sets.
    """
    cids = [str(c) for c in (condition_ids or []) if c]
    if not cids:
        return 0

    total_changed = 0
    for start in range(0, len(cids), chunk_size):
        batch = cids[start : start + chunk_size]
        placeholders = ",".join("?" * len(batch))
        cur = conn.execute(
            f"""
            UPDATE market_metadata
            SET is_open = 0,
                last_synced_ts = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE condition_id IN ({placeholders})
              AND is_open = 1
            """,
            [int(time.time()), *batch],
        )
        total_changed += cur.rowcount or 0

    if commit:
        conn.commit()
    return total_changed


def upsert_market_outcome(
    conn: sqlite3.Connection,
    condition_id: str,
    outcome: str,
    resolved_price: float,
    resolved_at: int,
    source: str = "polymarket_markets_api",
) -> None:
    """Upsert one market settlement row (Phase 2 scaffold)."""
    if not condition_id or not outcome:
        return
    conn.execute(
        """
        INSERT INTO market_outcomes
            (condition_id, outcome, resolved_price, resolved_at, source, updated_at)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(condition_id, outcome) DO UPDATE SET
            resolved_price = excluded.resolved_price,
            resolved_at    = excluded.resolved_at,
            source         = excluded.source,
            updated_at     = CURRENT_TIMESTAMP
        """,
        (str(condition_id), str(outcome).lower(), float(resolved_price), int(resolved_at or 0), str(source or "")),
    )


def get_market_outcome(
    conn: sqlite3.Connection,
    condition_id: str,
    outcome: str,
) -> sqlite3.Row | None:
    """Fetch one market settlement row (Phase 2 scaffold)."""
    return conn.execute(
        """
        SELECT *
        FROM market_outcomes
        WHERE condition_id = ? AND LOWER(outcome) = LOWER(?)
        LIMIT 1
        """,
        (str(condition_id), str(outcome)),
    ).fetchone()


def record_low_buy_trade(
    conn: sqlite3.Connection,
    trade: Dict[str, Any],
    ws_price: float,
    ws_timestamp_ms: int,
    market_volume_24h: float | None = None,
    source: str = "realtime",
    trigger_type: str = "low_price_entry",
    trigger_reason: str = "",
) -> bool:
    tx_hash = _normalize_tx_hash(trade.get("transactionHash"))
    if not tx_hash:
        return False
    if _lazy_is_trade_deleted(conn, tx_hash):
        return False

    # Use explicit transaction so low_buy_trades insert and candidate_users update
    # are always atomic — a partial write would leave analytics in inconsistent state.
    try:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO low_buy_trades (
                transaction_hash,
                proxy_wallet,
                condition_id,
                asset,
                side,
                price,
                size,
                notional,
                timestamp,
                title,
                slug,
                event_slug,
                outcome,
                ws_price,
                ws_timestamp_ms,
                market_volume_24h,
                source,
                trigger_type,
                trigger_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tx_hash,
                _normalize_wallet(trade.get("proxyWallet")),
                trade.get("conditionId"),
                trade.get("asset"),
                trade.get("side"),
                float(trade.get("price", 0.0)),
                float(trade.get("size", 0.0)),
                float(trade.get("price", 0.0)) * float(trade.get("size", 0.0)),
                int(trade.get("timestamp", 0)),
                trade.get("title"),
                trade.get("slug"),
                trade.get("eventSlug"),
                trade.get("outcome"),
                ws_price,
                ws_timestamp_ms,
                float(market_volume_24h or 0.0),
                source,
                str(trigger_type or "low_price_entry"),
                str(trigger_reason or ""),
            ),
        )

        inserted = cur.rowcount > 0
        if inserted:
            wallet = _normalize_wallet(trade.get("proxyWallet"))
            ts = int(trade.get("timestamp", 0))
            conn.execute(
                """
                INSERT INTO candidate_users (
                    wallet,
                    first_seen_ts,
                    last_seen_ts,
                    low_buy_hits,
                    last_condition_id,
                    last_title
                ) VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(wallet) DO UPDATE SET
                    last_seen_ts = excluded.last_seen_ts,
                    low_buy_hits = candidate_users.low_buy_hits + 1,
                    last_condition_id = excluded.last_condition_id,
                    last_title = excluded.last_title,
                    stage1_updated_at = CURRENT_TIMESTAMP
                """,
                (
                    wallet,
                    ts,
                    ts,
                    trade.get("conditionId"),
                    trade.get("title"),
                ),
            )

        conn.commit()
        return inserted
    except sqlite3.Error:
        conn.rollback()
        raise


def upsert_position(
    conn: sqlite3.Connection,
    trade: Dict[str, Any],
    market_meta: Dict[str, Any] | None = None,
) -> None:
    """Upsert a live aggregated position keyed by (wallet, condition_id, outcome)."""
    wallet = _normalize_wallet(trade.get("wallet") or trade.get("proxyWallet"))
    condition_id = str(trade.get("condition_id") or trade.get("conditionId") or "").strip()
    outcome = str(trade.get("outcome") or "").strip().lower()
    if not wallet or not condition_id or not outcome:
        return

    price = float(trade.get("price") or 0.0)
    size = float(trade.get("size") or 0.0)
    if size <= 0:
        return
    notional = max(0.0, price * size)
    timestamp = int(trade.get("timestamp") or 0)
    trigger_type = str(trade.get("trigger_type") or "").strip()

    market_meta = market_meta or {}
    current_price = market_meta.get("current_price")
    end_date_iso = market_meta.get("end_date_iso")
    category = market_meta.get("category")
    if current_price is not None:
        try:
            current_price = float(current_price)
        except (TypeError, ValueError):
            current_price = None
    if end_date_iso is not None:
        end_date_iso = str(end_date_iso)
    if category is not None:
        category = str(category)

    conn.execute(
        """
        INSERT INTO positions (
            wallet,
            condition_id,
            outcome,
            total_shares,
            avg_entry_price,
            total_notional,
            trade_count,
            first_trade_ts,
            last_trade_ts,
            current_price,
            market_end_date_iso,
            market_category,
            is_open,
            trigger_type,
            updated_at
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, CURRENT_TIMESTAMP
        )
        ON CONFLICT(wallet, condition_id, outcome) DO UPDATE SET
            total_shares = positions.total_shares + excluded.total_shares,
            total_notional = positions.total_notional + excluded.total_notional,
            trade_count = positions.trade_count + 1,
            first_trade_ts = CASE
                WHEN positions.first_trade_ts <= 0 THEN excluded.first_trade_ts
                WHEN excluded.first_trade_ts <= 0 THEN positions.first_trade_ts
                ELSE MIN(positions.first_trade_ts, excluded.first_trade_ts)
            END,
            last_trade_ts = CASE
                WHEN excluded.last_trade_ts > positions.last_trade_ts THEN excluded.last_trade_ts
                ELSE positions.last_trade_ts
            END,
            avg_entry_price = CASE
                WHEN (positions.total_shares + excluded.total_shares) > 0
                    THEN (positions.total_notional + excluded.total_notional) /
                         (positions.total_shares + excluded.total_shares)
                ELSE 0
            END,
            current_price = CASE
                WHEN excluded.current_price IS NOT NULL THEN excluded.current_price
                ELSE positions.current_price
            END,
            market_end_date_iso = CASE
                WHEN excluded.market_end_date_iso IS NOT NULL THEN excluded.market_end_date_iso
                ELSE positions.market_end_date_iso
            END,
            market_category = CASE
                WHEN excluded.market_category IS NOT NULL THEN excluded.market_category
                ELSE positions.market_category
            END,
            trigger_type = CASE
                WHEN excluded.trigger_type IS NOT NULL AND excluded.trigger_type <> ''
                    THEN excluded.trigger_type
                ELSE positions.trigger_type
            END,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            wallet,
            condition_id,
            outcome,
            size,
            (notional / size) if size > 0 else 0.0,
            notional,
            1,
            timestamp,
            timestamp,
            current_price,
            end_date_iso,
            category,
            trigger_type or None,
        ),
    )


## REMOVED: wallets_for_analysis (dead code, replaced by discovery pipeline — audit I1 2026-03-30)


def wallets_for_open_position_scan(
    conn: sqlite3.Connection,
    limit: int,
    stale_after_seconds: int,
) -> List[str]:
    """Return wallets due for an open-position scan.

    Strategy (two-phase, fast):
    1. Primary: use wallet_analysis_queue (indexed, maintained by streamer) — these
       wallets already have priority scores and recency info. Covers the vast majority
       of active wallets seen in the stream.
    2. Fallback: a lightweight UNION of high-value sources (watchlist, followed, flagged,
       high-winrate) for wallets that may never have appeared in the stream but are
       tracked for other reasons. These tables are small (<10K rows each) and indexed.

    Both paths exclude ignored wallets and recently-analyzed wallets.

    Tiered staleness (added 2026-03-14):
    - Never analyzed → always eligible (stale_after_seconds)
    - Watchlisted / high_winrate / insider-class → stale_after_seconds (default 10min)
    - Medium interest (insider 30-49) → 4x staleness (~40min)
    - Confirmed noise (insider<30, low_signal/spec_bot, 50+ trades) → 24x (~4h)
    This reduces wasted API calls on wallets that never produce signals.
    """
    stale_ts = int(stale_after_seconds)
    # Tier multipliers — noise wallets analyzed much less frequently
    stale_medium = stale_ts * 4       # ~40 min for medium-interest
    stale_noise = stale_ts * 24       # ~4 hours for confirmed noise
    stale_dead = stale_ts * 100       # ~7 days for dead wallets (0 positions, no activity)
    # Guard: wallet_analysis_queue may not exist if discovery schema hasn't been
    # initialized yet (created by streamer, not init_db). Fall back to empty primary_src.
    _has_waq = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='wallet_analysis_queue'"
    ).fetchone() is not None

    _primary_src_sql = """
            SELECT waq.wallet,
                   waq.last_event_ts AS seen_ts,
                   waq.priority_score
            FROM wallet_analysis_queue waq
            WHERE waq.wallet IS NOT NULL AND waq.wallet <> ''
    """ if _has_waq else """
            SELECT NULL AS wallet, 0 AS seen_ts, 0 AS priority_score
            WHERE 0  -- empty result set
    """

    rows = conn.execute(
        f"""
        WITH primary_src AS (
            -- wallet_analysis_queue: maintained by streamer, fully indexed
            {_primary_src_sql}
        ),
        fallback_src AS (
            -- High-value wallets that may not appear in the stream queue
            SELECT wallet, added_ts AS seen_ts, 1000 AS priority_score
            FROM watchlist_wallets
            UNION ALL
            SELECT wallet, MAX(COALESCE(last_synced_ts, added_ts, 0)) AS seen_ts, 900 AS priority_score
            FROM followed_positions
            GROUP BY wallet
            UNION ALL
            SELECT wallet, flagged_ts AS seen_ts, 800 AS priority_score
            FROM flagged_users
            UNION ALL
            SELECT wallet, analyzed_ts AS seen_ts, 500 AS priority_score
            FROM user_analysis
            WHERE high_winrate_qualified = 1
        ),
        combined AS (
            SELECT wallet, seen_ts, priority_score FROM primary_src
            UNION ALL
            SELECT wallet, seen_ts, priority_score FROM fallback_src
        ),
        merged AS (
            SELECT
                wallet,
                MAX(COALESCE(seen_ts, 0)) AS seen_ts,
                MAX(priority_score) AS priority_score
            FROM combined
            WHERE wallet IS NOT NULL AND wallet <> ''
            GROUP BY wallet
        )
        SELECT m.wallet
        FROM merged m
        LEFT JOIN user_analysis a  ON a.wallet  = m.wallet
        LEFT JOIN ignored_wallets iw ON iw.wallet = m.wallet
        LEFT JOIN watchlist_wallets ww ON ww.wallet = m.wallet
        WHERE (
                a.wallet IS NULL
             OR a.analyzed_ts < (strftime('%s', 'now') - CASE
                    -- Watchlisted wallets: always high priority
                    WHEN ww.wallet IS NOT NULL THEN ?
                    -- Insider-class or high_winrate: high priority
                    WHEN a.classification IN ('emerging_insider', 'insider_candidate', 'high_winrate_review') THEN ?
                    -- Interesting wallets (insider >= 50): high priority
                    WHEN COALESCE(a.insider_score, 0) >= 50 THEN ?
                    -- Medium interest (insider 30-49): 4x staleness
                    WHEN COALESCE(a.insider_score, 0) >= 30 THEN ?
                    -- Dead wallets (0 positions, low score, no activity): ~7 day staleness
                    WHEN COALESCE(a.insider_score, 0) < 30
                         AND COALESCE(a.position_count, 0) = 0
                         AND a.classification IN ('low_signal', 'speculator_bot')
                         AND COALESCE(a.closed_sample_size, 0) >= 10 THEN ?
                    -- Confirmed noise (low score, stable class, enough data): 24x staleness
                    WHEN COALESCE(a.insider_score, 0) < 30
                         AND a.classification IN ('low_signal', 'speculator_bot')
                         AND COALESCE(a.closed_sample_size, 0) >= 50 THEN ?
                    -- Everything else: 4x staleness
                    ELSE ?
                END)
        )
          AND (COALESCE(a.is_bot, 0) = 0 OR ww.wallet IS NOT NULL OR COALESCE(a.insider_score, 0) >= 50)
          AND iw.wallet IS NULL
        ORDER BY m.priority_score DESC, m.seen_ts DESC, m.wallet ASC
        LIMIT ?
        """,
        (stale_ts, stale_ts, stale_ts, stale_medium, stale_dead, stale_noise, stale_medium, int(limit)),
    ).fetchall()
    return [str(row["wallet"] or "") for row in rows if row["wallet"]]
