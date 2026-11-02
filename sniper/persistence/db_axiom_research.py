"""Research account lists do not grant identity proof or funded authority."""
from __future__ import annotations

from sniper.feeds.axiom_parse import _valid_address


def ensure_research_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS axiom_research_accounts (
        chain TEXT NOT NULL, address TEXT NOT NULL, handle TEXT,
        active INTEGER NOT NULL CHECK(active IN (0,1)), requested_source_user_id TEXT,
        updated_at TEXT NOT NULL, origin TEXT NOT NULL DEFAULT 'manual', PRIMARY KEY(chain,address))""")
    columns = {r[1] for r in conn.execute("PRAGMA table_info(axiom_research_accounts)")}
    if "origin" not in columns:
        conn.execute("ALTER TABLE axiom_research_accounts ADD COLUMN origin TEXT NOT NULL DEFAULT 'manual'")


def _table_exists(conn, table):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _account(conn, chain, address):
    if chain not in ("solana", "evm", "4663", "56", "8453"):
        raise ValueError("unsupported_chain")
    if not _valid_address(address, solana=chain == "solana"):
        raise ValueError("invalid_address")
    if chain != "solana":
        address = address.lower()
    if not _table_exists(conn, "axiom_trader_directory"):
        return address, None, "manual"
    row = conn.execute(
        "SELECT x_handle FROM axiom_trader_directory WHERE chain IN (?,?) "
        "AND CASE WHEN chain='solana' THEN address ELSE lower(address) END=? "
        "ORDER BY seen_at DESC LIMIT 1",
        (chain, "evm" if chain != "solana" else chain, address)).fetchone()
    return address, row[0] if row else None, "axiom_directory" if row else "manual"


def set_research_account(conn, *, chain, address, handle=None, active=True,
                         requested_source_user_id=None, now_iso):
    """Set one research account. The caller owns the transaction."""
    if not isinstance(active, bool):
        raise ValueError("active_must_be_boolean")
    address, handle, origin = _account(conn, chain, address)
    if requested_source_user_id is not None:
        if not isinstance(requested_source_user_id, str) or not conn.execute(
            "SELECT 1 FROM memecoin_wallets WHERE source_user_id=?",
            (requested_source_user_id,)).fetchone():
            raise ValueError("unknown_tracked_account")
    conn.execute(
        "INSERT INTO axiom_research_accounts "
        "(chain,address,handle,active,requested_source_user_id,updated_at,origin) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(chain,address) DO UPDATE SET handle=excluded.handle,active=excluded.active,"
        "requested_source_user_id=excluded.requested_source_user_id,updated_at=excluded.updated_at,origin=excluded.origin",
        (chain, address, handle, int(active), requested_source_user_id if active else None, now_iso, origin))


def swap_research_account(conn, *, old_chain, old_address, new_chain, new_address,
                          handle=None, now_iso):
    """Replace an active research account. The caller owns the transaction."""
    old_address, _, _ = _account(conn, old_chain, old_address)
    new_address, handle, _ = _account(conn, new_chain, new_address)
    if (old_chain, old_address) == (new_chain, new_address):
        raise ValueError("same_account")
    if not conn.execute(
        "SELECT 1 FROM axiom_research_accounts WHERE chain=? AND address=? AND active=1",
        (old_chain, old_address)).fetchone():
        raise ValueError("old_account_not_active")
    set_research_account(conn, chain=new_chain, address=new_address, handle=handle, now_iso=now_iso)
    conn.execute(
        "UPDATE axiom_research_accounts SET active=0,requested_source_user_id=NULL,updated_at=? "
        "WHERE chain=? AND address=?", (now_iso, old_chain, old_address))


def _rows(conn, query, args=()):
    cursor = conn.execute(query, args)
    keys = [column[0] for column in cursor.description]
    return [dict(zip(keys, row)) for row in cursor.fetchall()]


def list_research_accounts(conn):
    if not _table_exists(conn, "axiom_research_accounts"):
        return []
    return _rows(conn, "SELECT * FROM axiom_research_accounts ORDER BY active DESC,updated_at DESC")


def research_snapshot(conn, limit=100):
    """Read bounded source observations without a provider request."""
    limit = max(1, min(int(limit), 200))
    result = {"accounts": list_research_accounts(conn)}
    queries = {
        "directory": ("axiom_trader_directory", "ORDER BY pnl_24h IS NULL,pnl_24h DESC,seen_at DESC"),
        "callouts": ("axiom_kol_callouts", "ORDER BY seen_at DESC,id DESC"),
        "events": ("axiom_wallet_events", "ORDER BY received_at DESC,id DESC"),
        "discovery": ("axiom_discovery_tokens", "ORDER BY seen_at DESC,id DESC"),
        "proofs": ("axiom_verified_addresses", "ORDER BY verified_at DESC"),
    }
    for key, (table, order) in queries.items():
        result[key] = _rows(conn, f"SELECT * FROM {table} {order} LIMIT ?", (limit,)) if _table_exists(conn, table) else []
    result["tracked_accounts"] = _rows(conn,
        "SELECT source_user_id,handle FROM memecoin_wallets ORDER BY handle")
    for event in result["events"]:
        event["observation_kind"] = event.get("observation_kind") or "unknown"
    for token in result["discovery"]:
        for key in ("mcap_unit", "liquidity_unit", "volume_unit", "volume_window_s", "parser_version"):
            token.setdefault(key, None)
    return result
