"""Tick-tape persistence — its own SQLite file, deliberately OUTSIDE sniper_tracker.db.

The swap tape is a firehose relative to the trading ledger (whole-chain on 4663); keeping it in a
separate file keeps the money path's WAL, backups, and vacuum behavior untouched. Spec:
docs/superpowers/specs/2026-07-23-memecoin-tick-stream-design.md.

Sign convention: amount0/amount1 are SIGNED atomic amounts stored as TEXT (int256-safe),
positive = tokens INTO the pool (V2 rows are normalized in−out to match V3's convention).
`v2_sync` rows carry the post-event reserves in amount0/amount1 (always positive).
"""
from __future__ import annotations

import os
import sqlite3

DEFAULT_TICKS_DB = "data/memecoin_ticks.db"
DEFAULT_SOL_TAPE_DB = "data/memecoin_sol_tape.db"


def ticks_db_path():
    """Anchored at the STATE root, never the process CWD — the pinned runner's CWD is its
    release dir, which is discarded on the next deploy (v0 launch wrote 4.6k rows there)."""
    override = os.getenv("MEMECOIN_TICKS_DB")
    if override:
        return override
    from sniper.util.state_paths import state_root
    return str(state_root() / DEFAULT_TICKS_DB)


def connect_ticks(path=None):
    c = sqlite3.connect(path or ticks_db_path(), timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA journal_size_limit=16777216")  # trim the WAL after a checkpoint reset
    ensure_tick_tables(c)
    ensure_block_times(c)
    return c


def connect_ticks_readonly(path=None):
    """Read-only handle on the shared ticks DB (own-DB split, P0-3 2026-07-24): a consumer that only
    ever needs to READ pool_ticks/pool_meta/block_times (the exit-state worker) must not be able to
    write this file even by mistake — the funded runner is its sole writer, and a second writer here
    already caused a WAL-lock incident once (see `sol_tape_db_path` above). SQLite's `mode=ro` URI
    makes any write attempt raise, structurally, rather than by discipline. No `ensure_*` calls here
    on purpose: those are writes, and the funded runner already guarantees the tables exist."""
    c = sqlite3.connect(f"file:{path or ticks_db_path()}?mode=ro", uri=True, timeout=30,
                        isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA query_only=1")
    return c


def sol_tape_db_path():
    """Own file for the Solana pull tape — NEVER the shared funded tick tape. The exit-state
    worker's Solana pass and the funded EVM tickstream used to write pool_ticks/block_times into
    the same `data/memecoin_ticks.db`, a two-writer WAL lock incident that got the worker disabled
    (2026-07-24). Same STATE-root anchoring as `ticks_db_path()`."""
    override = os.getenv("MEMECOIN_SOL_TAPE_DB")
    if override:
        return override
    from sniper.util.state_paths import state_root
    return str(state_root() / DEFAULT_SOL_TAPE_DB)


def connect_sol_tape(path=None):
    """Solana tape's ONLY connection — pool_ticks, block_times, sol_sig_cursor, pool_meta all live
    here. `ensure_feature_tables` (not just the tick tables) is reused because pool_meta — the
    vault->mint resolution cache the Solana tape reads/writes every pass — is defined there, not in
    this module; the ticks DB got it from a separate cron, which this standalone file has no
    equivalent of, so it must be ensured at connect time."""
    c = sqlite3.connect(path or sol_tape_db_path(), timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA journal_size_limit=16777216")  # trim the WAL after a checkpoint reset
    ensure_tick_tables(c)
    ensure_block_times(c)
    from sniper.analysis.tick_features import ensure_feature_tables
    ensure_feature_tables(c)
    return c


def connect_sol_tape_readonly(path=None):
    """Read-only consumer handle for the independently-written Solana tape."""
    c = sqlite3.connect(f"file:{path or sol_tape_db_path()}?mode=ro", uri=True, timeout=30,
                        isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA query_only=1")
    return c


def ensure_tick_tables(c):
    c.execute("""
        CREATE TABLE IF NOT EXISTS pool_ticks (
            id INTEGER PRIMARY KEY,
            chain_id TEXT NOT NULL,
            pool TEXT NOT NULL,
            kind TEXT NOT NULL,            -- v2_swap|v3_swap|v2_sync|v2_mint|v2_burn|v3_mint|v3_burn|v3_collect|sol_*
            block_no INTEGER,              -- EVM block number / Solana slot
            tx_sig TEXT,
            log_index INTEGER,
            maker TEXT,
            amount0 TEXT,
            amount1 TEXT,
            observed_at TEXT NOT NULL,
            -- WO-10 Uniswap-V3 depth (all nullable -> pre-V3 rows read byte-equivalent). tick =
            -- current tick on a v3_swap; tick_lower/tick_upper = a v3_mint/v3_burn's indexed range;
            -- liquidity = in-range liquidity (v3_swap) or removed/added liquidity (v3_burn/v3_mint);
            -- sqrt_price_x96 = post-swap price; block_hash = reorg/provenance anchor for the hot lane.
            tick INTEGER,
            tick_lower INTEGER,
            tick_upper INTEGER,
            liquidity TEXT,
            sqrt_price_x96 TEXT,
            block_hash TEXT,
            UNIQUE(chain_id, tx_sig, log_index, kind)
        )""")
    # Migrate pre-WO-10 tapes in place: add each column only if absent (old rows keep NULL).
    existing = {r[1] for r in c.execute("PRAGMA table_info(pool_ticks)")}
    for col, decl in (("tick", "INTEGER"), ("tick_lower", "INTEGER"), ("tick_upper", "INTEGER"),
                      ("liquidity", "TEXT"), ("sqrt_price_x96", "TEXT"), ("block_hash", "TEXT")):
        if col not in existing:
            c.execute(f"ALTER TABLE pool_ticks ADD COLUMN {col} {decl}")
    c.execute("CREATE INDEX IF NOT EXISTS idx_pool_ticks_pool "
              "ON pool_ticks(chain_id, pool, block_no)")
    # Time-ordered lane. idx_pool_ticks_pool leads with (chain_id, pool), so every by-TIME query
    # degrades to a full scan of the chain's rows: on the live 4663 tape (19.9M rows / 10GB)
    # `SELECT MAX(observed_at) WHERE chain_id=?` measured 11.9s, and the exit-state materializer
    # runs it every 30s against the same file the tape appends to every 3s. This index turns that
    # into a single seek and bounds market_breadth's window scan to the window instead of the chain.
    c.execute("CREATE INDEX IF NOT EXISTS idx_pool_ticks_chain_time "
              "ON pool_ticks(chain_id, observed_at)")
    # Per-pool time lane. The two indexes above cover (chain_id, pool, block_no) and
    # (chain_id, observed_at), so a query that pins a pool AND a time range can seek neither:
    # SQLite chose idx_pool_ticks_chain_time and re-checked `pool=?` per row. `kind` sits before
    # `observed_at` because compute_pool_windows' reserve read is
    # `kind='v2_sync' ... ORDER BY observed_at DESC LIMIT 1`, and a V3 pool holds NO v2_sync row,
    # so without `kind` in the index that read walked the pool's whole history to find nothing.
    # Measured on the live 4663 tape (15GB): one pool's three reads went 10,734ms -> 1.9ms, and
    # the v2_sync read alone 777ms -> 0.01ms. tick_feature_job runs it for 200 pools x 2 windows,
    # so the job could not finish inside its own 5-minute cron interval (2026-08-12).
    c.execute("CREATE INDEX IF NOT EXISTS idx_pool_ticks_pool_kind_time "
              "ON pool_ticks(chain_id, pool, kind, observed_at)")
    c.execute("""
        CREATE TABLE IF NOT EXISTS tick_cursors (
            chain_id TEXT PRIMARY KEY,
            last_block INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        )""")
    c.execute("""
        CREATE TABLE IF NOT EXISTS tick_gaps (
            id INTEGER PRIMARY KEY,
            chain_id TEXT NOT NULL,
            from_block INTEGER NOT NULL,
            to_block INTEGER NOT NULL,
            reason TEXT NOT NULL,
            detected_at TEXT NOT NULL,
            resolved_at TEXT,
            CHECK (to_block >= from_block),
            UNIQUE(chain_id, from_block, to_block, reason)
        )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_tick_gaps_open "
              "ON tick_gaps(chain_id,resolved_at,from_block)")
    c.commit()


def insert_ticks(c, rows):
    """Idempotent append (UNIQUE dedupes re-scans). Returns inserted count."""
    if not rows:
        return 0
    before = c.total_changes
    c.executemany(
        "INSERT OR IGNORE INTO pool_ticks (chain_id, pool, kind, block_no, tx_sig, log_index,"
        " maker, amount0, amount1, observed_at, tick, tick_lower, tick_upper, liquidity,"
        " sqrt_price_x96, block_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(r["chain_id"], r["pool"], r["kind"], r.get("block_no"), r.get("tx_sig"),
          r.get("log_index"), r.get("maker"), r.get("amount0"), r.get("amount1"),
          r["observed_at"], r.get("tick"), r.get("tick_lower"), r.get("tick_upper"),
          r.get("liquidity"), r.get("sqrt_price_x96"), r.get("block_hash")) for r in rows])
    c.commit()
    return c.total_changes - before


def ensure_block_times(c):
    """Immutable block->time map for event-time feature windows (exit brain, 2026-07-23).

    The tape stores block_no + INGEST-time observed_at, not block timestamps. Time-based features
    window on true block_ts from this table, backfilled forward-only via bounded eth_getBlockByNumber.
    A block absent here is DATA_INSUFFICIENT for the window — never converted with an assumed constant.
    """
    c.execute("""
        CREATE TABLE IF NOT EXISTS block_times (
            chain_id TEXT NOT NULL,
            block_no INTEGER NOT NULL,
            block_ts TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            -- The CANONICAL hash for this height, so a sum can be taken over verified identity
            -- rather than over a height. A reorg replaces this row, and a tick still carrying the
            -- replaced hash stops matching instead of being counted twice (2026-09-23 review, F1).
            -- NULL on every row written before the column existed: unverified, never "no swaps".
            block_hash TEXT,
            PRIMARY KEY (chain_id, block_no)
        )""")
    if "block_hash" not in {r[1] for r in c.execute("PRAGMA table_info(block_times)")}:
        c.execute("ALTER TABLE block_times ADD COLUMN block_hash TEXT")
    c.commit()


def backfill_block_times(c, chain_id, blocks, get_block_fn, now_iso, *, hash_fn=None, header_fn=None):
    """Insert times from `get_block_fn(height) -> unix_ts | None`.

    `header_fn(height) -> (unix_ts, hash) | None` supplies ONE header/log observation instead.
    Only that pair may enrich a legacy
    NULL-hash row, replacing its time and identity together: the old time may belong to a fork.
    A known hash is never replaced here. Scalar times and legacy `hash_fn` remain supported for
    new rows, but cannot establish a legacy row's identity. Returns inserted rows only.
    """
    from datetime import datetime, timezone
    ensure_block_times(c)
    want = sorted({int(b) for b in blocks})
    if not want:
        return 0
    q = ",".join("?" for _ in want)
    # THE HASH OF EACH HEIGHT WE ALREADY HOLD, not merely whether we hold it (N3, 2026-09-23).
    # Skipping an existing height BEFORE consulting `hash_fn` meant a row written before the
    # `block_hash` column existed — or migrated in with NULL — could never acquire an identity, and
    # every tick in that block read `hash_unverified` forever.
    have = {int(r[0]): r[1] for r in c.execute(
        f"SELECT block_no, block_hash FROM block_times WHERE chain_id=? AND block_no IN ({q})",
        (str(chain_id), *want))}

    def _offered(block):
        """The caller's hash for this height, lowercased, or None. Never raises."""
        if hash_fn is None:
            return None
        try:
            value = hash_fn(block)
        except Exception:
            return None
        return str(value).lower() if value else None

    rows, enrich = [], []
    for b in want:
        if b in have and (have[b] is not None or header_fn is None):
            continue
        try:
            value = header_fn(b) if header_fn is not None else get_block_fn(b)
        except Exception:
            continue
        paired = header_fn is not None and isinstance(value, tuple) and len(value) == 2
        if header_fn is not None and not paired:
            continue
        ts, block_hash = value if paired else (value, _offered(b))
        if ts is None:
            continue                                    # fail-closed: no header -> no row
        try:
            iso = datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
        except (TypeError, ValueError, OverflowError, OSError):
            continue
        block_hash = str(block_hash).lower() if block_hash else None
        if b in have:
            if paired and block_hash:
                enrich.append((iso, now_iso, block_hash, str(chain_id), b))
        else:
            rows.append((str(chain_id), b, iso, now_iso, block_hash))
    if enrich:
        c.executemany("UPDATE block_times SET block_ts=?, fetched_at=?, block_hash=? "
                      "WHERE chain_id=? AND block_no=? AND block_hash IS NULL", enrich)
    if rows:
        c.executemany(
            "INSERT INTO block_times (chain_id, block_no, block_ts, fetched_at, block_hash) "
            "VALUES (?,?,?,?,?) ON CONFLICT(chain_id, block_no) DO NOTHING", rows)
    if rows or enrich:
        c.commit()
    return len(rows)


def block_ts_map(c, chain_id, blocks):
    """{block_no: block_ts} for the KNOWN blocks among `blocks`. Missing blocks are simply absent —
    the caller treats an incomplete map as DATA_INSUFFICIENT for that window."""
    want = sorted({int(b) for b in blocks})
    if not want:
        return {}
    q = ",".join("?" for _ in want)
    return {r[0]: r[1] for r in c.execute(
        f"SELECT block_no, block_ts FROM block_times WHERE chain_id=? AND block_no IN ({q})",
        (str(chain_id), *want))}


def get_cursor(c, chain_id):
    row = c.execute("SELECT last_block FROM tick_cursors WHERE chain_id=?", (chain_id,)).fetchone()
    return row["last_block"] if row else None


def set_cursor(c, chain_id, last_block, now_iso):
    c.execute("INSERT INTO tick_cursors (chain_id, last_block, updated_at) VALUES (?,?,?) "
              "ON CONFLICT(chain_id) DO UPDATE SET last_block=excluded.last_block, "
              "updated_at=excluded.updated_at", (chain_id, int(last_block), now_iso))
    c.commit()


def record_gap(c, chain_id, from_block, to_block, reason, detected_at):
    """Persist an immutable skipped range. Returns True only for a new gap identity."""
    start, end = int(from_block), int(to_block)
    if start < 0 or end < start or not str(reason).strip():
        raise ValueError("invalid tick gap")
    before = c.total_changes
    c.execute(
        "INSERT OR IGNORE INTO tick_gaps "
        "(chain_id,from_block,to_block,reason,detected_at) VALUES(?,?,?,?,?)",
        (str(chain_id), start, end, str(reason), detected_at))
    c.commit()
    return c.total_changes > before


def unresolved_gaps(c, chain_id):
    return c.execute(
        "SELECT * FROM tick_gaps WHERE chain_id=? AND resolved_at IS NULL "
        "ORDER BY from_block", (str(chain_id),)).fetchall()


def prune_unlinked(c, *, keep_pools, older_than_iso):
    """Retention: drop rows for pools NOT in keep_pools older than the horizon. Position-linked
    tape is preserved by passing its pools in keep_pools. Returns deleted count."""
    keep = {str(p).lower() for p in keep_pools}
    before = c.total_changes
    if keep:
        marks = ",".join("?" for _ in keep)
        c.execute(f"DELETE FROM pool_ticks WHERE observed_at < ? "
                  f"AND lower(pool) NOT IN ({marks})", (older_than_iso, *keep))
    else:
        c.execute("DELETE FROM pool_ticks WHERE observed_at < ?", (older_than_iso,))
    c.commit()
    return c.total_changes - before
