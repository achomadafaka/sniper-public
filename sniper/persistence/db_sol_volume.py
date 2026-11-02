"""Versioned exact Solana swap proof, stored only in the observation tape DB."""


def ensure_tables(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS sol_volume_coverage_v1 ("
                 "pool TEXT PRIMARY KEY,mint TEXT NOT NULL,venue TEXT NOT NULL,"
                 "market_pool TEXT NOT NULL,quote_mint TEXT NOT NULL,verified_since REAL,"
                 "through REAL,complete INTEGER NOT NULL DEFAULT 0,reason TEXT NOT NULL,"
                 "binding TEXT,pass_id TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS sol_volume_swaps_v1 ("
                 "pool TEXT NOT NULL,signature TEXT NOT NULL,event_index INTEGER NOT NULL,"
                 "block_ts REAL NOT NULL,quote_atomic TEXT NOT NULL,maker TEXT NOT NULL,payer TEXT,"
                 "PRIMARY KEY(pool,signature,event_index))")
    if "payer" not in {row[1] for row in conn.execute("PRAGMA table_info(sol_volume_swaps_v1)")}:
        conn.execute("ALTER TABLE sol_volume_swaps_v1 ADD COLUMN payer TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sol_volume_swaps_v1_window "
                 "ON sol_volume_swaps_v1(pool,block_ts)")
    conn.commit()


def _dicts(cursor):
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor]


def coverage_rows(conn, *, now, limit=256):
    # Invalid rows remain observable; the cache decides whether a complete window is fresh.
    return _dicts(conn.execute("SELECT * FROM sol_volume_coverage_v1 "
                              "ORDER BY through DESC LIMIT ?", (limit,)))


def swap_rows(conn, pool, *, since, through):
    # One overflow row lets the reader refuse an oversized window instead of sampling it.
    return _dicts(conn.execute("SELECT signature,event_index,block_ts,quote_atomic,maker,payer "
                              "FROM sol_volume_swaps_v1 WHERE pool=? AND block_ts>? "
                              "AND block_ts<=? ORDER BY block_ts,event_index LIMIT 4097",
                              (pool, since, through)))
