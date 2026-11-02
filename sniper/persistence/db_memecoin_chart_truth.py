"""Chart price at the SOURCE TRADER'S buy minute — measurement only.

Why this exists: no record holds the chart price at the moment the copied trader bought.
`memecoin_entry_attempts.source_price` is his FILL and carries his market impact,
`memecoin_signal_outcomes_v2.ref_price` is a COPY of `their_price` (ratio 1.000 at p10/median/p90
over n=5,000), `memecoin_events.wouldbe_price` is another route quote on 1.1% of events, and
`display_price` is read 15-30s too late at OUR decision. Design:
`docs/superpowers/specs/2026-08-27-memecoin-chart-truth-at-source-buy-design.md`.

THE KEY CARRIES NO MEASUREMENT. `(network_id, pool_address, buy_minute)` are all stable
identifiers; the prices are the measurement. A revised candle UPDATES the row and bumps
`revision_count` — it never inserts a second one. The 2026-08-26 watch sweep keyed its repage
window on a whole message, every message embedded a drifting number, and one continuous problem
paged the operator every 15 minutes. A value that changes cannot be an identity.

`price_space` is a constant column on purpose: a reader must never compare this price to a route
price without the label.
"""
from __future__ import annotations

import sqlite3

from sniper.util.state_paths import state_path

CHART_SPACE = "chart"

# ITS OWN FILE, NOT THE FUNDED LEDGER (Alpha, 2026-08-28; the refusal-captures precedent). A
# measurement lane gets no writer on `sniper_tracker.db` — the write-amplification finding is a day
# old, and an hourly collector has no business adding pages to the ledger the funded runner and the
# web contend on. A consumer that later needs joins ATTACHES this read-only.
# STATE_PATH, NOT `__file__`. A pinned deploy runs from `.runtime/memecoin/releases/<sha>/`, so a
# path derived from this module's location writes the queue INSIDE the release tree — and the next
# deploy points `current` at a new sha whose data directory is empty, stranding every deferred item.
# Forward-only means those events are gone, not delayed. This is the runtime-repoint class, and the
# same reason `MEMECOIN_SOLANA_KEYPAIR` resolves through `state_path`.
DB_PATH = state_path("data", "memecoin_chart_truth.db")


def connect(path=None):
    """Open the chart-truth database, creating its schema."""
    conn = sqlite3.connect(str(path or DB_PATH), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # Without a limit the WAL keeps its high-water mark forever after a checkout resets it —
    # finding-sqlite-wal-highwater-reclaim-2026-07-29, root-caused 2026-08-27.
    conn.execute("PRAGMA journal_size_limit=16777216")
    ensure_tables(conn)
    return conn


def ensure_tables(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS memecoin_chart_truth (
        network_id     TEXT NOT NULL,
        pool_address   TEXT NOT NULL,
        buy_minute     TEXT NOT NULL,
        open_price     REAL,
        high_price     REAL,
        low_price      REAL,
        close_price    REAL,
        volume_usd     REAL,
        price_space    TEXT NOT NULL,
        minute_range   REAL,
        fetched_at     TEXT NOT NULL,
        revision_count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (network_id, pool_address, buy_minute)
    )""")
    # The deferral queue. A yielded cycle must DEFER a pool, never drop it — a busy funded lane and
    # a busy signal tape share a driver, so dropping on yield would lose exactly the busiest tape.
    # Deferral is only safe because the source is historical: a GT page reaches 16.7h back.
    conn.execute("""CREATE TABLE IF NOT EXISTS memecoin_chart_truth_queue (
        network_id   TEXT NOT NULL,
        pool_address TEXT NOT NULL,
        buy_minute   TEXT NOT NULL,
        deadline_at  TEXT NOT NULL,
        enqueued_at  TEXT NOT NULL,
        attempts     INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (network_id, pool_address, buy_minute)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_chart_queue_deadline "
                 "ON memecoin_chart_truth_queue(deadline_at)")
    conn.commit()


def enqueue(conn, rows, *, now_iso, deadline_at) -> int:
    """Add (network, pool, minute) work items. Idempotent on the key."""
    rows = list(rows)
    if not rows:
        return 0
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO memecoin_chart_truth_queue"
        "(network_id, pool_address, buy_minute, deadline_at, enqueued_at) VALUES(?,?,?,?,?)",
        [(n, p, m, deadline_at, now_iso) for n, p, m in rows])
    conn.commit()
    return conn.total_changes - before


def enqueue_with_source_deadlines(conn, rows, *, now_iso, page_minutes) -> int:
    """Each item expires `page_minutes` after ITS OWN buy minute, not after the fetch clock.

    A GeckoTerminal page reaches `page_minutes` back from whenever it is fetched, so the honest
    deadline for a work item is a property of the minute it describes. Deriving it from fetch time
    expired fresh rows up to an hour early.
    """
    from datetime import datetime, timedelta, timezone
    out = []
    for network_id, pool, minute in rows or ():
        try:
            m = datetime.fromisoformat(minute)
        except ValueError:
            continue
        if m.tzinfo is None:
            m = m.replace(tzinfo=timezone.utc)
        deadline = (m + timedelta(minutes=int(page_minutes))).isoformat()
        out.append((network_id, pool, minute, deadline, now_iso))
    if not out:
        return 0
    # COUNT WHAT WAS ACTUALLY INSERTED. `INSERT OR IGNORE` silently drops duplicates, so returning
    # `len(out)` reported every re-seen item as newly queued — a counter that can only ever agree
    # with its input is not a measurement. `total_changes` before/after gives the real number.
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO memecoin_chart_truth_queue"
        "(network_id, pool_address, buy_minute, deadline_at, enqueued_at) VALUES(?,?,?,?,?)", out)
    conn.commit()
    return conn.total_changes - before


def due(conn, *, now_iso, limit):
    """Oldest-deadline-first, so a nearly-expired item is fetched before a fresh one."""
    return [dict(r) for r in conn.execute(
        "SELECT network_id, pool_address, buy_minute, deadline_at, attempts "
        "FROM memecoin_chart_truth_queue WHERE deadline_at > ? "
        "ORDER BY deadline_at ASC LIMIT ?", (now_iso, int(limit)))]


def expire(conn, *, now_iso) -> int:
    """Drop items whose buy minute has aged past the page window. AN AGE-OUT IS REAL DATA LOSS —
    the caller must count and log it, never let it pass silently."""
    n = conn.execute("DELETE FROM memecoin_chart_truth_queue WHERE deadline_at <= ?",
                     (now_iso,)).rowcount
    conn.commit()
    return n


def resolve(conn, network_id, pool_address, buy_minute) -> None:
    conn.execute("DELETE FROM memecoin_chart_truth_queue "
                 "WHERE network_id=? AND pool_address=? AND buy_minute=?",
                 (network_id, pool_address, buy_minute))


def bump_attempt(conn, network_id, pool_address, buy_minute) -> None:
    conn.execute("UPDATE memecoin_chart_truth_queue SET attempts=attempts+1 "
                 "WHERE network_id=? AND pool_address=? AND buy_minute=?",
                 (network_id, pool_address, buy_minute))


def save_candles(conn, network_id, pool_address, candles, *, now_iso) -> int:
    """`candles` maps ISO minute -> (open, high, low, close, volume).

    A revision updates the measurement and bumps `revision_count`; the key never gains a row.
    """
    rows = []
    for minute, (o, h, low, close, vol) in (candles or {}).items():
        rng = (h / low) if (h and low and low > 0) else None
        rows.append((network_id, pool_address, minute, o, h, low, close, vol,
                     CHART_SPACE, rng, now_iso))
    if not rows:
        return 0
    conn.executemany(
        "INSERT INTO memecoin_chart_truth(network_id, pool_address, buy_minute, open_price,"
        " high_price, low_price, close_price, volume_usd, price_space, minute_range, fetched_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(network_id, pool_address, buy_minute) DO UPDATE SET"
        "   open_price=excluded.open_price, high_price=excluded.high_price,"
        "   low_price=excluded.low_price, close_price=excluded.close_price,"
        "   volume_usd=excluded.volume_usd, minute_range=excluded.minute_range,"
        "   fetched_at=excluded.fetched_at,"
        "   revision_count=memecoin_chart_truth.revision_count+1",
        rows)
    conn.commit()
    return len(rows)


def funded_gt_marks_since(conn, since_iso) -> int:
    """The yield signal: how hard the FUNDED mark lane is using the shared GeckoTerminal IP.

    `conn` here is a READ-ONLY handle on the funded ledger, not this lane's own database — the
    signal lives in `memecoin_position_marks` and we must never hold a writable handle on that file
    for a measurement job. `run_once` opens it with `get_readonly_connection`.

    A WINDOWED COUNT, never a cumulative process counter — a counter resets on restart, so one read
    reports hours-since-restart rather than a rate.
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM memecoin_position_marks WHERE source='geckoterminal' AND sweep_ts>=?",
        (since_iso,)).fetchone()
    return int(row[0] if row else 0)
