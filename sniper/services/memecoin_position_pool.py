"""Persisted fill-anchored pool per funded position.

`resolve_pool_for_position` is called from at least four sites including the hot exit supervisor
and the exit-state worker. Putting a network call there would put I/O in the funded exit decision
path, and worse: a transient RPC failure would return None, which the caller reads as "no usable
pool" and converts into an exit refusal indistinguishable from a genuine ambiguity afterwards. That
is the inert-gate shape in a new coat.

So the receipt is read ONCE by a resolver worker, persisted here, and the exit path does a pure
read of the stored value.

Keyed by POSITION, never by mint. The whole reason this exists is that the fill knows which pool
was traded and the mint does not; writing a fill-derived answer into mint-keyed `pool_meta` would
give that column two writers with different semantics, which is the trap that cost hours on
`source_drift_pct`.

`resolution_tx` and `resolution_log_index` are stored alongside so the derivation is re-checkable
from the receipt later. A bare address nobody can re-derive is not evidence.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("memecoin")

_V4_LEN = 66      # 32-byte pool id
_EVM_LEN = 42     # 20-byte pool address


def _venue_kind(pool):
    if len(pool) == _V4_LEN:
        return "v4"
    if len(pool) == _EVM_LEN:
        return "v2v3"
    return None


def _clean(pool):
    """Lowercased pool if it is a shape we can classify, else None. A shape we cannot classify is
    not stored — an unrecognised value here would be read later as a pool."""
    p = str(pool or "").lower()
    if not p.startswith("0x") or _venue_kind(p) is None:
        return None
    try:
        int(p[2:], 16)
    except ValueError:
        return None
    return p


def ensure_position_pool_table(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS evm_position_pools ("
        "position_id INTEGER NOT NULL, chain_id TEXT NOT NULL, mint TEXT NOT NULL, "
        "pool TEXT NOT NULL, venue_kind TEXT NOT NULL, resolution_tx TEXT, "
        "resolution_log_index INTEGER, resolved_at TEXT NOT NULL, "
        "PRIMARY KEY (position_id, chain_id))")
    conn.commit()


def record_position_pool(conn, position_id, chain_id, mint, pool, *, tx, log_index, now_iso):
    """Write the pool this position actually traded. Returns True when it was written.

    IMMUTABLE: a position traded one pool, so an existing row is never overwritten. A second,
    different answer means something upstream is wrong, and silently repointing a live position's
    price source is the worst available response to that.
    """
    clean = _clean(pool)
    if clean is None or position_id is None:
        return False
    try:
        ensure_position_pool_table(conn)
        cur = conn.execute(
            "INSERT OR IGNORE INTO evm_position_pools (position_id, chain_id, mint, pool, "
            "venue_kind, resolution_tx, resolution_log_index, resolved_at) VALUES (?,?,?,?,?,?,?,?)",
            (int(position_id), str(chain_id), str(mint or "").lower(), clean, _venue_kind(clean),
             (str(tx).lower() if tx else None),
             (int(log_index) if log_index is not None else None), now_iso))
        conn.commit()
        return cur.rowcount > 0
    except Exception:
        logger.exception("position pool: write failed for position %s", position_id)
        return False


def get_position_pool(conn, position_id, chain_id):
    """The stored pool, or None. PURE READ — no network, and never raises.

    A missing table, an unresolved position and a read error are all None on purpose: this is
    consulted on the funded exit path, where an exception would abort an exit sweep. None means
    "unknown", which the caller already handles by failing closed.
    """
    if position_id is None:
        return None
    try:
        row = conn.execute(
            "SELECT pool FROM evm_position_pools WHERE position_id=? AND chain_id=?",
            (int(position_id), str(chain_id))).fetchone()
    except Exception:
        return None
    if not row:
        return None
    return str(row[0]).lower()
