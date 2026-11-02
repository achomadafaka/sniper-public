"""
Followed wallets, watchlists, position snapshots, and research queries.
"""
import datetime as dt
import json
import logging
import sqlite3
import time
from typing import Any, Dict, Iterable, List, Sequence

from sniper.persistence.db_core import _table_exists, with_db_init_lock
from sniper.persistence.db_utils import (
    _normalize_wallet, _normalize_tx_hash,
    _normalize_condition_id, _normalize_outcome,
    _coerce_float, _coerce_int, _coerce_history_ts,
    _followed_wallet_activity_key, _classify_title_domain,
    is_synthetic_ws_hash,
)

logger = logging.getLogger(__name__)


# ── wallet directory schema and write helpers ──

def _json_text(value: Any) -> str:
    if isinstance(value, str):
        text = value.strip()
        return text or "{}"
    try:
        return json.dumps(value or {}, sort_keys=True)
    except Exception:
        return "{}"


def _copy_status_from_followed_status(status: str | None) -> str | None:
    key = str(status or "").strip().lower()
    if not key:
        return None
    mapping = {
        "active": "active",
        "paused": "paused",
        "gate0_failed": "gate0_failed",
        "unfollowed": "inactive",
        "removed": "inactive",
    }
    return mapping.get(key, key)


def upsert_wallet_registry(
    conn: sqlite3.Connection,
    *,
    wallet: str,
    display_label: str = "",
    status: str | None = None,
    gate0_status: str | None = None,
    copy_status: str | None = None,
    analysis_status: str | None = None,
    classification: str | None = None,
    notes: str = "",
    first_seen_ts: int = 0,
    last_seen_ts: int = 0,
) -> None:
    normalized = _normalize_wallet(wallet)
    if not normalized:
        return
    display_label_value = str(display_label or "").strip()
    status_value = str(status or "").strip()
    gate0_status_value = str(gate0_status or "").strip()
    copy_status_value = str(copy_status or "").strip()
    analysis_status_value = str(analysis_status or "").strip()
    classification_value = str(classification or "").strip()
    notes_value = str(notes or "").strip()
    first_seen_value = int(first_seen_ts or 0)
    last_seen_value = int(last_seen_ts or 0)
    conn.execute(
        """
        INSERT OR IGNORE INTO wallet_registry (
            wallet, display_label, status, gate0_status, copy_status,
            analysis_status, classification, notes, first_seen_ts, last_seen_ts
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            normalized,
            display_label_value,
            status_value or "active",
            gate0_status_value or "unknown",
            copy_status_value or "not_followed",
            analysis_status_value or "unknown",
            classification_value,
            notes_value,
            first_seen_value,
            last_seen_value,
        ),
    )
    conn.execute(
        """
        UPDATE wallet_registry SET
            display_label = CASE
                WHEN ? <> '' THEN ?
                ELSE wallet_registry.display_label
            END,
            status = CASE
                WHEN ? <> '' THEN ?
                ELSE wallet_registry.status
            END,
            gate0_status = CASE
                WHEN ? <> '' THEN ?
                ELSE wallet_registry.gate0_status
            END,
            copy_status = CASE
                WHEN ? <> '' THEN ?
                ELSE wallet_registry.copy_status
            END,
            analysis_status = CASE
                WHEN ? <> '' THEN ?
                ELSE wallet_registry.analysis_status
            END,
            classification = CASE
                WHEN ? <> '' THEN ?
                ELSE wallet_registry.classification
            END,
            notes = CASE
                WHEN ? <> '' THEN ?
                ELSE wallet_registry.notes
            END,
            first_seen_ts = CASE
                WHEN wallet_registry.first_seen_ts <= 0 AND ? > 0
                    THEN ?
                ELSE wallet_registry.first_seen_ts
            END,
            last_seen_ts = CASE
                WHEN ? > wallet_registry.last_seen_ts
                    THEN ?
                ELSE wallet_registry.last_seen_ts
            END,
            updated_at = CURRENT_TIMESTAMP
        WHERE wallet = ?
        """,
        (
            display_label_value,
            display_label_value,
            status_value,
            status_value,
            gate0_status_value,
            gate0_status_value,
            copy_status_value,
            copy_status_value,
            analysis_status_value,
            analysis_status_value,
            classification_value,
            classification_value,
            notes_value,
            notes_value,
            first_seen_value,
            first_seen_value,
            last_seen_value,
            last_seen_value,
            normalized,
        ),
    )


def upsert_wallet_membership(
    conn: sqlite3.Connection,
    *,
    wallet: str,
    scope: str,
    status: str = "active",
    label: str = "",
    reason: str = "",
    source: str = "",
    metadata: Any = None,
) -> None:
    normalized = _normalize_wallet(wallet)
    scope_key = str(scope or "").strip().lower()
    if not normalized or not scope_key:
        return
    upsert_wallet_registry(
        conn,
        wallet=normalized,
        display_label=label,
        copy_status=_copy_status_from_followed_status(status) if scope_key == "copy_execution" else None,
        gate0_status="failed" if str(status or "").strip().lower() == "gate0_failed" else "unknown",  # I3: "unknown" allows re-evaluation; None preserves stale "failed"
    )
    conn.execute(
        """
        INSERT INTO wallet_memberships (
            wallet, scope, status, label, reason, source, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(wallet, scope) DO UPDATE SET
            status = excluded.status,
            label = CASE
                WHEN excluded.label <> '' THEN excluded.label
                ELSE wallet_memberships.label
            END,
            reason = CASE
                WHEN excluded.reason <> '' THEN excluded.reason
                ELSE wallet_memberships.reason
            END,
            source = CASE
                WHEN excluded.source <> '' THEN excluded.source
                ELSE wallet_memberships.source
            END,
            metadata_json = CASE
                WHEN excluded.metadata_json <> '{}' THEN excluded.metadata_json
                ELSE wallet_memberships.metadata_json
            END,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            normalized,
            scope_key,
            str(status or "active").strip() or "active",
            str(label or "").strip(),
            str(reason or "").strip(),
            str(source or "").strip(),
            _json_text(metadata),
        ),
    )


def deactivate_wallet_membership(
    conn: sqlite3.Connection,
    *,
    wallet: str,
    scope: str,
    status: str = "inactive",
    reason: str = "",
) -> None:
    normalized = _normalize_wallet(wallet)
    scope_key = str(scope or "").strip().lower()
    if not normalized or not scope_key:
        return
    conn.execute(
        """
        UPDATE wallet_memberships
        SET status = ?, reason = CASE WHEN ? <> '' THEN ? ELSE reason END,
            updated_at = CURRENT_TIMESTAMP
        WHERE wallet = ? AND scope = ?
        """,
        (
            str(status or "inactive").strip() or "inactive",
            str(reason or "").strip(),
            str(reason or "").strip(),
            normalized,
            scope_key,
        ),
    )
    if scope_key == "copy_execution":
        upsert_wallet_registry(
            conn,
            wallet=normalized,
            copy_status=_copy_status_from_followed_status(status),
            gate0_status="failed" if str(status or "").strip().lower() == "gate0_failed" else None,
        )


def upsert_wallet_tag(
    conn: sqlite3.Connection,
    *,
    wallet: str,
    tag: str,
    tag_type: str = "general",
    source: str = "system",
    confidence: float | None = None,
    active: bool = True,
) -> None:
    normalized = _normalize_wallet(wallet)
    tag_value = str(tag or "").strip().lower()
    tag_type_value = str(tag_type or "general").strip().lower()
    source_value = str(source or "system").strip().lower()
    if not normalized or not tag_value or not tag_type_value:
        return
    upsert_wallet_registry(conn, wallet=normalized)
    conn.execute(
        """
        INSERT INTO wallet_tags (
            wallet, tag, tag_type, source, confidence, active
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(wallet, tag, tag_type, source) DO UPDATE SET
            confidence = excluded.confidence,
            active = excluded.active,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            normalized,
            tag_value,
            tag_type_value,
            source_value,
            float(confidence) if confidence is not None else None,
            1 if active else 0,
        ),
    )


def deactivate_wallet_tag(
    conn: sqlite3.Connection,
    *,
    wallet: str,
    tag: str,
    tag_type: str = "general",
    source: str = "system",
) -> None:
    normalized = _normalize_wallet(wallet)
    if not normalized:
        return
    conn.execute(
        """
        UPDATE wallet_tags
        SET active = 0, updated_at = CURRENT_TIMESTAMP
        WHERE wallet = ? AND tag = ? AND tag_type = ? AND source = ?
        """,
        (
            normalized,
            str(tag or "").strip().lower(),
            str(tag_type or "general").strip().lower(),
            str(source or "system").strip().lower(),
        ),
    )


def replace_wallet_tags(
    conn: sqlite3.Connection,
    *,
    wallet: str,
    tag_type: str,
    source: str = "system",
    tags: Sequence[str] | None = None,
    confidence: float | None = None,
) -> None:
    normalized = _normalize_wallet(wallet)
    tag_type_value = str(tag_type or "").strip().lower()
    source_value = str(source or "system").strip().lower()
    if not normalized or not tag_type_value:
        return
    conn.execute(
        """
        UPDATE wallet_tags
        SET active = 0, updated_at = CURRENT_TIMESTAMP
        WHERE wallet = ? AND tag_type = ? AND source = ?
        """,
        (normalized, tag_type_value, source_value),
    )
    seen: set[str] = set()
    for raw_tag in tags or ():
        tag_value = str(raw_tag or "").strip().lower()
        if not tag_value or tag_value in seen:
            continue
        seen.add(tag_value)
        upsert_wallet_tag(
            conn,
            wallet=normalized,
            tag=tag_value,
            tag_type=tag_type_value,
            source=source_value,
            confidence=confidence,
            active=True,
        )


def ensure_wallet_directory_schema(conn: sqlite3.Connection) -> None:
    """Create/backfill the canonical wallet registry, memberships, and tags layer."""
    with with_db_init_lock(conn):
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS wallet_registry (
                wallet TEXT PRIMARY KEY,
                display_label TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                gate0_status TEXT NOT NULL DEFAULT 'unknown',
                copy_status TEXT NOT NULL DEFAULT 'not_followed',
                analysis_status TEXT NOT NULL DEFAULT 'unknown',
                classification TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                first_seen_ts INTEGER NOT NULL DEFAULT 0,
                last_seen_ts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_wallet_registry_status
                ON wallet_registry(status, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_wallet_registry_copy_status
                ON wallet_registry(copy_status, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_wallet_registry_analysis_status
                ON wallet_registry(analysis_status, updated_at DESC);

            CREATE TABLE IF NOT EXISTS wallet_memberships (
                wallet TEXT NOT NULL,
                scope TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                label TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (wallet, scope)
            );

            CREATE INDEX IF NOT EXISTS idx_wallet_memberships_scope_status
                ON wallet_memberships(scope, status, updated_at DESC);

            CREATE TABLE IF NOT EXISTS wallet_tags (
                wallet TEXT NOT NULL,
                tag TEXT NOT NULL,
                tag_type TEXT NOT NULL DEFAULT 'general',
                source TEXT NOT NULL DEFAULT 'system',
                confidence REAL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (wallet, tag, tag_type, source)
            );

            CREATE INDEX IF NOT EXISTS idx_wallet_tags_type_active
                ON wallet_tags(tag_type, active, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_wallet_tags_wallet_type
                ON wallet_tags(wallet, tag_type, active);
            """
        )

        if _table_exists(conn, "watchlist_wallets"):
            watchlist_rows = conn.execute(
                "SELECT wallet, label, added_ts, last_synced_ts FROM watchlist_wallets"
            ).fetchall()
        else:
            watchlist_rows = []
        for row in watchlist_rows:
            wallet = str(row["wallet"] or "")
            if not wallet:
                continue
            label = str(row["label"] or "")
            upsert_wallet_registry(
                conn,
                wallet=wallet,
                display_label=label,
                first_seen_ts=int(row["added_ts"] or 0),
                last_seen_ts=int(row["last_synced_ts"] or row["added_ts"] or 0),
            )
            upsert_wallet_membership(
                conn,
                wallet=wallet,
                scope="watchlist",
                status="active",
                label=label,
                source="legacy_watchlist",
                metadata={
                    "added_ts": int(row["added_ts"] or 0),
                    "last_synced_ts": int(row["last_synced_ts"] or 0),
                },
            )
            upsert_wallet_tag(
                conn,
                wallet=wallet,
                tag="watchlist",
                tag_type="scope",
                source="legacy_watchlist",
            )

        if _table_exists(conn, "followed_wallets"):
            followed_rows = conn.execute(
                """
                SELECT wallet, label, added_reason, status, strategy_json, notes,
                       total_positions, total_pnl_usdc, last_copied_at
                FROM followed_wallets
                """
            ).fetchall()
        else:
            followed_rows = []
        for row in followed_rows:
            wallet = str(row["wallet"] or "")
            if not wallet:
                continue
            label = str(row["label"] or "")
            status_value = str(row["status"] or "active").strip() or "active"
            upsert_wallet_registry(
                conn,
                wallet=wallet,
                display_label=label,
                gate0_status="failed" if status_value == "gate0_failed" else None,
                copy_status=_copy_status_from_followed_status(status_value),
                notes=str(row["notes"] or ""),
            )
            upsert_wallet_membership(
                conn,
                wallet=wallet,
                scope="copy_execution",
                status=status_value,
                label=label,
                reason=str(row["added_reason"] or ""),
                source="legacy_followed_wallets",
                metadata={
                    "strategy_json": str(row["strategy_json"] or ""),
                    "notes": str(row["notes"] or ""),
                    "total_positions": int(row["total_positions"] or 0),
                    "total_pnl_usdc": float(row["total_pnl_usdc"] or 0.0),
                    "last_copied_at": str(row["last_copied_at"] or ""),
                },
            )
            upsert_wallet_tag(
                conn,
                wallet=wallet,
                tag="copy_execution",
                tag_type="scope",
                source="legacy_followed_wallets",
                active=status_value in {"active", "paused"},
            )

        if _table_exists(conn, "wallet_profiles"):
            profile_rows = conn.execute(
                "SELECT wallet, username, pseudonym FROM wallet_profiles"
            ).fetchall()
        else:
            profile_rows = []
        for row in profile_rows:
            wallet = str(row["wallet"] or "")
            if not wallet:
                continue
            display_label = (
                str(row["username"] or "").strip()
                or str(row["pseudonym"] or "").strip()
            )
            upsert_wallet_registry(
                conn,
                wallet=wallet,
                display_label=display_label,
            )

        if _table_exists(conn, "user_analysis"):
            ua_cols = {r[1] for r in conn.execute("PRAGMA table_info(user_analysis)").fetchall()}
            has_reason = "reason" in ua_cols
            has_edge_tier = "wallet_edge_tier" in ua_cols
            has_lb_username = "lb_username" in ua_cols
            select_cols = ["wallet", "analyzed_ts", "classification"]
            if has_reason:
                select_cols.append("reason")
            if has_edge_tier:
                select_cols.append("wallet_edge_tier")
            if has_lb_username:
                select_cols.append("lb_username")
            analysis_rows = conn.execute(
                f"SELECT {', '.join(select_cols)} FROM user_analysis"
            ).fetchall()
        else:
            analysis_rows = []
            has_reason = has_edge_tier = has_lb_username = False
        for row in analysis_rows:
            wallet = str(row["wallet"] or "")
            if not wallet:
                continue
            classification = str(row["classification"] or "").strip()
            upsert_wallet_registry(
                conn,
                wallet=wallet,
                display_label=str(row["lb_username"] or "") if has_lb_username else "",
                status="blocked" if classification == "blocked" else None,
                analysis_status="analyzed" if int(row["analyzed_ts"] or 0) > 0 else "pending",
                classification=classification,
                notes=str(row["reason"] or "") if has_reason else "",
                last_seen_ts=int(row["analyzed_ts"] or 0),
            )
            replace_wallet_tags(
                conn,
                wallet=wallet,
                tag_type="classification",
                source="user_analysis",
                tags=[classification] if classification else [],
            )
            replace_wallet_tags(
                conn,
                wallet=wallet,
                tag_type="wallet_edge_tier",
                source="user_analysis",
                tags=[str(row["wallet_edge_tier"] or "").strip()] if str(row["wallet_edge_tier"] or "").strip() else [],
            )

        if _table_exists(conn, "ignored_wallets"):
            ignored_rows = conn.execute("SELECT wallet FROM ignored_wallets").fetchall()
        else:
            ignored_rows = []
        for row in ignored_rows:
            wallet = str(row["wallet"] or "")
            if not wallet:
                continue
            upsert_wallet_registry(conn, wallet=wallet, status="ignored")
            upsert_wallet_tag(
                conn,
                wallet=wallet,
                tag="ignored",
                tag_type="state",
                source="ignored_wallets",
            )

        if _table_exists(conn, "blocked_wallets"):
            blocked_rows = conn.execute("SELECT wallet FROM blocked_wallets").fetchall()
        else:
            blocked_rows = []
        for row in blocked_rows:
            wallet = str(row["wallet"] or "")
            if not wallet:
                continue
            upsert_wallet_registry(conn, wallet=wallet, status="blocked")
            upsert_wallet_tag(
                conn,
                wallet=wallet,
                tag="blocked",
                tag_type="state",
                source="blocked_wallets",
            )

        conn.commit()



# ── root-shim helpers (lazy-imported only where circular imports remain) ──

def _lazy_is_trade_deleted(conn, tx_hash):
    from db import is_trade_deleted
    return is_trade_deleted(conn, tx_hash)


def _lazy_connection_scope(db_path=None):
    from db import connection_scope
    return connection_scope(db_path)


def _membership_scope_rows(
    conn: sqlite3.Connection,
    *,
    scope: str,
    statuses: Sequence[str] | None = None,
    extra_selects: str = "",
    order_by: str = "updated_at DESC",
) -> List[sqlite3.Row]:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='wallet_memberships' LIMIT 1"
    ).fetchone()
    if table is None:
        return []
    scope_key = str(scope or "").strip().lower()
    if not scope_key:
        return []
    where = ["scope = ?"]
    params: List[Any] = [scope_key]
    if statuses:
        cleaned = [str(status or "").strip().lower() for status in statuses if str(status or "").strip()]
        if cleaned:
            where.append(f"status IN ({','.join('?' for _ in cleaned)})")
            params.extend(cleaned)
    query = f"""
        SELECT wallet, label, status, reason, source, metadata_json, created_at, updated_at
               {extra_selects}
        FROM wallet_memberships
        WHERE {' AND '.join(where)}
        ORDER BY {order_by}
    """
    return conn.execute(query, params).fetchall()


def _lazy_upsert_wallet_registry(conn, **kwargs):
    return upsert_wallet_registry(conn, **kwargs)


def _lazy_upsert_wallet_membership(conn, **kwargs):
    return upsert_wallet_membership(conn, **kwargs)


def _lazy_deactivate_wallet_membership(conn, **kwargs):
    return deactivate_wallet_membership(conn, **kwargs)


def _lazy_upsert_wallet_tag(conn, **kwargs):
    return upsert_wallet_tag(conn, **kwargs)


def _lazy_deactivate_wallet_tag(conn, **kwargs):
    return deactivate_wallet_tag(conn, **kwargs)

def watchlist_wallets(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    membership_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='wallet_memberships' LIMIT 1"
    ).fetchone()
    if membership_table is not None:
        return conn.execute(
            """
            WITH canonical AS (
                SELECT wallet,
                       label,
                       COALESCE(CAST(json_extract(metadata_json, '$.added_ts') AS INTEGER), 0) AS added_ts,
                       COALESCE(CAST(json_extract(metadata_json, '$.last_synced_ts') AS INTEGER), 0) AS last_synced_ts,
                       updated_at
                FROM wallet_memberships
                WHERE scope = 'watchlist' AND status = 'active'
            ),
            fallback AS (
                SELECT ww.wallet,
                       ww.label,
                       ww.added_ts,
                       ww.last_synced_ts,
                       ww.updated_at
                FROM watchlist_wallets ww
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM wallet_memberships wm
                    WHERE wm.wallet = ww.wallet AND wm.scope = 'watchlist'
                )
            )
            SELECT wallet, label, added_ts, last_synced_ts, updated_at
            FROM canonical
            UNION ALL
            SELECT wallet, label, added_ts, last_synced_ts, updated_at
            FROM fallback
            ORDER BY added_ts DESC, updated_at DESC
            """
        ).fetchall()
    return conn.execute(
        """
        SELECT wallet, label, added_ts, last_synced_ts, updated_at
        FROM watchlist_wallets
        ORDER BY added_ts DESC
        """
    ).fetchall()


def upsert_watchlist_wallet(conn: sqlite3.Connection, wallet: str, label: str = "") -> None:
    wallet = _normalize_wallet(wallet)
    if not wallet:
        return
    existing = conn.execute(
        "SELECT added_ts, last_synced_ts FROM watchlist_wallets WHERE wallet = ?",
        (wallet,),
    ).fetchone()
    added_ts = int(existing["added_ts"] or 0) if existing else int(time.time())
    last_synced_ts = int(existing["last_synced_ts"] or 0) if existing else 0
    conn.execute(
        """
        INSERT INTO watchlist_wallets (wallet, label, added_ts, last_synced_ts)
        VALUES (?, ?, ?, 0)
        ON CONFLICT(wallet) DO UPDATE SET
            label = CASE
                WHEN excluded.label <> '' THEN excluded.label
                ELSE watchlist_wallets.label
            END,
            updated_at = CURRENT_TIMESTAMP
        """,
        (wallet, (label or "").strip(), int(time.time())),
    )
    _lazy_upsert_wallet_registry(
        conn,
        wallet=wallet,
        display_label=(label or "").strip(),
        first_seen_ts=added_ts,
        last_seen_ts=max(added_ts, last_synced_ts),
    )
    _lazy_upsert_wallet_membership(
        conn,
        wallet=wallet,
        scope="watchlist",
        status="active",
        label=(label or "").strip(),
        source="watchlist_wallets",
        metadata={
            "added_ts": added_ts,
            "last_synced_ts": last_synced_ts,
        },
    )
    _lazy_upsert_wallet_tag(
        conn,
        wallet=wallet,
        tag="watchlist",
        tag_type="scope",
        source="watchlist_wallets",
    )
    conn.commit()


def mark_watchlist_wallet_synced(conn: sqlite3.Connection, wallet: str, synced_ts: int | None = None) -> None:
    wallet = _normalize_wallet(wallet)
    if not wallet:
        return
    synced_value = int(synced_ts or time.time())
    conn.execute(
        """
        UPDATE watchlist_wallets
        SET last_synced_ts = ?, updated_at = CURRENT_TIMESTAMP
        WHERE wallet = ?
        """,
        (synced_value, wallet),
    )
    row = conn.execute(
        "SELECT label, added_ts, last_synced_ts FROM watchlist_wallets WHERE wallet = ?",
        (wallet,),
    ).fetchone()
    if row:
        _lazy_upsert_wallet_registry(
            conn,
            wallet=wallet,
            display_label=str(row["label"] or ""),
            first_seen_ts=int(row["added_ts"] or 0),
            last_seen_ts=int(row["last_synced_ts"] or synced_value),
        )
        _lazy_upsert_wallet_membership(
            conn,
            wallet=wallet,
            scope="watchlist",
            status="active",
            label=str(row["label"] or ""),
            source="watchlist_wallets",
            metadata={
                "added_ts": int(row["added_ts"] or 0),
                "last_synced_ts": int(row["last_synced_ts"] or synced_value),
            },
        )
    conn.commit()


def remove_watchlist_wallet(conn: sqlite3.Connection, wallet: str) -> None:
    wallet = _normalize_wallet(wallet)
    if not wallet:
        return
    conn.execute("DELETE FROM watchlist_trades WHERE wallet = ?", (wallet,))
    conn.execute("DELETE FROM watchlist_wallets WHERE wallet = ?", (wallet,))
    _lazy_deactivate_wallet_membership(
        conn,
        wallet=wallet,
        scope="watchlist",
        status="removed",
        reason="removed from watchlist",
    )
    _lazy_deactivate_wallet_tag(
        conn,
        wallet=wallet,
        tag="watchlist",
        tag_type="scope",
        source="watchlist_wallets",
    )
    conn.commit()


def upsert_watchlist_trade(conn: sqlite3.Connection, wallet: str, trade: Dict[str, Any]) -> None:
    wallet = _normalize_wallet(wallet)
    if not wallet:
        return

    tx_hash = _normalize_tx_hash(trade.get("transactionHash"))
    if not tx_hash:
        tx_hash = (
            f"{wallet}:{trade.get('conditionId') or ''}:{trade.get('timestamp') or ''}:"
            f"{trade.get('price') or ''}:{trade.get('size') or ''}:{trade.get('outcome') or ''}"
        )
    tx_hash = _normalize_tx_hash(tx_hash)
    if not tx_hash or _lazy_is_trade_deleted(conn, tx_hash):
        return

    price = float(trade.get("price") or 0.0)
    size = float(trade.get("size") or 0.0)
    notional = price * size
    timestamp = int(trade.get("timestamp") or 0)
    condition_id = str(trade.get("conditionId") or trade.get("market") or "").strip()

    conn.execute(
        """
        INSERT INTO watchlist_trades (
            wallet,
            transaction_hash,
            condition_id,
            side,
            price,
            size,
            notional,
            timestamp,
            title,
            outcome,
            event_slug,
            source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'watchlist_sync')
        ON CONFLICT(wallet, transaction_hash) DO UPDATE SET
            condition_id = CASE
                WHEN excluded.condition_id IS NOT NULL AND excluded.condition_id <> ''
                THEN excluded.condition_id
                ELSE watchlist_trades.condition_id
            END,
            side = excluded.side,
            price = excluded.price,
            size = excluded.size,
            notional = excluded.notional,
            timestamp = excluded.timestamp,
            title = excluded.title,
            outcome = excluded.outcome,
            event_slug = excluded.event_slug,
            source = excluded.source,
            inserted_at = CURRENT_TIMESTAMP
        """,
        (
            wallet,
            tx_hash,
            condition_id,
            trade.get("side"),
            price,
            size,
            notional,
            timestamp,
            trade.get("title"),
            trade.get("outcome"),
            trade.get("eventSlug"),
        ),
    )


def upsert_watchlist_trades(conn: sqlite3.Connection, wallet: str, trades: Iterable[Dict[str, Any]]) -> None:
    for trade in trades:
        upsert_watchlist_trade(conn, wallet, trade)
    conn.commit()


def recent_watchlist_trades(
    conn: sqlite3.Connection,
    limit: int = 300,
    wallet: str | None = None,
) -> List[sqlite3.Row]:
    if wallet:
        return conn.execute(
            """
            SELECT
                t.*,
                w.label AS wallet_label
            FROM watchlist_trades t
            LEFT JOIN watchlist_wallets w ON w.wallet = t.wallet
            WHERE t.wallet = ?
            ORDER BY t.timestamp DESC
            LIMIT ?
            """,
            (_normalize_wallet(wallet), limit),
        ).fetchall()

    return conn.execute(
        """
        SELECT
            t.*,
            w.label AS wallet_label
        FROM watchlist_trades t
        LEFT JOIN watchlist_wallets w ON w.wallet = t.wallet
        ORDER BY t.timestamp DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def upsert_followed_trade(
    conn: sqlite3.Connection,
    *,
    wallet: str,
    transaction_hash: str,
    condition_id: str,
    side: str = "",
    outcome: str = "",
    title: str = "",
    event_slug: str = "",
    entry_price: float = 0.0,
    entry_size: float = 0.0,
    entry_ts: int = 0,
) -> None:
    wallet = _normalize_wallet(wallet)
    transaction_hash = _normalize_tx_hash(transaction_hash)
    condition_id = str(condition_id or "").strip()
    if not wallet or not transaction_hash or not condition_id:
        return
    if _lazy_is_trade_deleted(conn, transaction_hash):
        return

    now_ts = int(time.time())
    if not entry_ts:
        entry_ts = now_ts
    entry_notional = float(entry_price or 0.0) * float(entry_size or 0.0)

    conn.execute(
        """
        INSERT INTO followed_trades (
            wallet,
            transaction_hash,
            condition_id,
            side,
            outcome,
            title,
            event_slug,
            entry_price,
            entry_size,
            entry_notional,
            entry_ts,
            status,
            status_label,
            status_reason,
            is_closed,
            added_ts,
            last_synced_ts
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 'Active', '', 0, ?, 0)
        ON CONFLICT(wallet, transaction_hash) DO UPDATE SET
            condition_id = excluded.condition_id,
            side = excluded.side,
            outcome = excluded.outcome,
            title = CASE
                WHEN excluded.title <> '' THEN excluded.title
                ELSE followed_trades.title
            END,
            event_slug = CASE
                WHEN excluded.event_slug <> '' THEN excluded.event_slug
                ELSE followed_trades.event_slug
            END,
            entry_price = excluded.entry_price,
            entry_size = excluded.entry_size,
            entry_notional = excluded.entry_notional,
            entry_ts = excluded.entry_ts,
            status = CASE
                WHEN followed_trades.last_synced_ts <= 0 THEN 'active'
                ELSE followed_trades.status
            END,
            status_label = CASE
                WHEN followed_trades.last_synced_ts <= 0 THEN 'Active'
                ELSE followed_trades.status_label
            END,
            status_reason = CASE
                WHEN followed_trades.last_synced_ts <= 0 THEN ''
                ELSE followed_trades.status_reason
            END,
            is_closed = CASE
                WHEN followed_trades.last_synced_ts <= 0 THEN 0
                ELSE followed_trades.is_closed
            END,
            added_ts = CASE
                WHEN followed_trades.added_ts > 0 THEN followed_trades.added_ts
                ELSE excluded.added_ts
            END,
            inserted_at = CURRENT_TIMESTAMP
        """,
        (
            wallet,
            transaction_hash,
            condition_id,
            str(side or "").upper(),
            str(outcome or ""),
            str(title or ""),
            str(event_slug or ""),
            float(entry_price or 0.0),
            float(entry_size or 0.0),
            entry_notional,
            int(entry_ts or now_ts),
            now_ts,
        ),
    )
    conn.commit()


def update_followed_trade_status(
    conn: sqlite3.Connection,
    *,
    wallet: str,
    transaction_hash: str,
    status: str,
    status_label: str,
    status_reason: str = "",
    is_closed: bool = False,
    is_win: bool | None = None,
    exited_early: bool | None = None,
    live_price: float | None = None,
    exit_price: float | None = None,
    current_value: float = 0.0,
    realized_pnl: float = 0.0,
    close_ts: int | None = None,
    event_end_ts: int | None = None,
    last_synced_ts: int | None = None,
) -> None:
    wallet = _normalize_wallet(wallet)
    transaction_hash = _normalize_tx_hash(transaction_hash)
    if not wallet or not transaction_hash:
        return

    conn.execute(
        """
        UPDATE followed_trades
        SET
            status = ?,
            status_label = ?,
            status_reason = ?,
            is_closed = ?,
            is_win = ?,
            exited_early = ?,
            live_price = ?,
            exit_price = ?,
            current_value = ?,
            realized_pnl = ?,
            close_ts = ?,
            event_end_ts = ?,
            last_synced_ts = ?,
            inserted_at = CURRENT_TIMESTAMP
        WHERE wallet = ? AND transaction_hash = ?
        """,
        (
            str(status or "active"),
            str(status_label or "Active"),
            str(status_reason or ""),
            1 if is_closed else 0,
            None if is_win is None else (1 if is_win else 0),
            None if exited_early is None else (1 if exited_early else 0),
            None if live_price is None else float(live_price),
            None if exit_price is None else float(exit_price),
            float(current_value or 0.0),
            float(realized_pnl or 0.0),
            None if close_ts is None else int(close_ts),
            None if event_end_ts is None else int(event_end_ts),
            int(last_synced_ts or time.time()),
            wallet,
            transaction_hash,
        ),
    )
    conn.commit()


def followed_trades(
    conn: sqlite3.Connection,
    limit: int = 500,
    wallet: str | None = None,
) -> List[sqlite3.Row]:
    if wallet:
        return conn.execute(
            """
            SELECT *
            FROM followed_trades
            WHERE wallet = ?
            ORDER BY added_ts DESC, entry_ts DESC
            LIMIT ?
            """,
            (_normalize_wallet(wallet), limit),
        ).fetchall()

    return conn.execute(
        """
        SELECT *
        FROM followed_trades
        ORDER BY added_ts DESC, entry_ts DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def remove_followed_trade(conn: sqlite3.Connection, wallet: str, transaction_hash: str) -> None:
    wallet = _normalize_wallet(wallet)
    transaction_hash = _normalize_tx_hash(transaction_hash)
    if not wallet or not transaction_hash:
        return
    conn.execute(
        "DELETE FROM followed_trades WHERE wallet = ? AND transaction_hash = ?",
        (wallet, transaction_hash),
    )
    conn.commit()


def upsert_followed_position(
    conn: sqlite3.Connection,
    *,
    wallet: str,
    condition_id: str,
    outcome: str,
    title: str = "",
    event_slug: str = "",
    label: str = "",
    added_source: str = "manual",
    entry_price: float | None = None,
    current_price: float | None = None,
    entry_notional: float | None = None,
    current_amount: float | None = None,
    current_size: float | None = None,
    added_ts: int | None = None,
    last_synced_ts: int | None = None,
) -> None:
    wallet = _normalize_wallet(wallet)
    condition_id = _normalize_condition_id(condition_id)
    outcome = _normalize_outcome(outcome)
    if not wallet or not condition_id:
        return

    source = str(added_source or "").strip().lower()
    if source not in {"manual", "auto_relevant"}:
        source = "manual"
    now_ts = int(time.time())
    added_ts_value = int(added_ts or now_ts)
    last_synced_value = int(last_synced_ts or now_ts)

    conn.execute(
        """
        INSERT INTO followed_positions (
            wallet,
            condition_id,
            outcome,
            title,
            event_slug,
            label,
            added_source,
            entry_price,
            current_price,
            entry_notional,
            current_amount,
            current_size,
            added_ts,
            last_synced_ts
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(wallet, condition_id, outcome) DO UPDATE SET
            title = CASE
                WHEN excluded.title <> '' THEN excluded.title
                ELSE followed_positions.title
            END,
            event_slug = CASE
                WHEN excluded.event_slug <> '' THEN excluded.event_slug
                ELSE followed_positions.event_slug
            END,
            label = CASE
                WHEN excluded.label <> '' THEN excluded.label
                ELSE followed_positions.label
            END,
            added_source = CASE
                WHEN followed_positions.added_source = 'manual' OR excluded.added_source = 'manual'
                THEN 'manual'
                ELSE 'auto_relevant'
            END,
            added_ts = CASE
                WHEN followed_positions.added_ts > 0 THEN followed_positions.added_ts
                ELSE excluded.added_ts
            END,
            entry_price = CASE
                WHEN excluded.entry_price > 0 THEN excluded.entry_price
                ELSE followed_positions.entry_price
            END,
            current_price = CASE
                WHEN excluded.current_price > 0 THEN excluded.current_price
                ELSE followed_positions.current_price
            END,
            entry_notional = CASE
                WHEN excluded.entry_notional > 0 THEN excluded.entry_notional
                ELSE followed_positions.entry_notional
            END,
            current_amount = CASE
                WHEN excluded.current_amount > 0 THEN excluded.current_amount
                ELSE followed_positions.current_amount
            END,
            current_size = CASE
                WHEN excluded.current_size > 0 THEN excluded.current_size
                ELSE followed_positions.current_size
            END,
            last_synced_ts = CASE
                WHEN excluded.last_synced_ts > COALESCE(followed_positions.last_synced_ts, 0)
                THEN excluded.last_synced_ts
                ELSE followed_positions.last_synced_ts
            END,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            wallet,
            condition_id,
            outcome,
            str(title or ""),
            str(event_slug or ""),
            str(label or ""),
            source,
            float(entry_price or 0.0),
            float(current_price or 0.0),
            float(entry_notional or 0.0),
            float(current_amount or 0.0),
            float(current_size or 0.0),
            added_ts_value,
            last_synced_value,
        ),
    )
    conn.commit()


def remove_followed_position(
    conn: sqlite3.Connection,
    *,
    wallet: str,
    condition_id: str,
    outcome: str,
) -> None:
    wallet = _normalize_wallet(wallet)
    condition_id = _normalize_condition_id(condition_id)
    outcome = _normalize_outcome(outcome)
    if not wallet or not condition_id:
        return
    conn.execute(
        "DELETE FROM followed_positions WHERE wallet = ? AND condition_id = ? AND outcome = ?",
        (wallet, condition_id, outcome),
    )
    conn.commit()


def prune_followed_positions_for_wallet(
    conn: sqlite3.Connection,
    *,
    wallet: str,
    open_positions: Iterable[Dict[str, Any]],
) -> None:
    wallet = _normalize_wallet(wallet)
    if not wallet:
        return

    pair_keys: List[str] = []
    seen: set[str] = set()
    for row in open_positions:
        condition_id = _normalize_condition_id(row.get("condition_id"))
        outcome = _normalize_outcome(row.get("outcome"))
        if not condition_id:
            continue
        key = f"{condition_id}::{outcome}"
        if key in seen:
            continue
        seen.add(key)
        pair_keys.append(key)

    if pair_keys:
        placeholders = ",".join("?" for _ in pair_keys)
        conn.execute(
            f"""
            DELETE FROM followed_positions
            WHERE wallet = ?
              AND (condition_id || '::' || outcome) NOT IN ({placeholders})
            """,
            (wallet, *pair_keys),
        )
    else:
        conn.execute(
            """
            DELETE FROM followed_positions
            WHERE wallet = ?
            """,
            (wallet,),
        )
    conn.commit()


def followed_positions(
    conn: sqlite3.Connection,
    *,
    wallet: str | None = None,
    include_auto: bool = True,
    limit: int = 1000,
) -> List[sqlite3.Row]:
    where: List[str] = ["1=1"]
    params: List[Any] = []
    if wallet:
        where.append("wallet = ?")
        params.append(_normalize_wallet(wallet))
    if not include_auto:
        where.append("added_source = 'manual'")
    params.append(int(limit))
    return conn.execute(
        f"""
        SELECT *
        FROM followed_positions
        WHERE {" AND ".join(where)}
        ORDER BY
            CASE
                WHEN added_source = 'manual' THEN 0
                ELSE 1
            END ASC,
            last_synced_ts DESC,
            added_ts DESC
        LIMIT ?
        """,
        params,
    ).fetchall()


# ---------------------------------------------------------------------------
# WS -> real-hash reconciliation helpers
# ---------------------------------------------------------------------------

def _filter_price_size_matches(candidates, price, size, usdc_size):
    """Return candidates whose price/size match within 6-decimal tolerance."""
    matched = []
    for c in candidates:
        if round(float(c["price"]), 6) != round(price, 6):
            continue
        if round(float(c["size"]), 6) != round(size, 6):
            continue
        c_usdc = float(c["usdc_size"] or 0)
        if c_usdc > 0 and usdc_size > 0:
            if round(c_usdc, 4) != round(usdc_size, 4):
                continue
        matched.append(c)
    return matched


def _cleanup_synthetic_duplicates(conn, wallet_key, condition_id, outcome, side,
                                   activity_type, activity_ts, price, size, usdc_size):
    """Delete synthetic rows that match the canonical real row's fingerprint.

    Only deletes when exactly one synthetic candidate matches (unambiguous).
    """
    candidates = conn.execute("""
        SELECT activity_key, activity_ts, price, size, usdc_size
        FROM followed_wallet_activity
        WHERE wallet = ? AND condition_id = ? AND UPPER(outcome) = UPPER(?)
          AND activity_type = ? AND side = ?
          AND activity_ts BETWEEN ? AND ?
          AND transaction_hash LIKE 'ws:%'
    """, (wallet_key, condition_id, outcome, activity_type, side,
          activity_ts - 2, activity_ts + 2)).fetchall()

    matched = _filter_price_size_matches(candidates, price, size, usdc_size)

    if len(matched) == 1:
        conn.execute("DELETE FROM followed_wallet_activity WHERE activity_key = ?",
                      (matched[0]["activity_key"],))
    elif len(matched) > 1:
        logger.warning("ambiguous_cleanup_synthetic_duplicates: %d candidates for wallet=%s cond=%s",
                        len(matched), wallet_key[:16], condition_id[:16])


def _reconcile_real_hash_row(conn, wallet_key, canonical_activity_key, row_tuple) -> bool:
    """Attempt to reconcile a real-hash row with an existing synthetic WS row.

    Returns True if reconciled (caller skips insert), False to fall through
    to normal upsert.
    """
    tx_hash = row_tuple[2]
    condition_id = row_tuple[3]
    activity_type = row_tuple[4]
    side = row_tuple[5]
    outcome = row_tuple[6]
    price = row_tuple[10]
    size = row_tuple[11]
    usdc_size = row_tuple[12]
    activity_ts = row_tuple[13]

    # Step 2: Check if canonical already exists
    existing = conn.execute(
        "SELECT activity_key FROM followed_wallet_activity WHERE activity_key = ?",
        (canonical_activity_key,)
    ).fetchone()

    if existing:
        # Canonical exists -- clean up synthetics, then let caller do normal upsert
        _cleanup_synthetic_duplicates(conn, wallet_key, condition_id, outcome, side,
                                       activity_type, activity_ts, price, size, usdc_size)
        return False  # caller handles upsert

    # Step 3: Search for synthetic candidate within +/-2s window
    candidates = conn.execute("""
        SELECT activity_key, activity_ts, price, size, usdc_size
        FROM followed_wallet_activity
        WHERE wallet = ? AND condition_id = ? AND UPPER(outcome) = UPPER(?)
          AND activity_type = ? AND side = ?
          AND activity_ts BETWEEN ? AND ?
          AND transaction_hash LIKE 'ws:%'
    """, (wallet_key, condition_id, outcome, activity_type, side,
          activity_ts - 2, activity_ts + 2)).fetchall()

    matched = _filter_price_size_matches(candidates, price, size, usdc_size)

    if len(matched) == 1:
        # Step 4: Update in place -- promote synthetic to canonical
        conn.execute("""
            UPDATE followed_wallet_activity SET
                activity_key = ?, transaction_hash = ?,
                title = ?, slug = ?, event_slug = ?,
                price = ?, size = ?, usdc_size = ?,
                activity_ts = ?, source_endpoint = ?, raw_json = ?,
                inserted_at = CURRENT_TIMESTAMP
            WHERE activity_key = ?
        """, (canonical_activity_key, tx_hash,
              row_tuple[7], row_tuple[8], row_tuple[9],  # title, slug, event_slug
              price, size, usdc_size, activity_ts,
              row_tuple[14], row_tuple[15],  # source_endpoint, raw_json
              matched[0]["activity_key"]))
        return True

    if len(matched) > 1:
        logger.warning("ambiguous_followed_wallet_activity_reconcile: %d candidates for wallet=%s cond=%s",
                        len(matched), wallet_key[:16], condition_id[:16])

    return False  # no match or ambiguous -- fall through to insert


def upsert_followed_wallet_activity(
    conn: sqlite3.Connection,
    *,
    wallet: str,
    activities: Iterable[Dict[str, Any]],
    source_endpoint: str = "activity",
) -> int:
    wallet_key = _normalize_wallet(wallet)
    if not wallet_key:
        return 0

    _UPSERT_SQL = """
        INSERT INTO followed_wallet_activity (
            activity_key,
            wallet,
            transaction_hash,
            condition_id,
            activity_type,
            side,
            outcome,
            title,
            slug,
            event_slug,
            price,
            size,
            usdc_size,
            activity_ts,
            source_endpoint,
            raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(activity_key) DO UPDATE SET
            transaction_hash = excluded.transaction_hash,
            condition_id = excluded.condition_id,
            activity_type = excluded.activity_type,
            side = excluded.side,
            outcome = excluded.outcome,
            title = CASE
                WHEN excluded.title <> '' THEN excluded.title
                ELSE followed_wallet_activity.title
            END,
            slug = CASE
                WHEN excluded.slug <> '' THEN excluded.slug
                ELSE followed_wallet_activity.slug
            END,
            event_slug = CASE
                WHEN excluded.event_slug <> '' THEN excluded.event_slug
                ELSE followed_wallet_activity.event_slug
            END,
            price = excluded.price,
            size = excluded.size,
            usdc_size = excluded.usdc_size,
            activity_ts = excluded.activity_ts,
            source_endpoint = excluded.source_endpoint,
            raw_json = excluded.raw_json,
            inserted_at = CURRENT_TIMESTAMP
    """

    ws_payload: List[tuple[Any, ...]] = []
    real_rows: List[tuple[Any, ...]] = []

    for row in activities:
        if not isinstance(row, dict):
            continue
        transaction_hash = _normalize_tx_hash(
            row.get("transactionHash") or row.get("transaction_hash") or ""
        )
        condition_id = _normalize_condition_id(
            row.get("conditionId") or row.get("condition_id") or row.get("market") or ""
        )
        activity_type = str(row.get("type") or row.get("activity_type") or "").strip().upper()
        side = str(row.get("side") or "").strip().upper()
        outcome = str(row.get("outcome") or "").strip()
        title = str(row.get("title") or row.get("question") or "").strip()
        slug = str(row.get("slug") or "").strip()
        event_slug = str(row.get("eventSlug") or row.get("event_slug") or "").strip()
        price = float(row.get("price") or 0.0)
        size = float(row.get("size") or 0.0)
        usdc_size = float(row.get("usdcSize") or row.get("usdc_size") or 0.0)
        activity_ts = _coerce_history_ts(
            row.get("timestamp") or row.get("createdAt") or row.get("created_at")
        )
        activity_key = _followed_wallet_activity_key(
            wallet_key,
            transaction_hash,
            condition_id,
            outcome,
            side,
            activity_type,
            activity_ts,
            price,
            size,
        )
        row_tuple = (
            activity_key,
            wallet_key,
            transaction_hash,
            condition_id,
            activity_type,
            side,
            outcome,
            title,
            slug,
            event_slug,
            price,
            size,
            usdc_size,
            activity_ts,
            str(source_endpoint or "activity"),
            json.dumps(row, separators=(",", ":"), sort_keys=True),
        )

        if is_synthetic_ws_hash(transaction_hash):
            # Reject WS rows with invalid price/size
            if price <= 0 or size <= 0:
                continue
            ws_payload.append(row_tuple)
        else:
            real_rows.append(row_tuple)

    inserted = 0

    # Batch-insert WS synthetic rows (existing executemany path)
    if ws_payload:
        conn.executemany(_UPSERT_SQL, ws_payload)
        inserted += len(ws_payload)

    # Per-row reconciliation for real-hash rows
    for row_tuple in real_rows:
        activity_key = row_tuple[0]
        reconciled = _reconcile_real_hash_row(conn, wallet_key, activity_key, row_tuple)
        if not reconciled:
            conn.execute(_UPSERT_SQL, row_tuple)
        inserted += 1

    conn.commit()
    return inserted


def followed_wallet_activity(
    conn: sqlite3.Connection,
    *,
    wallet: str | None = None,
    limit: int = 500,
) -> List[sqlite3.Row]:
    if wallet:
        return conn.execute(
            """
            SELECT *
            FROM followed_wallet_activity
            WHERE wallet = ?
            ORDER BY activity_ts DESC, inserted_at DESC
            LIMIT ?
            """,
            (_normalize_wallet(wallet), int(limit)),
        ).fetchall()
    return conn.execute(
        """
        SELECT *
        FROM followed_wallet_activity
        ORDER BY activity_ts DESC, inserted_at DESC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()


def followed_wallet_research_scope_wallets(conn: sqlite3.Connection) -> List[str]:
    membership_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='wallet_memberships' LIMIT 1"
    ).fetchone()
    if membership_table is not None:
        rows = conn.execute(
            """
            WITH universe AS (
                SELECT wallet
                FROM wallet_memberships
                WHERE (scope = 'copy_execution' AND status = 'active')
                   OR (scope = 'watchlist' AND status = 'active')
                UNION
                SELECT fw.wallet
                FROM followed_wallets fw
                WHERE fw.status = 'active'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM wallet_memberships wm
                      WHERE wm.wallet = fw.wallet AND wm.scope = 'copy_execution'
                  )
                UNION
                SELECT ww.wallet
                FROM watchlist_wallets ww
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM wallet_memberships wm
                    WHERE wm.wallet = ww.wallet AND wm.scope = 'watchlist'
                )
                UNION
                SELECT wallet FROM followed_trades
                UNION
                SELECT wallet FROM followed_positions
            )
            SELECT wallet
            FROM universe
            WHERE wallet IS NOT NULL AND wallet <> ''
            ORDER BY wallet ASC
            """
        ).fetchall()
    else:
        rows = conn.execute(
            """
            WITH universe AS (
                SELECT wallet FROM followed_wallets WHERE status = 'active'
                UNION
                SELECT wallet FROM watchlist_wallets
                UNION
                SELECT wallet FROM followed_trades
                UNION
                SELECT wallet FROM followed_positions
            )
            SELECT wallet
            FROM universe
            WHERE wallet IS NOT NULL AND wallet <> ''
            ORDER BY wallet ASC
            """
        ).fetchall()
    return [str(row[0] or "") for row in rows if row[0]]


def upsert_strategy_research_note(
    conn: sqlite3.Connection,
    *,
    slug: str,
    title: str,
    category: str = "",
    status: str = "active",
    hypothesis: str = "",
    edge_summary: str = "",
    entry_logic: str = "",
    exit_logic: str = "",
    risk_notes: str = "",
    evidence: str = "",
    source_wallet: str = "",
    source_market: str = "",
    sample_size: int = 0,
    tags: Sequence[str] | None = None,
) -> None:
    slug_key = str(slug or "").strip().lower()
    if not slug_key:
        raise ValueError("slug is required")
    conn.execute(
        """
        INSERT INTO strategy_research (
            slug,
            title,
            category,
            status,
            hypothesis,
            edge_summary,
            entry_logic,
            exit_logic,
            risk_notes,
            evidence,
            source_wallet,
            source_market,
            sample_size,
            tags_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(slug) DO UPDATE SET
            title = excluded.title,
            category = excluded.category,
            status = excluded.status,
            hypothesis = excluded.hypothesis,
            edge_summary = excluded.edge_summary,
            entry_logic = excluded.entry_logic,
            exit_logic = excluded.exit_logic,
            risk_notes = excluded.risk_notes,
            evidence = excluded.evidence,
            source_wallet = excluded.source_wallet,
            source_market = excluded.source_market,
            sample_size = excluded.sample_size,
            tags_json = excluded.tags_json,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            slug_key,
            str(title or "").strip(),
            str(category or "").strip(),
            str(status or "active").strip(),
            str(hypothesis or "").strip(),
            str(edge_summary or "").strip(),
            str(entry_logic or "").strip(),
            str(exit_logic or "").strip(),
            str(risk_notes or "").strip(),
            str(evidence or "").strip(),
            _normalize_wallet(source_wallet),
            str(source_market or "").strip(),
            int(sample_size or 0),
            json.dumps(sorted({str(tag).strip() for tag in (tags or []) if str(tag).strip()})),
        ),
    )
    conn.commit()


def strategy_research_notes(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    limit: int = 200,
) -> List[sqlite3.Row]:
    where = ["1=1"]
    params: List[Any] = []
    if status:
        where.append("status = ?")
        params.append(str(status))
    params.append(int(limit))
    return conn.execute(
        f"""
        SELECT *
        FROM strategy_research
        WHERE {" AND ".join(where)}
        ORDER BY updated_at DESC, created_at DESC
        LIMIT ?
        """,
        params,
    ).fetchall()


def auto_follow_scope_wallets(conn: sqlite3.Connection) -> List[str]:
    membership_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='wallet_memberships' LIMIT 1"
    ).fetchone()
    if membership_table is not None:
        rows = conn.execute(
            """
            WITH universe AS (
                SELECT wallet
                FROM wallet_memberships
                WHERE scope = 'watchlist' AND status = 'active'
                UNION
                SELECT ww.wallet
                FROM watchlist_wallets ww
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM wallet_memberships wm
                    WHERE wm.wallet = ww.wallet AND wm.scope = 'watchlist'
                )
                UNION
                SELECT wallet FROM followed_trades
            )
            SELECT wallet
            FROM universe
            WHERE wallet IS NOT NULL AND wallet <> ''
            ORDER BY wallet ASC
            """
        ).fetchall()
    else:
        rows = conn.execute(
            """
            WITH universe AS (
                SELECT wallet FROM watchlist_wallets
                UNION
                SELECT wallet FROM followed_trades
            )
            SELECT wallet
            FROM universe
            WHERE wallet IS NOT NULL AND wallet <> ''
            ORDER BY wallet ASC
            """
        ).fetchall()
    return [str(row["wallet"] or "") for row in rows if row["wallet"]]


def sync_auto_followed_positions_for_wallet(
    conn: sqlite3.Connection,
    *,
    wallet: str,
    open_positions: Iterable[Dict[str, Any]],
    synced_ts: int | None = None,
) -> None:
    wallet = _normalize_wallet(wallet)
    if not wallet:
        return
    synced_value = int(synced_ts or time.time())
    key_rows: List[tuple[str, str, str, str, float, float, float, float, float]] = []
    seen_keys: set[tuple[str, str]] = set()
    for row in open_positions:
        condition_id = _normalize_condition_id(row.get("condition_id"))
        outcome = _normalize_outcome(row.get("outcome"))
        if not condition_id:
            continue
        key = (condition_id, outcome)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        cash_pnl_raw = row.get("cashPnl")
        realized_pnl_raw = row.get("realizedPnl")
        key_rows.append(
            (
                condition_id,
                outcome,
                str(row.get("title") or ""),
                str(row.get("event_slug") or ""),
                float(row.get("entry_price") or 0.0),
                float(row.get("current_price") or 0.0),
                float(row.get("entry_notional") or 0.0),
                float(row.get("current_amount") or 0.0),
                float(row.get("current_size") or 0.0),
                float(cash_pnl_raw) if cash_pnl_raw is not None else None,
                float(realized_pnl_raw) if realized_pnl_raw is not None else None,
            )
        )

    for (
        condition_id,
        outcome,
        title,
        event_slug,
        entry_price,
        current_price,
        entry_notional,
        current_amount,
        current_size,
        cash_pnl,
        realized_pnl,
    ) in key_rows:
        conn.execute(
            """
            INSERT INTO followed_positions (
                wallet,
                condition_id,
                outcome,
                title,
                event_slug,
                label,
                added_source,
                entry_price,
                current_price,
                entry_notional,
                current_amount,
                current_size,
                cash_pnl,
                realized_pnl,
                added_ts,
                last_synced_ts
            ) VALUES (?, ?, ?, ?, ?, '', 'auto_relevant', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(wallet, condition_id, outcome) DO UPDATE SET
                title = CASE
                    WHEN excluded.title <> '' THEN excluded.title
                    ELSE followed_positions.title
                END,
                event_slug = CASE
                    WHEN excluded.event_slug <> '' THEN excluded.event_slug
                    ELSE followed_positions.event_slug
                END,
                entry_price = CASE
                    WHEN excluded.entry_price > 0 THEN excluded.entry_price
                    ELSE followed_positions.entry_price
                END,
                current_price = CASE
                    WHEN excluded.current_price > 0 THEN excluded.current_price
                    ELSE followed_positions.current_price
                END,
                entry_notional = CASE
                    WHEN excluded.entry_notional > 0 THEN excluded.entry_notional
                    ELSE followed_positions.entry_notional
                END,
                current_amount = CASE
                    WHEN excluded.current_amount > 0 THEN excluded.current_amount
                    ELSE followed_positions.current_amount
                END,
                current_size = CASE
                    WHEN excluded.current_size > 0 THEN excluded.current_size
                    ELSE followed_positions.current_size
                END,
                cash_pnl = excluded.cash_pnl,
                realized_pnl = excluded.realized_pnl,
                last_synced_ts = excluded.last_synced_ts,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                wallet,
                condition_id,
                outcome,
                title,
                event_slug,
                entry_price,
                current_price,
                entry_notional,
                current_amount,
                current_size,
                cash_pnl,
                realized_pnl,
                synced_value,
                synced_value,
            ),
        )

    if key_rows:
        placeholders = ",".join("?" for _ in key_rows)
        pair_keys = [f"{condition_id}::{outcome}" for condition_id, outcome, *_rest in key_rows]
        conn.execute(
            f"""
            DELETE FROM followed_positions
            WHERE wallet = ?
              AND added_source = 'auto_relevant'
              AND (condition_id || '::' || outcome) NOT IN ({placeholders})
            """,
            (wallet, *pair_keys),
        )
    else:
        conn.execute(
            """
            DELETE FROM followed_positions
            WHERE wallet = ?
              AND added_source = 'auto_relevant'
            """,
            (wallet,),
        )
    conn.commit()


def followed_trade_wallets_for_sync(
    conn: sqlite3.Connection,
    stale_after_seconds: int,
    limit: int = 200,
) -> List[str]:
    rows = conn.execute(
        """
        SELECT wallet, MAX(last_synced_ts) AS max_synced
        FROM followed_trades
        GROUP BY wallet
        HAVING COALESCE(max_synced, 0) < (strftime('%s', 'now') - ?)
        ORDER BY max_synced ASC
        LIMIT ?
        """,
        (int(stale_after_seconds), int(limit)),
    ).fetchall()
    return [str(row["wallet"] or "") for row in rows if row["wallet"]]


def real_trades_positions_summary(
    conn: sqlite3.Connection,
    *,
    wallet: str,
    limit: int = 5000,
) -> Dict[str, Any]:
    """Aggregate imported real trades into open/closed position buckets.

    Reads from `real_trades` if present. If the table does not exist yet,
    returns an empty payload instead of raising.
    """
    normalized_wallet = _normalize_wallet(wallet)
    if not normalized_wallet:
        return {"wallet": "", "summary": {}, "open": [], "closed": []}

    def _to_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    def _to_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return int(default)

    def _empty_payload() -> Dict[str, Any]:
        return {
            "wallet": normalized_wallet,
            "summary": {
                "total_spent": 0.0,
                "total_received": 0.0,
                "net_pnl": 0.0,
                "open_count": 0,
                "closed_count": 0,
            },
            "open": [],
            "closed": [],
        }

    def _table_columns(table_name: str) -> set[str]:
        table_row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table_name,),
        ).fetchone()
        if table_row is None:
            return set()
        try:
            info_rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        except sqlite3.Error:
            return set()
        return {str(row[1] or "").strip() for row in info_rows}

    def _load_trade_rows(table_name: str, columns: set[str]) -> list[sqlite3.Row]:
        row_limit = max(1, int(limit))

        # Canonical imported schema.
        if table_name == "real_trades" and {
            "id",
            "wallet",
            "condition_id",
            "title",
            "event_slug",
            "outcome",
            "side",
            "price",
            "size",
            "usdc_size",
            "traded_at",
        }.issubset(columns):
            return conn.execute(
                """
                SELECT
                    COALESCE(condition_id, '') AS condition_id,
                    COALESCE(title, '') AS title,
                    COALESCE(event_slug, '') AS event_slug,
                    COALESCE(outcome, '') AS outcome,
                    COALESCE(side, '') AS side,
                    COALESCE(price, 0) AS price,
                    COALESCE(size, 0) AS size,
                    COALESCE(usdc_size, 0) AS usdc_size,
                    COALESCE(traded_at, 0) AS traded_at,
                    id AS trade_id
                FROM real_trades
                WHERE LOWER(wallet) = ?
                ORDER BY traded_at DESC, id DESC
                LIMIT ?
                """,
                (normalized_wallet, row_limit),
            ).fetchall()

        # Legacy local schema.
        if table_name == "real_trades" and {
            "trade_id",
            "wallet",
            "condition_id",
            "market_title",
            "outcome",
            "direction",
            "entry_price",
            "size_usd",
            "size_shares",
            "opened_at",
        }.issubset(columns):
            # Emit paired BUY + SELL rows so the aggregation logic can compute net_shares.
            # Open trades → single BUY row.
            # Closed trades → BUY row (entry) + SELL row (exit) so net_shares goes to 0.
            # Cancelled trades are excluded entirely (never filled).
            return conn.execute(
                """
                SELECT * FROM (
                    SELECT
                        COALESCE(condition_id, '') AS condition_id,
                        COALESCE(market_title, '') AS title,
                        '' AS event_slug,
                        COALESCE(outcome, '') AS outcome,
                        'BUY' AS side,
                        COALESCE(executed_price, entry_price, 0) AS price,
                        COALESCE(size_shares, 0) AS size,
                        COALESCE(
                            size_usd,
                            COALESCE(executed_price, entry_price, 0) * COALESCE(size_shares, 0)
                        ) AS usdc_size,
                        CAST(
                            COALESCE(
                                strftime('%s', opened_at),
                                strftime('%s', created_at),
                                0
                            ) AS INTEGER
                        ) AS traded_at,
                        trade_id AS trade_id
                    FROM real_trades
                    WHERE LOWER(wallet) = ?
                      AND COALESCE(status, 'open') != 'cancelled'

                    UNION ALL

                    SELECT
                        COALESCE(condition_id, '') AS condition_id,
                        COALESCE(market_title, '') AS title,
                        '' AS event_slug,
                        COALESCE(outcome, '') AS outcome,
                        'SELL' AS side,
                        COALESCE(exit_price, 0) AS price,
                        COALESCE(size_shares, 0) AS size,
                        COALESCE(exit_price, 0) * COALESCE(size_shares, 0) AS usdc_size,
                        CAST(
                            COALESCE(
                                strftime('%s', closed_at),
                                0
                            ) AS INTEGER
                        ) AS traded_at,
                        trade_id AS trade_id
                    FROM real_trades
                    WHERE LOWER(wallet) = ?
                      AND status = 'closed'
                      AND exit_price IS NOT NULL
                )
                ORDER BY traded_at DESC, trade_id DESC
                LIMIT ?
                """,
                (normalized_wallet, normalized_wallet, row_limit),
            ).fetchall()

        # Personal trade import table.
        if table_name == "my_trades" and {
            "id",
            "wallet",
            "condition_id",
            "title",
            "event_slug",
            "outcome",
            "side",
            "price",
            "size",
            "usdc_size",
            "traded_at",
        }.issubset(columns):
            return conn.execute(
                """
                SELECT
                    COALESCE(condition_id, '') AS condition_id,
                    COALESCE(title, '') AS title,
                    COALESCE(event_slug, '') AS event_slug,
                    COALESCE(outcome, '') AS outcome,
                    COALESCE(side, '') AS side,
                    COALESCE(price, 0) AS price,
                    COALESCE(size, 0) AS size,
                    COALESCE(usdc_size, 0) AS usdc_size,
                    COALESCE(
                        traded_at,
                        CAST(strftime('%s', traded_at_iso) AS INTEGER),
                        0
                    ) AS traded_at,
                    id AS trade_id
                FROM my_trades
                WHERE LOWER(wallet) = ?
                ORDER BY traded_at DESC, id DESC
                LIMIT ?
                """,
                (normalized_wallet, row_limit),
            ).fetchall()

        return []

    source_candidates = [
        ("real_trades", _table_columns("real_trades")),
        ("my_trades", _table_columns("my_trades")),
    ]
    rows: list[sqlite3.Row] = []
    for table_name, columns in source_candidates:
        if not columns:
            continue
        rows = _load_trade_rows(table_name, columns)
        if rows:
            break

    if not rows:
        return _empty_payload()

    grouped: Dict[tuple[str, str], Dict[str, Any]] = {}
    total_spent = 0.0
    total_received = 0.0

    for row in rows:
        condition_id = str(row["condition_id"] or "").strip()
        outcome = str(row["outcome"] or "").strip().lower()
        if not condition_id:
            continue
        key = (condition_id, outcome)
        bucket = grouped.get(key)
        if bucket is None:
            bucket = {
                "condition_id": condition_id,
                "title": str(row["title"] or "").strip(),
                "event_slug": str(row["event_slug"] or "").strip(),
                "outcome": outcome,
                "side": str(row["side"] or "").strip().upper(),
                "buy_shares": 0.0,
                "sell_shares": 0.0,
                "buy_notional": 0.0,
                "sell_notional": 0.0,
                "trades": 0,
                "last_traded_at": 0,
            }
            grouped[key] = bucket

        side = str(row["side"] or "").strip().upper()
        price = _to_float(row["price"])
        shares = _to_float(row["size"])
        notional = _to_float(row["usdc_size"], price * shares)
        traded_at = _to_int(row["traded_at"], 0)

        if side == "SELL":
            bucket["sell_shares"] += max(0.0, shares)
            bucket["sell_notional"] += max(0.0, notional)
            total_received += max(0.0, notional)
        else:
            bucket["buy_shares"] += max(0.0, shares)
            bucket["buy_notional"] += max(0.0, notional)
            total_spent += max(0.0, notional)

        bucket["trades"] += 1
        if traded_at > int(bucket["last_traded_at"] or 0):
            bucket["last_traded_at"] = traded_at

    open_items: List[Dict[str, Any]] = []
    closed_items: List[Dict[str, Any]] = []

    for bucket in grouped.values():
        buy_shares = float(bucket["buy_shares"] or 0.0)
        sell_shares = float(bucket["sell_shares"] or 0.0)
        buy_notional = float(bucket["buy_notional"] or 0.0)
        sell_notional = float(bucket["sell_notional"] or 0.0)
        net_shares = buy_shares - sell_shares
        avg_entry = (buy_notional / buy_shares) if buy_shares > 0 else 0.0
        avg_exit = (sell_notional / sell_shares) if sell_shares > 0 else 0.0
        last_traded_at = int(bucket["last_traded_at"] or 0)
        common = {
            "condition_id": str(bucket["condition_id"] or ""),
            "title": str(bucket["title"] or ""),
            "event_slug": str(bucket["event_slug"] or ""),
            "outcome": str(bucket["outcome"] or ""),
            "side": str(bucket["side"] or ""),
            "trades": int(bucket["trades"] or 0),
            "last_traded_at": last_traded_at,
            "last_traded_at_iso": (
                dt.datetime.fromtimestamp(last_traded_at, dt.UTC).isoformat()
                if last_traded_at > 0
                else ""
            ),
        }

        if net_shares > 1e-9:
            open_items.append(
                {
                    **common,
                    "spent": max(0.0, buy_notional - sell_notional),
                    "avg_entry": avg_entry,
                    "size_shares": net_shares,
                }
            )
        else:
            closed_items.append(
                {
                    **common,
                    "spent": buy_notional,
                    "received": sell_notional,
                    "pnl": sell_notional - buy_notional,
                    "avg_entry": avg_entry,
                    "avg_exit": avg_exit,
                }
            )

    open_items.sort(key=lambda row: int(row.get("last_traded_at") or 0), reverse=True)
    closed_items.sort(key=lambda row: int(row.get("last_traded_at") or 0), reverse=True)

    return {
        "wallet": normalized_wallet,
        "summary": {
            "total_spent": total_spent,
            "total_received": total_received,
            "net_pnl": total_received - total_spent,
            "open_count": len(open_items),
            "closed_count": len(closed_items),
        },
        "open": open_items,
        "closed": closed_items,
    }


# ============================================================================
# Human-in-Loop: Followed Wallets
# ============================================================================


def upsert_followed_wallet_record(
    conn: sqlite3.Connection,
    *,
    wallet: str,
    label: str = "",
    reason: str = "",
    status: str = "active",
    strategy: Dict[str, Any] | None = None,
    notes: str = "",
    total_positions: int = 0,
    total_pnl_usdc: float = 0.0,
    added_at: str | None = None,
    last_copied_at: str | None = None,
) -> None:
    normalized_wallet = _normalize_wallet(wallet)
    if not normalized_wallet:
        return
    status_value = str(status or "active").strip() or "active"
    strategy_json = json.dumps(strategy) if strategy is not None else None
    added_at_value = str(added_at or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
    conn.execute(
        """
        INSERT INTO followed_wallets
        (wallet, label, added_at, added_reason, status, strategy_json, notes,
         total_positions, total_pnl_usdc, last_copied_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(wallet) DO UPDATE SET
            label = excluded.label,
            added_reason = excluded.added_reason,
            status = excluded.status,
            strategy_json = COALESCE(excluded.strategy_json, followed_wallets.strategy_json),
            notes = excluded.notes,
            total_positions = excluded.total_positions,
            total_pnl_usdc = excluded.total_pnl_usdc,
            last_copied_at = COALESCE(excluded.last_copied_at, followed_wallets.last_copied_at),
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            normalized_wallet,
            str(label or "").strip(),
            added_at_value,
            str(reason or "").strip(),
            status_value,
            strategy_json,
            str(notes or "").strip(),
            int(total_positions or 0),
            float(total_pnl_usdc or 0.0),
            str(last_copied_at or "") or None,
        ),
    )
    _lazy_upsert_wallet_registry(
        conn,
        wallet=normalized_wallet,
        display_label=str(label or "").strip(),
        gate0_status="failed" if status_value == "gate0_failed" else None,
        copy_status={"active": "active", "paused": "paused", "gate0_failed": "gate0_failed"}.get(status_value, "inactive"),
        notes=str(notes or "").strip(),
    )
    _lazy_upsert_wallet_membership(
        conn,
        wallet=normalized_wallet,
        scope="copy_execution",
        status=status_value,
        label=str(label or "").strip(),
        reason=str(reason or "").strip(),
        source="followed_wallets",
        metadata={
            "strategy_json": strategy_json or "",
            "notes": str(notes or "").strip(),
            "total_positions": int(total_positions or 0),
            "total_pnl_usdc": float(total_pnl_usdc or 0.0),
            "last_copied_at": str(last_copied_at or ""),
            "added_at": added_at_value,
        },
    )
    if status_value in {"active", "paused"}:
        _lazy_upsert_wallet_tag(
            conn,
            wallet=normalized_wallet,
            tag="copy_execution",
            tag_type="scope",
            source="followed_wallets",
        )
    else:
        _lazy_deactivate_wallet_tag(
            conn,
            wallet=normalized_wallet,
            tag="copy_execution",
            tag_type="scope",
            source="followed_wallets",
        )


def set_followed_wallet_status(
    conn: sqlite3.Connection,
    *,
    wallet: str,
    status: str,
    updated_at: str | None = None,
    reason: str = "",
) -> None:
    normalized_wallet = _normalize_wallet(wallet)
    if not normalized_wallet:
        return
    status_value = str(status or "").strip() or "active"
    updated_at_value = str(updated_at or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
    conn.execute(
        "UPDATE followed_wallets SET status = ?, updated_at = ? WHERE wallet = ?",
        (status_value, updated_at_value, normalized_wallet),
    )
    row = conn.execute(
        """
        SELECT label, added_reason, strategy_json, notes, total_positions,
               total_pnl_usdc, last_copied_at, added_at
        FROM followed_wallets
        WHERE wallet = ?
        """,
        (normalized_wallet,),
    ).fetchone()
    if row:
        _lazy_upsert_wallet_registry(
            conn,
            wallet=normalized_wallet,
            display_label=str(row["label"] or ""),
            gate0_status="failed" if status_value == "gate0_failed" else None,
            copy_status={"active": "active", "paused": "paused", "gate0_failed": "gate0_failed"}.get(status_value, "inactive"),
            notes=str(row["notes"] or ""),
        )
        _lazy_upsert_wallet_membership(
            conn,
            wallet=normalized_wallet,
            scope="copy_execution",
            status=status_value,
            label=str(row["label"] or ""),
            reason=str(reason or row["added_reason"] or ""),
            source="followed_wallets",
            metadata={
                "strategy_json": str(row["strategy_json"] or ""),
                "notes": str(row["notes"] or ""),
                "total_positions": int(row["total_positions"] or 0),
                "total_pnl_usdc": float(row["total_pnl_usdc"] or 0.0),
                "last_copied_at": str(row["last_copied_at"] or ""),
                "added_at": str(row["added_at"] or ""),
            },
        )
    if status_value in {"active", "paused"}:
        _lazy_upsert_wallet_tag(
            conn,
            wallet=normalized_wallet,
            tag="copy_execution",
            tag_type="scope",
            source="followed_wallets",
        )
    else:
        _lazy_deactivate_wallet_tag(
            conn,
            wallet=normalized_wallet,
            tag="copy_execution",
            tag_type="scope",
            source="followed_wallets",
        )


def sync_followed_wallet_membership(conn: sqlite3.Connection, wallet: str) -> None:
    normalized_wallet = _normalize_wallet(wallet)
    if not normalized_wallet:
        return
    row = conn.execute(
        """
        SELECT label, added_reason, status, strategy_json, notes, total_positions,
               total_pnl_usdc, last_copied_at, added_at
        FROM followed_wallets
        WHERE wallet = ?
        """,
        (normalized_wallet,),
    ).fetchone()
    if not row:
        _lazy_deactivate_wallet_membership(
            conn,
            wallet=normalized_wallet,
            scope="copy_execution",
            status="inactive",
            reason="missing legacy followed_wallets row",
        )
        _lazy_deactivate_wallet_tag(
            conn,
            wallet=normalized_wallet,
            tag="copy_execution",
            tag_type="scope",
            source="followed_wallets",
        )
        return
    status_value = str(row["status"] or "active").strip() or "active"
    _lazy_upsert_wallet_registry(
        conn,
        wallet=normalized_wallet,
        display_label=str(row["label"] or ""),
        gate0_status="failed" if status_value == "gate0_failed" else None,
        copy_status={"active": "active", "paused": "paused", "gate0_failed": "gate0_failed"}.get(status_value, "inactive"),
        notes=str(row["notes"] or ""),
    )
    _lazy_upsert_wallet_membership(
        conn,
        wallet=normalized_wallet,
        scope="copy_execution",
        status=status_value,
        label=str(row["label"] or ""),
        reason=str(row["added_reason"] or ""),
        source="followed_wallets",
        metadata={
            "strategy_json": str(row["strategy_json"] or ""),
            "notes": str(row["notes"] or ""),
            "total_positions": int(row["total_positions"] or 0),
            "total_pnl_usdc": float(row["total_pnl_usdc"] or 0.0),
            "last_copied_at": str(row["last_copied_at"] or ""),
            "added_at": str(row["added_at"] or ""),
        },
    )
    if status_value in {"active", "paused"}:
        _lazy_upsert_wallet_tag(
            conn,
            wallet=normalized_wallet,
            tag="copy_execution",
            tag_type="scope",
            source="followed_wallets",
        )
    else:
        _lazy_deactivate_wallet_tag(
            conn,
            wallet=normalized_wallet,
            tag="copy_execution",
            tag_type="scope",
            source="followed_wallets",
        )


def get_followed_wallets(status: str = "active") -> List[sqlite3.Row]:
    """Get list of wallets currently being followed."""
    with _lazy_connection_scope() as conn:
        membership_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='wallet_memberships' LIMIT 1"
        ).fetchone()
        if membership_table is not None:
            rows = conn.execute(
                """
                WITH canonical AS (
                    SELECT wallet,
                           label,
                           status,
                           reason,
                           COALESCE(NULLIF(CAST(json_extract(metadata_json, '$.added_at') AS TEXT), ''), created_at) AS added_at,
                           COALESCE(CAST(json_extract(metadata_json, '$.strategy_json') AS TEXT), '') AS strategy_json,
                           COALESCE(CAST(json_extract(metadata_json, '$.total_positions') AS INTEGER), 0) AS total_positions,
                           COALESCE(CAST(json_extract(metadata_json, '$.total_pnl_usdc') AS REAL), 0.0) AS total_pnl_usdc,
                           NULLIF(CAST(json_extract(metadata_json, '$.last_copied_at') AS TEXT), '') AS last_copied_at,
                           updated_at
                    FROM wallet_memberships
                    WHERE scope = 'copy_execution' AND status = ?
                ),
                fallback AS (
                    SELECT fw.wallet,
                           fw.label,
                           fw.status,
                           COALESCE(fw.added_reason, '') AS reason,
                           fw.added_at,
                           COALESCE(fw.strategy_json, '') AS strategy_json,
                           COALESCE(fw.total_positions, 0) AS total_positions,
                           COALESCE(fw.total_pnl_usdc, 0.0) AS total_pnl_usdc,
                           fw.last_copied_at,
                           fw.updated_at
                    FROM followed_wallets fw
                    WHERE fw.status = ?
                      AND NOT EXISTS (
                          SELECT 1
                          FROM wallet_memberships wm
                          WHERE wm.wallet = fw.wallet AND wm.scope = 'copy_execution'
                      )
                )
                SELECT wallet, label, status, reason, added_at, strategy_json,
                       total_positions, total_pnl_usdc, last_copied_at
                FROM canonical
                UNION ALL
                SELECT wallet, label, status, reason, added_at, strategy_json,
                       total_positions, total_pnl_usdc, last_copied_at
                FROM fallback
                ORDER BY added_at DESC
                """,
                (status, status),
            ).fetchall()
            if rows:
                return rows
        cur = conn.execute(
            """
            SELECT wallet, label, status, COALESCE(added_reason, '') AS reason, added_at, strategy_json,
                   total_positions, total_pnl_usdc, last_copied_at
            FROM followed_wallets
            WHERE status = ?
            ORDER BY added_at DESC
            """,
            (status,),
        )
        return cur.fetchall()


## Dead code removed (audit 2026-03-30):
## update_followed_wallet_strategy, add_copied_position, get_open_positions,
## close_copied_position — all never called. close_copied_position had wrong
## PnL formula (notional vs shares, C2 in audit).


# ---------------------------------------------------------------------------
# External leaderboard helpers (V2)
# ---------------------------------------------------------------------------


def upsert_external_leaderboard_entry(
    conn: sqlite3.Connection,
    *,
    wallet: str,
    category: str = "OVERALL",
    time_period: str = "ALL",
    order_by: str = "PNL",
    rank: int = 0,
    official_pnl: float = 0.0,
    official_volume: float = 0.0,
    username: str = "",
    profile_image: str = "",
    x_username: str = "",
    verified_badge: bool = False,
    fetched_at: int = 0,
) -> None:
    wallet = _normalize_wallet(wallet)
    if not wallet:
        return
    conn.execute(
        """
        INSERT INTO external_leaderboard_entries
            (wallet, category, time_period, order_by, rank,
             official_pnl, official_volume, username, profile_image,
             x_username, verified_badge, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(wallet, category, time_period, order_by) DO UPDATE SET
            rank = excluded.rank,
            official_pnl = excluded.official_pnl,
            official_volume = excluded.official_volume,
            username = CASE WHEN excluded.username <> '' THEN excluded.username
                           ELSE external_leaderboard_entries.username END,
            profile_image = CASE WHEN excluded.profile_image <> '' THEN excluded.profile_image
                                ELSE external_leaderboard_entries.profile_image END,
            x_username = CASE WHEN excluded.x_username <> '' THEN excluded.x_username
                             ELSE external_leaderboard_entries.x_username END,
            verified_badge = excluded.verified_badge,
            fetched_at = excluded.fetched_at
        """,
        (
            wallet,
            str(category).upper(),
            str(time_period).upper(),
            str(order_by).upper(),
            int(rank),
            float(official_pnl),
            float(official_volume),
            str(username),
            str(profile_image),
            str(x_username),
            1 if verified_badge else 0,
            int(fetched_at or int(time.time())),
        ),
    )



# ═══════════════════════════════════════════════════════════════════════════════
# Research capture – unfiltered position snapshots for watchlist wallets
# ═══════════════════════════════════════════════════════════════════════════════


def insert_position_snapshots(
    conn: sqlite3.Connection,
    *,
    wallet: str,
    positions: Iterable[Dict[str, Any]],
    snapshot_ts: int,
) -> int:
    """Append full position snapshots for a wallet (no value floor).

    Each call writes one row per position. The same (wallet, condition_id,
    outcome, snapshot_ts) may appear across syncs — that is fine; the table is
    append-only and snapshot_id disambiguates.
    """
    payload = _build_position_snapshot_payload(
        wallet=wallet,
        positions=positions,
        snapshot_ts=snapshot_ts,
    )

    if not payload:
        return 0

    conn.executemany(
        """
        INSERT INTO position_snapshots (
            wallet, condition_id, outcome, title,
            avg_price, current_price, size, current_value, initial_value,
            cash_pnl, pnl_percent, resolved, snapshot_ts
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        payload,
    )
    conn.commit()
    return len(payload)


def _build_position_snapshot_payload(
    *,
    wallet: str,
    positions: Iterable[Dict[str, Any]],
    snapshot_ts: int,
) -> List[tuple]:
    """Normalize raw Polymarket positions into position_snapshots rows."""
    wallet_key = _normalize_wallet(wallet)
    if not wallet_key:
        return []

    payload: List[tuple] = []
    for pos in positions:
        if not isinstance(pos, dict):
            continue
        condition_id = _normalize_condition_id(
            pos.get("conditionId") or pos.get("condition_id") or ""
        )
        if not condition_id:
            continue
        outcome = str(pos.get("outcome") or "").strip()
        title = str(pos.get("title") or pos.get("question") or "").strip()
        avg_price = float(pos.get("avgPrice") or pos.get("avg_price") or 0)
        cur_price = float(pos.get("curPrice") or pos.get("current_price") or 0)
        size = float(pos.get("size") or 0)
        current_value = size * cur_price
        initial_value = float(pos.get("initialValue") or pos.get("initial_value") or 0)
        cash_pnl = float(pos.get("cashPnl") or pos.get("cash_pnl") or 0)
        pnl_pct = ((cur_price - avg_price) / avg_price * 100) if avg_price > 0 else 0
        resolved = 1 if (cur_price >= 0.99 or cur_price <= 0.01) else 0

        payload.append((
            wallet_key, condition_id, outcome, title,
            avg_price, cur_price, size, current_value, initial_value,
            cash_pnl, pnl_pct, resolved, snapshot_ts,
        ))
    return payload


def _position_snapshot_signature_from_payload(payload: Iterable[tuple]) -> tuple:
    """Return a stable structural signature for change-only capture.

    Uses only structural fields: (condition_id, outcome, size, avg_price).
    Excludes mark-to-market fields (current_price, current_value, cash_pnl,
    pnl_percent) so routine price drift does not trigger new snapshot writes.
    """
    sig: List[tuple] = []
    for row in payload:
        # payload tuple layout:
        # wallet, condition_id, outcome, title, avg_price, current_price, size,
        # current_value, initial_value, cash_pnl, pnl_percent, resolved, snapshot_ts
        sig.append((
            str(row[1] or "").lower(),   # condition_id
            str(row[2] or "").lower(),   # outcome
            round(float(row[6] or 0.0), 6),  # size
            round(float(row[4] or 0.0), 6),  # avg_price
        ))
    return tuple(sorted(sig))


def _latest_position_snapshot_signature(
    conn: sqlite3.Connection,
    wallet: str,
) -> tuple:
    """Return the structural signature for the latest stored snapshot set.

    Uses only structural fields (condition_id, outcome, size, avg_price)
    to match ``_position_snapshot_signature_from_payload``.
    """
    wallet_key = _normalize_wallet(wallet)
    if not wallet_key:
        return tuple()
    rows = conn.execute(
        """
        SELECT condition_id, outcome, size, avg_price
        FROM position_snapshots
        WHERE wallet = ?
          AND snapshot_ts = (
              SELECT MAX(snapshot_ts) FROM position_snapshots WHERE wallet = ?
          )
        """,
        (wallet_key, wallet_key),
    ).fetchall()
    sig = [
        (
            str(r[0] or "").lower(),   # condition_id
            str(r[1] or "").lower(),   # outcome
            round(float(r[2] or 0.0), 6),  # size
            round(float(r[3] or 0.0), 6),  # avg_price
        )
        for r in rows
    ]
    return tuple(sorted(sig))


def prune_position_snapshots(
    conn: sqlite3.Connection,
    wallet: str,
    *,
    keep_recent_snapshots: int = 500,
    keep_daily_days: int = 365,
    now_ts: int | None = None,
) -> int:
    """Prune old wallet snapshots while preserving recent and daily history.

    Policy:
    - Keep the most recent `keep_recent_snapshots` snapshot timestamps in full.
    - For older snapshots within `keep_daily_days`, keep only the latest
      snapshot timestamp per UTC day.
    - Drop everything older than that window.

    Retention works at the snapshot_ts level so a full multi-position wallet
    snapshot is kept or dropped together.
    """
    wallet_key = _normalize_wallet(wallet)
    if not wallet_key:
        return 0

    snapshot_rows = conn.execute(
        """
        SELECT DISTINCT snapshot_ts
        FROM position_snapshots
        WHERE wallet = ?
        ORDER BY snapshot_ts DESC
        """,
        (wallet_key,),
    ).fetchall()
    snapshot_ts_list = [int(r[0] or 0) for r in snapshot_rows if int(r[0] or 0) > 0]
    if len(snapshot_ts_list) <= keep_recent_snapshots:
        return 0

    keep_ts = set(snapshot_ts_list[:keep_recent_snapshots])
    cutoff_ts = int(now_ts or time.time()) - max(0, keep_daily_days) * 86400
    daily_seen: set[str] = {
        dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).strftime("%Y-%m-%d")
        for ts in snapshot_ts_list[:keep_recent_snapshots]
    }
    for ts in snapshot_ts_list[keep_recent_snapshots:]:
        if ts < cutoff_ts:
            continue
        day_key = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).strftime("%Y-%m-%d")
        if day_key in daily_seen:
            continue
        daily_seen.add(day_key)
        keep_ts.add(ts)

    delete_ts = [ts for ts in snapshot_ts_list if ts not in keep_ts]
    if not delete_ts:
        return 0

    placeholders = ",".join("?" for _ in delete_ts)
    cur = conn.execute(
        f"""
        DELETE FROM position_snapshots
        WHERE wallet = ?
          AND snapshot_ts IN ({placeholders})
        """,
        (wallet_key, *delete_ts),
    )
    conn.commit()
    return int(cur.rowcount or 0)


def capture_position_snapshots(
    conn: sqlite3.Connection,
    *,
    wallet: str,
    positions: Iterable[Dict[str, Any]],
    snapshot_ts: int,
    keep_recent_snapshots: int = 500,
    keep_daily_days: int = 365,
    force_write: bool = False,
) -> Dict[str, Any]:
    """Capture wallet snapshots, then prune old rows.

    By default, only writes when the structural position signature changes
    (condition_id, outcome, size, avg_price). Set ``force_write=True`` to
    write every cycle regardless (used for active-signal wallets that need
    dense mark-to-market history).
    """
    payload = _build_position_snapshot_payload(
        wallet=wallet,
        positions=positions,
        snapshot_ts=snapshot_ts,
    )
    if not payload:
        return {"inserted": 0, "pruned": 0, "changed": False}

    wallet_key = _normalize_wallet(wallet)
    if not force_write:
        latest_sig = _latest_position_snapshot_signature(conn, wallet_key)
        new_sig = _position_snapshot_signature_from_payload(payload)
        if latest_sig and latest_sig == new_sig:
            return {"inserted": 0, "pruned": 0, "changed": False}

    conn.executemany(
        """
        INSERT INTO position_snapshots (
            wallet, condition_id, outcome, title,
            avg_price, current_price, size, current_value, initial_value,
            cash_pnl, pnl_percent, resolved, snapshot_ts
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        payload,
    )
    pruned = prune_position_snapshots(
        conn,
        wallet_key,
        keep_recent_snapshots=keep_recent_snapshots,
        keep_daily_days=keep_daily_days,
        now_ts=snapshot_ts,
    )
    conn.commit()
    return {"inserted": len(payload), "pruned": pruned, "changed": True}


def latest_position_snapshot_ts(
    conn: sqlite3.Connection,
    wallet: str,
) -> int:
    """Return the most recent snapshot_ts for a wallet, or 0 if none."""
    row = conn.execute(
        "SELECT MAX(snapshot_ts) AS ts FROM position_snapshots WHERE wallet = ?",
        (_normalize_wallet(wallet),),
    ).fetchone()
    return int(row[0] or 0) if row else 0


def wallet_has_open_signals(conn: sqlite3.Connection, wallet: str) -> bool:
    """Return True if the wallet has at least one open row in position_signals."""
    row = conn.execute(
        "SELECT 1 FROM position_signals WHERE wallet = ? AND is_open = 1 LIMIT 1",
        (_normalize_wallet(wallet),),
    ).fetchone()
    return row is not None


def position_snapshot_history(
    conn: sqlite3.Connection,
    wallet: str,
    *,
    condition_id: str | None = None,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    """Return position snapshots for a wallet, optionally filtered by market."""
    wallet_key = _normalize_wallet(wallet)
    if condition_id:
        cur = conn.execute(
            """SELECT * FROM position_snapshots
               WHERE wallet = ? AND condition_id = ?
               ORDER BY snapshot_ts DESC LIMIT ?""",
            (wallet_key, condition_id, limit),
        )
    else:
        cur = conn.execute(
            """SELECT * FROM position_snapshots
               WHERE wallet = ?
               ORDER BY snapshot_ts DESC LIMIT ?""",
            (wallet_key, limit),
        )
    cols = [d[0] for d in cur.description] if cur.description else []
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def research_wallet_activity_max_ts(
    conn: sqlite3.Connection,
    wallet: str,
) -> int:
    """Return the most recent activity_ts for a wallet, or 0."""
    row = conn.execute(
        "SELECT MAX(activity_ts) AS ts FROM followed_wallet_activity WHERE wallet = ?",
        (_normalize_wallet(wallet),),
    ).fetchone()
    return int(row[0] or 0) if row else 0


def research_position_building_clusters(
    conn: sqlite3.Connection,
    wallet: str,
    *,
    min_trades: int = 3,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Detect position-building clusters from activity data.

    Groups activity by (wallet, condition_id, outcome, side, date) and
    labels each cluster based on trade count and context.

    Labels:
    - twap_build: >= 10 buys in a day on the same market (algorithmic execution)
    - position_add: 3-9 buys on the same market in a day
    - trimming: 3+ sells on an existing position in a day
    - single_block: large single entry (for completeness, not returned here)

    Returns list of cluster dicts sorted by date desc, total_usdc desc.
    """
    wallet_key = _normalize_wallet(wallet)
    if not wallet_key:
        return []

    rows = conn.execute(
        """
        SELECT
            condition_id,
            title,
            side,
            outcome,
            DATE(activity_ts, 'unixepoch') AS trade_date,
            COUNT(*) AS trade_count,
            SUM(usdc_size) AS total_usdc,
            MIN(price) AS min_price,
            MAX(price) AS max_price,
            AVG(price) AS avg_price,
            MIN(activity_ts) AS first_ts,
            MAX(activity_ts) AS last_ts
        FROM followed_wallet_activity
        WHERE wallet = ? AND activity_type = 'TRADE'
        GROUP BY condition_id, outcome, side, trade_date
        HAVING trade_count >= ?
        ORDER BY trade_date DESC, total_usdc DESC
        LIMIT ?
        """,
        (wallet_key, min_trades, limit),
    ).fetchall()

    clusters: List[Dict[str, Any]] = []
    for r in rows:
        tc = int(r["trade_count"])
        side = str(r["side"] or "").upper()
        span_seconds = int(r["last_ts"] or 0) - int(r["first_ts"] or 0)

        if side == "BUY" and tc >= 10:
            label = "twap_build"
        elif side == "BUY":
            label = "position_add"
        elif side == "SELL" and tc >= 10:
            label = "twap_exit"
        else:
            label = "trimming"

        clusters.append({
            "condition_id": r["condition_id"],
            "title": r["title"] or "",
            "side": side,
            "outcome": r["outcome"] or "",
            "trade_date": r["trade_date"],
            "trade_count": tc,
            "total_usdc": round(float(r["total_usdc"] or 0), 2),
            "min_price": round(float(r["min_price"] or 0), 4),
            "max_price": round(float(r["max_price"] or 0), 4),
            "avg_price": round(float(r["avg_price"] or 0), 4),
            "span_seconds": span_seconds,
            "first_ts": int(r["first_ts"] or 0),
            "last_ts": int(r["last_ts"] or 0),
            "label": label,
        })

    return clusters
