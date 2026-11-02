"""One-time backfill of network_id for the four chain-ambiguous memecoin tables.

Unambiguous-only (mirrors db_memecoin.resolve_network_from_events): a non-0x mint is Solana-native;
a 0x mint is resolved from the feed's own events (single concrete chain) or LEFT NULL — never
coerced to a default chain. Batched + committed per batch (id watermark) so each write-lock is short
vs the live 15s sweep (WAL: readers never block). Idempotent — safe to re-run.

Usage:
    python3 scripts/research/memecoin_backfill_network_id.py [DB_PATH]

Run behind a fresh SQLite backup (scripts/daily_backup.sh). seen_sells is intentionally NOT
backfilled: it dedups by a globally-unique swap_id and is already chain-safe.
"""
import sqlite3
import sys

sys.path.insert(0, ".")
import sniper.persistence.db_memecoin as m
from sniper.util.clock import real_utc_now_iso

DB = sys.argv[1] if len(sys.argv) > 1 else "sniper_tracker.db"


def main():
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)   # idempotent: guarantees the network_id columns exist
    print(f"[{real_utc_now_iso()}] backfilling network_id in {DB}")
    for name, fn in (("quote_ledger", m.backfill_quote_ledger_network),
                     ("alert_latency", m.backfill_alert_latency_network),
                     ("exec_latency", m.backfill_exec_latency_network)):
        started = real_utc_now_iso()
        stats = fn(conn)
        print(f"[{real_utc_now_iso()}] {name:14s} {stats} (started {started})")
    conn.close()


if __name__ == "__main__":
    main()
