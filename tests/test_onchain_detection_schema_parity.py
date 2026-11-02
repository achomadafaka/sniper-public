"""One table, two creators, one column set (2026-09-29).

`onchain_wallet_detections` is created by the Solana watcher's own SCHEMA
(scripts/research/onchain_wallet_watch.py) and by db_memecoin.ensure_memecoin_tables, which ALTERs in
the later columns. The on-chain lane's hot-path SELECT reads `source_observation_state` (Ink source
retraction); on a table the watcher created, that column did not exist until the runner ran its
ensure, and 29 lane tests failed. The watcher's CREATE now carries every column ensure adds.
"""
import sqlite3

from scripts.research import onchain_wallet_watch as watcher
from sniper.persistence import db_memecoin as db


def _columns(conn):
    return {r[1] for r in conn.execute("PRAGMA table_info(onchain_wallet_detections)")}


def test_the_watcher_schema_has_every_column_the_ledger_ensure_creates():
    ours = sqlite3.connect(":memory:")
    ours.executescript(watcher.SCHEMA)
    ledger = sqlite3.connect(":memory:")
    ledger.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(ledger)
    missing = _columns(ledger) - _columns(ours)
    assert not missing, sorted(missing)


def test_ensure_on_a_watcher_created_table_adds_nothing_and_breaks_nothing():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(watcher.SCHEMA)
    before = _columns(conn)
    db.ensure_memecoin_tables(conn)
    assert _columns(conn) == before
