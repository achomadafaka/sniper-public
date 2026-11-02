import datetime as dt
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List

from db import (
    auto_follow_scope_wallets,
    followed_positions,
    followed_trade_wallets_for_sync,
    followed_trades,
    get_connection,
    ignored_wallet_set,
    mark_watchlist_wallet_synced,
    prune_followed_positions_for_wallet,
    sync_auto_followed_positions_for_wallet,
    update_followed_trade_status,
    upsert_watchlist_trades,
    watchlist_wallets,
)
from sniper.filters import is_excluded_market
from sniper.markets.polymarket_api import (
    build_session,
    fetch_user_positions,
    fetch_user_trades,
)
from sniper.utils import (
    _normalize_wallet,
    _optional_float,
    _parse_any_ts,
    _safe_float,
    _safe_int,
)

WALLET_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


def _unwrap_positions(result) -> List[Dict[str, Any]]:
    """Unpack fetch_user_positions result regardless of return type.

    fetch_user_positions returns (rows, FetchTermination). Legacy test mocks
    return a plain list. This helper handles both cases without requiring
    callers to change when mocks haven't been updated.
    """
    if isinstance(result, tuple):
        rows, _ft = result
        return rows
    return list(result)


@dataclass(frozen=True)
class SyncConfig:
    watchlist_trade_fetch_limit: int
    watchlist_sync_min_interval_seconds: int
    watchlist_max_sync_wallets_per_request: int
    max_followed_ui_trades: int
    followed_trade_sync_min_interval_seconds: int
    followed_max_sync_wallets_per_request: int
    followed_position_sync_min_interval_seconds: int
    followed_position_max_sync_wallets_per_request: int
    max_followed_position_ui_rows: int
    position_min_open_value: float
    position_settlement_price_eps: float
    position_settlement_end_grace_seconds: int
    external_leaderboard_sync_interval_seconds: int = 3600


def _wallet_valid(wallet: str) -> bool:
    return bool(WALLET_RE.fullmatch(wallet))


def _ignored_wallets(conn_obj) -> set[str]:
    return {_normalize_wallet(wallet) for wallet in ignored_wallet_set(conn_obj)}


def position_key(condition_id: Any, outcome: Any) -> tuple[str, str]:
    return (str(condition_id or "").strip().lower(), str(outcome or "").strip().lower())


def followed_position_key(wallet: str, condition_id: str, outcome: str) -> tuple[str, str, str]:
    return (
        _normalize_wallet(wallet),
        str(condition_id or "").strip().lower(),
        str(outcome or "").strip().lower(),
    )


def _derive_entry_notional(row: Dict[str, Any], entry_price: float, current_size: float) -> float:
    """Derive open-position cost basis from stable position fields.

    Primary: avgPrice * size (passed in as entry_price * current_size).
    Fallback: initialValue (Polymarket's position snapshot value).
    Excluded: totalBought (cumulative gross flow, NOT cost basis).
    """
    if entry_price > 0 and current_size > 0:
        return float(entry_price * current_size)

    initial_value = _safe_float(row.get("initialValue"))
    if initial_value > 0:
        return float(initial_value)

    return 0.0


def _status_rank(status: str) -> int:
    key = str(status or "").strip().lower()
    rank = {
        "active": 5,
        "pending_settlement_win": 4,
        "pending_settlement_loss": 1,
        "won": 4,
        "closed_early_win": 3,
        "closed": 2,
        "closed_early": 2,
        "lost": 1,
        "closed_early_loss": 1,
        "unknown": 0,
    }
    return rank.get(key, 0)


def _build_position_maps(rows: List[Dict[str, Any]]) -> Dict[tuple[str, str], Dict[str, Any]]:
    by_key: Dict[tuple[str, str], Dict[str, Any]] = {}
    for row in rows:
        key = position_key(row.get("conditionId"), row.get("outcome"))
        if not key[0]:
            continue
        prev = by_key.get(key)
        if prev is None:
            by_key[key] = row
            continue
        if _safe_float(row.get("currentValue")) > _safe_float(prev.get("currentValue")):
            by_key[key] = row
    return by_key


def _build_closed_position_maps(rows: List[Dict[str, Any]]) -> Dict[tuple[str, str], Dict[str, Any]]:
    by_key: Dict[tuple[str, str], Dict[str, Any]] = {}
    for row in rows:
        key = position_key(row.get("conditionId"), row.get("outcome"))
        if not key[0]:
            continue
        prev = by_key.get(key)
        if prev is None or _safe_int(row.get("timestamp")) >= _safe_int(prev.get("timestamp")):
            by_key[key] = row
    return by_key


def fetch_wallet_position_maps(
    wallet: str,
    *,
    http_session=None,
) -> tuple[Dict[tuple[str, str], Dict[str, Any]], Dict[tuple[str, str], Dict[str, Any]]]:
    session = http_session or build_session()
    open_rows = _unwrap_positions(fetch_user_positions(session, wallet, endpoint="positions"))
    closed_rows = _unwrap_positions(fetch_user_positions(session, wallet, endpoint="closed-positions"))
    return _build_position_maps(open_rows), _build_closed_position_maps(closed_rows)


def resolve_followed_status(
    *,
    open_position: Dict[str, Any] | None,
    closed_position: Dict[str, Any] | None,
    position_settlement_price_eps: float,
    position_settlement_end_grace_seconds: int,
) -> Dict[str, Any]:
    if open_position is not None:
        cur_price = _optional_float(open_position.get("curPrice"))
        current_value = _safe_float(open_position.get("currentValue"))
        size = max(0.0, _safe_float(open_position.get("size")))
        cash_pnl = _safe_float(open_position.get("cashPnl"))
        end_ts = _parse_any_ts(open_position.get("endDate"))
        now_ts = int(dt.datetime.now(dt.UTC).timestamp())
        end_reached = bool(end_ts and now_ts >= (end_ts - position_settlement_end_grace_seconds))
        eps = max(0.0, position_settlement_price_eps)

        value_ratio = 0.0
        if size > 0:
            value_ratio = current_value / size
        likely_win = False
        likely_loss = False
        if cur_price is not None:
            likely_win = cur_price >= (1.0 - eps)
            likely_loss = cur_price <= eps
        if size > 0:
            if value_ratio >= (1.0 - eps):
                likely_win = True
            if value_ratio <= eps:
                likely_loss = True

        if end_reached and likely_win:
            status = "pending_settlement_win"
            status_label = "Pending Settlement (Likely Won)"
            status_reason = "Event has ended and payout is at/near 1.0; awaiting final settlement"
            is_win: bool | None = True
        elif end_reached and likely_loss:
            status = "pending_settlement_loss"
            status_label = "Pending Settlement (Likely Lost)"
            status_reason = "Event has ended and payout is at/near 0.0; awaiting final settlement"
            is_win = False
        elif end_reached and cash_pnl > 0:
            status = "pending_settlement_win"
            status_label = "Pending Settlement (Likely Won)"
            status_reason = "Event has ended and open cash PNL is positive; awaiting final settlement"
            is_win = True
        elif end_reached and cash_pnl < 0:
            status = "pending_settlement_loss"
            status_label = "Pending Settlement (Likely Lost)"
            status_reason = "Event has ended and open cash PNL is negative; awaiting final settlement"
            is_win = False
        else:
            status = "active"
            status_label = "Active"
            status_reason = "Position is currently open"
            is_win = None

        return {
            "status": status,
            "status_label": status_label,
            "status_reason": status_reason,
            "is_closed": False,
            "is_win": is_win,
            "exited_early": None,
            "live_price": cur_price,
            "exit_price": None,
            "current_value": current_value,
            "realized_pnl": _safe_float(open_position.get("realizedPnl")),
            "close_ts": None,
            "event_end_ts": end_ts,
            "status_rank": _status_rank(status),
        }

    if closed_position is not None:
        realized_pnl = _safe_float(closed_position.get("realizedPnl"))
        close_ts = _parse_any_ts(closed_position.get("timestamp"))
        event_end_ts = _parse_any_ts(closed_position.get("endDate"))
        exited_early = bool(close_ts and event_end_ts and close_ts < event_end_ts)

        if exited_early:
            if realized_pnl > 0:
                status = "closed_early_win"
                status_label = "Took Win Early"
                status_reason = "Closed before event end with positive realized PNL"
                is_win: bool | None = True
            elif realized_pnl < 0:
                status = "closed_early_loss"
                status_label = "Took Loss Early"
                status_reason = "Closed before event end with negative realized PNL"
                is_win = False
            else:
                status = "closed_early"
                status_label = "Closed Early"
                status_reason = "Position closed before event end"
                is_win = None
        else:
            if realized_pnl > 0:
                status = "won"
                status_label = "Won"
                status_reason = "Position closed with positive realized PNL"
                is_win = True
            elif realized_pnl < 0:
                status = "lost"
                status_label = "Lost"
                status_reason = "Position closed with negative realized PNL"
                is_win = False
            else:
                status = "closed"
                status_label = "Closed"
                status_reason = "Position closed with flat realized PNL"
                is_win = None

        return {
            "status": status,
            "status_label": status_label,
            "status_reason": status_reason,
            "is_closed": True,
            "is_win": is_win,
            "exited_early": exited_early,
            "live_price": None,
            "exit_price": _optional_float(closed_position.get("curPrice")),
            "current_value": 0.0,
            "realized_pnl": realized_pnl,
            "close_ts": close_ts,
            "event_end_ts": event_end_ts,
            "status_rank": _status_rank(status),
        }

    return {
        "status": "unknown",
        "status_label": "Unknown",
        "status_reason": "No matching open or closed position found yet",
        "is_closed": False,
        "is_win": None,
        "exited_early": None,
        "live_price": None,
        "exit_price": None,
        "current_value": 0.0,
        "realized_pnl": 0.0,
        "close_ts": None,
        "event_end_ts": None,
        "status_rank": _status_rank("unknown"),
    }


def is_excluded_position_record(record: Dict[str, Any]) -> bool:
    excluded, _ = is_excluded_market(
        record.get("title") or record.get("question"),
        record.get("slug"),
        record.get("eventSlug") or record.get("event_slug"),
    )
    return bool(excluded)


def sync_watchlist_wallet(
    wallet: str,
    *,
    config: SyncConfig,
    force: bool = False,
    conn_obj=None,
    http_session=None,
) -> None:
    conn_use = conn_obj or get_connection()
    session = http_session or build_session()
    wallet = _normalize_wallet(wallet)
    if not _wallet_valid(wallet):
        return
    if wallet in _ignored_wallets(conn_use):
        return

    now_ts = int(dt.datetime.now(dt.UTC).timestamp())
    rows = [row for row in watchlist_wallets(conn_use) if row["wallet"] == wallet]
    if not rows:
        return
    last_synced_ts = int(rows[0]["last_synced_ts"] or 0)
    if not force and now_ts - last_synced_ts < config.watchlist_sync_min_interval_seconds:
        return

    try:
        trades = fetch_user_trades(session, wallet, limit=config.watchlist_trade_fetch_limit)
        upsert_watchlist_trades(conn_use, wallet, trades)
        mark_watchlist_wallet_synced(conn_use, wallet, now_ts)
    except Exception:
        logging.exception("watchlist sync failed wallet=%s", wallet)


def sync_all_watchlist_wallets(
    *,
    config: SyncConfig,
    force: bool = False,
    conn_obj=None,
    http_session=None,
) -> None:
    conn_use = conn_obj or get_connection()
    rows = list(watchlist_wallets(conn_use))
    if not force and config.watchlist_max_sync_wallets_per_request > 0:
        now_ts = int(dt.datetime.now(dt.UTC).timestamp())
        stale_rows = [
            row
            for row in rows
            if now_ts - _safe_int(row["last_synced_ts"]) >= config.watchlist_sync_min_interval_seconds
        ]
        stale_rows.sort(key=lambda row: _safe_int(row["last_synced_ts"]))
        rows = stale_rows[: config.watchlist_max_sync_wallets_per_request]

    for row in rows:
        try:
            sync_watchlist_wallet(
                str(row["wallet"] or ""),
                force=force,
                config=config,
                conn_obj=conn_use,
                http_session=http_session,
            )
        except Exception:
            logging.exception("watchlist batch sync failed wallet=%s", str(row["wallet"] or ""))


def sync_followed_wallet(
    wallet: str,
    *,
    config: SyncConfig,
    force: bool = False,
    conn_obj=None,
    http_session=None,
) -> None:
    conn_use = conn_obj or get_connection()
    wallet = _normalize_wallet(wallet)
    if not _wallet_valid(wallet):
        return
    if wallet in _ignored_wallets(conn_use):
        return

    rows = followed_trades(conn_use, limit=config.max_followed_ui_trades, wallet=wallet)
    if not rows:
        return

    now_ts = int(dt.datetime.now(dt.UTC).timestamp())
    if not force:
        max_synced = max((_safe_int(row["last_synced_ts"]) for row in rows), default=0)
        if now_ts - max_synced < config.followed_trade_sync_min_interval_seconds:
            return

    try:
        open_by_key, closed_by_key = fetch_wallet_position_maps(wallet, http_session=http_session)
    except Exception:
        return

    for tracked in rows:
        try:
            key = position_key(tracked["condition_id"], tracked["outcome"])
            open_pos = open_by_key.get(key)
            closed_pos = closed_by_key.get(key)
            status = resolve_followed_status(
                open_position=open_pos,
                closed_position=closed_pos,
                position_settlement_price_eps=config.position_settlement_price_eps,
                position_settlement_end_grace_seconds=config.position_settlement_end_grace_seconds,
            )
            update_followed_trade_status(
                conn_use,
                wallet=wallet,
                transaction_hash=str(tracked["transaction_hash"]),
                status=str(status["status"]),
                status_label=str(status["status_label"]),
                status_reason=str(status["status_reason"]),
                is_closed=bool(status["is_closed"]),
                is_win=status["is_win"],
                exited_early=status["exited_early"],
                live_price=status["live_price"],
                exit_price=status["exit_price"],
                current_value=_safe_float(status["current_value"]),
                realized_pnl=_safe_float(status["realized_pnl"]),
                close_ts=status["close_ts"],
                event_end_ts=status["event_end_ts"],
                last_synced_ts=now_ts,
            )
        except Exception:
            logging.exception(
                "followed trade sync row failed wallet=%s tx=%s",
                wallet,
                str(tracked.get("transaction_hash") or ""),
            )


def sync_followed_trades(
    *,
    config: SyncConfig,
    force: bool = False,
    conn_obj=None,
    http_session=None,
) -> None:
    conn_use = conn_obj or get_connection()
    if force:
        wallets = sorted({str(row["wallet"] or "") for row in followed_trades(conn_use, limit=config.max_followed_ui_trades)})
    else:
        wallets = followed_trade_wallets_for_sync(conn_use, config.followed_trade_sync_min_interval_seconds)
        if config.followed_max_sync_wallets_per_request > 0:
            wallets = wallets[: config.followed_max_sync_wallets_per_request]
    for wallet in wallets:
        sync_followed_wallet(wallet, force=force, config=config, conn_obj=conn_use, http_session=http_session)


def sync_followed_positions_wallet(
    wallet: str,
    *,
    config: SyncConfig,
    force: bool = False,
    conn_obj=None,
    http_session=None,
    auto_scope_wallets: set[str] | None = None,
) -> None:
    conn_use = conn_obj or get_connection()
    session = http_session or build_session()
    wallet = _normalize_wallet(wallet)
    if not _wallet_valid(wallet):
        return
    if wallet in _ignored_wallets(conn_use):
        return

    auto_scope = (
        {str(row or "").strip().lower() for row in auto_scope_wallets}
        if auto_scope_wallets is not None
        else {str(row or "").strip().lower() for row in auto_follow_scope_wallets(conn_use)}
    )
    tracked_rows = followed_positions(
        conn_use,
        wallet=wallet,
        include_auto=True,
        limit=config.max_followed_position_ui_rows,
    )
    if not tracked_rows and wallet not in auto_scope:
        return

    now_ts = int(dt.datetime.now(dt.UTC).timestamp())
    if not force and tracked_rows:
        max_synced = max((_safe_int(row["last_synced_ts"]) for row in tracked_rows), default=0)
        if now_ts - max_synced < config.followed_position_sync_min_interval_seconds:
            return

    try:
        open_rows = _unwrap_positions(fetch_user_positions(session, wallet, endpoint="positions"))
    except Exception:
        return

    open_payload: List[Dict[str, Any]] = []
    for row in open_rows:
        if is_excluded_position_record(row):
            continue
        position_value = _safe_float(row.get("currentValue"))
        if position_value < config.position_min_open_value:
            continue
        condition_id = str(row.get("conditionId") or "").strip().lower()
        outcome = str(row.get("outcome") or "").strip().lower()
        if not condition_id:
            continue
        current_size = _safe_float(row.get("size"))
        entry_price = _safe_float(row.get("avgPrice"))
        entry_notional = _derive_entry_notional(row, entry_price, current_size)
        current_price = _safe_float(row.get("curPrice"))
        if current_price <= 0 and current_size > 0 and position_value > 0:
            current_price = position_value / current_size
        open_payload.append(
            {
                "condition_id": condition_id,
                "outcome": outcome,
                "title": str(row.get("title") or row.get("question") or ""),
                "event_slug": str(row.get("eventSlug") or row.get("event_slug") or row.get("slug") or ""),
                "entry_price": float(entry_price),
                "current_price": float(current_price),
                "entry_notional": float(entry_notional),
                "current_amount": float(position_value),
                "current_size": float(current_size),
            }
        )

    if wallet in auto_scope:
        sync_auto_followed_positions_for_wallet(
            conn_use,
            wallet=wallet,
            open_positions=open_payload,
            synced_ts=now_ts,
        )

    prune_followed_positions_for_wallet(
        conn_use,
        wallet=wallet,
        open_positions=open_payload,
    )

    for position in open_payload:
        condition_id = str(position.get("condition_id") or "").strip().lower()
        outcome = str(position.get("outcome") or "").strip().lower()
        if not condition_id:
            continue
        title = str(position.get("title") or "")
        event_slug = str(position.get("event_slug") or "")
        entry_price = _safe_float(position.get("entry_price"))
        current_price = _safe_float(position.get("current_price"))
        entry_notional = _safe_float(position.get("entry_notional"))
        current_amount = _safe_float(position.get("current_amount"))
        current_size = _safe_float(position.get("current_size"))
        conn_use.execute(
            """
            UPDATE followed_positions
            SET
                title = CASE WHEN ? <> '' THEN ? ELSE title END,
                event_slug = CASE WHEN ? <> '' THEN ? ELSE event_slug END,
                entry_price = CASE WHEN ? > 0 THEN ? ELSE entry_price END,
                current_price = CASE WHEN ? > 0 THEN ? ELSE current_price END,
                entry_notional = CASE WHEN ? > 0 THEN ? ELSE entry_notional END,
                current_amount = CASE WHEN ? > 0 THEN ? ELSE current_amount END,
                current_size = CASE WHEN ? > 0 THEN ? ELSE current_size END,
                last_synced_ts = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE wallet = ? AND condition_id = ? AND outcome = ?
            """,
            (
                title,
                title,
                event_slug,
                event_slug,
                entry_price,
                entry_price,
                current_price,
                current_price,
                entry_notional,
                entry_notional,
                current_amount,
                current_amount,
                current_size,
                current_size,
                now_ts,
                wallet,
                condition_id,
                outcome,
            ),
        )

    conn_use.execute(
        """
        UPDATE followed_positions
        SET last_synced_ts = ?, updated_at = CURRENT_TIMESTAMP
        WHERE wallet = ?
        """,
        (now_ts, wallet),
    )
    conn_use.commit()


def sync_followed_positions(
    *,
    config: SyncConfig,
    force: bool = False,
    conn_obj=None,
    http_session=None,
) -> None:
    conn_use = conn_obj or get_connection()
    auto_scope = {str(row or "").strip().lower() for row in auto_follow_scope_wallets(conn_use)}
    wallet_last_synced: Dict[str, int] = {}
    wallets = set(auto_scope)
    for row in followed_positions(conn_use, include_auto=True, limit=config.max_followed_position_ui_rows * 4):
        wallet = _normalize_wallet(row["wallet"])
        if wallet:
            wallets.add(wallet)
            wallet_last_synced[wallet] = max(wallet_last_synced.get(wallet, 0), _safe_int(row["last_synced_ts"]))

    ordered_wallets = sorted(wallets)
    if not force and config.followed_position_max_sync_wallets_per_request > 0:
        now_ts = int(dt.datetime.now(dt.UTC).timestamp())
        stale_wallets = [
            wallet
            for wallet in ordered_wallets
            if now_ts - wallet_last_synced.get(wallet, 0) >= config.followed_position_sync_min_interval_seconds
        ]
        stale_wallets.sort(key=lambda wallet: wallet_last_synced.get(wallet, 0))
        ordered_wallets = stale_wallets[: config.followed_position_max_sync_wallets_per_request]

    for wallet in ordered_wallets:
        sync_followed_positions_wallet(
            wallet,
            force=force,
            config=config,
            conn_obj=conn_use,
            http_session=http_session,
            auto_scope_wallets=auto_scope,
        )


def background_sync_watchlist(wallet: str | None = None, *, config: SyncConfig) -> None:
    bg_conn = get_connection()
    bg_http = build_session()
    try:
        if wallet:
            sync_watchlist_wallet(wallet, force=True, config=config, conn_obj=bg_conn, http_session=bg_http)
        else:
            sync_all_watchlist_wallets(force=True, config=config, conn_obj=bg_conn, http_session=bg_http)
    finally:
        try:
            bg_http.close()
        except Exception:
            pass
        bg_conn.close()


def background_sync_followed_trades(wallet: str | None = None, *, config: SyncConfig) -> None:
    bg_conn = get_connection()
    bg_http = build_session()
    try:
        if wallet:
            sync_followed_wallet(wallet, force=True, config=config, conn_obj=bg_conn, http_session=bg_http)
        else:
            sync_followed_trades(force=True, config=config, conn_obj=bg_conn, http_session=bg_http)
    finally:
        try:
            bg_http.close()
        except Exception:
            pass
        bg_conn.close()


def background_sync_followed_positions(wallet: str | None = None, *, config: SyncConfig) -> None:
    bg_conn = get_connection()
    bg_http = build_session()
    try:
        if wallet:
            sync_followed_positions_wallet(wallet, force=True, config=config, conn_obj=bg_conn, http_session=bg_http)
        else:
            sync_followed_positions(force=True, config=config, conn_obj=bg_conn, http_session=bg_http)
    finally:
        try:
            bg_http.close()
        except Exception:
            pass
        bg_conn.close()


# ---------------------------------------------------------------------------
# External leaderboard sync (V2)
# ---------------------------------------------------------------------------

_EXT_LB_CATEGORIES = [
    "OVERALL", "POLITICS", "SPORTS", "CRYPTO",
    "CULTURE", "ECONOMICS", "TECH", "FINANCE",
]
_EXT_LB_TIME_PERIODS = ["ALL", "MONTH", "WEEK", "DAY"]
_EXT_LB_ORDER_BYS = ["PNL", "VOL"]

logger = logging.getLogger(__name__)


def sync_external_leaderboard(
    *,
    config: SyncConfig,
    conn_obj: Any = None,
    http_session: Any = None,
) -> int:
    """Fetch all official leaderboard combos and upsert into DB.

    Returns total number of wallet rows upserted.
    """
    from db import get_connection as _get_conn, upsert_external_leaderboard_entry
    from db_discovery import enqueue_wallets_for_analysis
    from sniper.markets.polymarket_api import build_session as _build, fetch_leaderboard_rankings
    import time as _time

    conn = conn_obj or _get_conn()
    http = http_session or _build()
    now_ts = int(_time.time())
    total_upserted = 0
    all_wallets: set[str] = set()

    try:
        for cat in _EXT_LB_CATEGORIES:
            for tp in _EXT_LB_TIME_PERIODS:
                for ob in _EXT_LB_ORDER_BYS:
                    try:
                        rows = fetch_leaderboard_rankings(
                            http,
                            category=cat,
                            time_period=tp,
                            order_by=ob,
                            limit=50,
                        )
                    except Exception:
                        logger.exception(
                            "Failed to fetch leaderboard cat=%s tp=%s ob=%s", cat, tp, ob
                        )
                        continue

                    for row in rows:
                        w = row.get("wallet", "")
                        if not w:
                            continue
                        upsert_external_leaderboard_entry(
                            conn,
                            wallet=w,
                            category=cat,
                            time_period=tp,
                            order_by=ob,
                            rank=row.get("rank", 0),
                            official_pnl=row.get("official_pnl", 0),
                            official_volume=row.get("official_volume", 0),
                            username=row.get("username", ""),
                            profile_image=row.get("profile_image", ""),
                            x_username=row.get("x_username", ""),
                            verified_badge=row.get("verified_badge", False),
                            fetched_at=now_ts,
                        )
                        all_wallets.add(w)
                        total_upserted += 1

                    conn.commit()

        # Enqueue discovered wallets for analysis
        if all_wallets:
            enqueue_wallets_for_analysis(
                conn,
                list(all_wallets),
                priority_score=1100,
                now_ts=now_ts,
                commit=True,
            )
            logger.info(
                "External leaderboard sync: upserted=%d wallets_discovered=%d",
                total_upserted,
                len(all_wallets),
            )
    finally:
        if conn_obj is None:
            conn.close()
        if http_session is None:
            try:
                http.close()
            except Exception:
                pass

    return total_upserted


def background_sync_external_leaderboard(*, config: SyncConfig) -> None:
    bg_conn = get_connection()
    bg_http = build_session()
    try:
        sync_external_leaderboard(config=config, conn_obj=bg_conn, http_session=bg_http)
    finally:
        try:
            bg_http.close()
        except Exception:
            pass
        bg_conn.close()
