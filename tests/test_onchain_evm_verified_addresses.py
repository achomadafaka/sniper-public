"""The EVM watcher subscribes to PROVEN addresses only, and refuses to start without any.

Before 2026-09-16 the list came from fomo profile pages. Measured against our own funded entries
that address was contradicted on 45 of 50 checks on 4663 and confirmed on none, and the lane has
never written a single 4663 detection.
"""
import sqlite3

import pytest

import scripts.research.onchain_evm_wallet_watch as w
from sniper.persistence.db_axiom import ensure_axiom_tables

NOW = "2026-09-16T12:00:00+00:00"


def _conn(lane_rows=(), legacy_rows=(), armed=()):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_axiom_tables(c)
    c.executescript("""
        CREATE TABLE memecoin_wallets (id INTEGER PRIMARY KEY AUTOINCREMENT, source_user_id TEXT,
            handle TEXT, real_approved INTEGER DEFAULT 0, excluded INTEGER DEFAULT 0,
            paused INTEGER DEFAULT 0, real_chains TEXT);
        CREATE TABLE memecoin_wallet_addresses (source_user_id TEXT PRIMARY KEY, handle TEXT,
            network_id TEXT NOT NULL, address TEXT NOT NULL, resolved_at TEXT NOT NULL,
            method TEXT, coins_held INTEGER, coins_checked INTEGER, verify_status TEXT,
            verify_detail TEXT);
    """)
    for uid, handle, chains in armed:
        c.execute("INSERT INTO memecoin_wallets(source_user_id,handle,real_approved,real_chains) "
                  "VALUES(?,?,1,?)", (uid, handle, chains))
    for uid, handle, chain, address, status in lane_rows:
        c.execute("INSERT INTO axiom_verified_addresses(source_user_id,handle,x_handle,network_id,"
                  "address,verify_status,verified_at) VALUES(?,?,?,?,?,?,?)",
                  (uid, handle, handle, chain, address, status, NOW))
    for uid, handle, chain, address, method, status in legacy_rows:
        c.execute("INSERT INTO memecoin_wallet_addresses(source_user_id,handle,network_id,address,"
                  "resolved_at,method,verify_status) VALUES(?,?,?,?,?,?,?)",
                  (uid, handle, chain, address, NOW, method, status))
    c.execute("UPDATE axiom_verified_addresses SET detail=?",
              ('{"proof_version":"wallet-paid-v2"}',))
    c.commit()
    return c


@pytest.fixture(autouse=True)
def _on_4663(monkeypatch):
    monkeypatch.setattr(w, "CHAIN", "4663")


def test_only_confirmed_addresses_of_armed_wallets_on_this_chain_are_watched():
    conn = _conn(
        lane_rows=[("u1", "alice", "4663", "0xAAA", "axiom_confirmed"),
                   ("u2", "bob", "4663", "0xBBB", "REFUTED"),
                   ("u3", "carol", "solana", "SOLADDR", "axiom_confirmed"),
                   # alice is armed on BOTH chains: her solana address must not be watched here
                   ("u1b", "alice", "solana", "ALICESOL", "axiom_confirmed"),
                   ("u4", "dave", "4663", "0xDDD", "axiom_confirmed")],
        legacy_rows=[("u5", "erin", "4663", "0xEEE", "fomoscan", "fomoscan_confirmed"),
                     ("u6", "frank", "4663", "0xFFF", "axiom_directory", "unverified")],
        armed=[("u1", "alice", "4663,solana"), ("u2", "bob", "4663"), ("u3", "carol", "solana"),
               ("u5", "erin", "4663"), ("u6", "frank", "4663"), ("u1b", "alice", "4663,solana")])
    # u4 is confirmed but NOT armed on this chain, so it is not watched
    assert w._verified_addresses(conn) == {"0xaaa": ("u1", "alice"), "0xeee": ("u5", "erin")}


def test_the_watcher_never_subscribes_to_an_empty_set():
    """It waits rather than exits (see the waiting test below), and address_readiness says so."""
    conn = _conn(lane_rows=[("u1", "alice", "4663", "0xAAA", "unverified")],
                 armed=[("u1", "alice", "4663")])
    assert w._verified_addresses(conn) == {}
    assert w.address_readiness(conn)["status"] == "blocked"


def test_a_proven_set_starts_normally():
    conn = _conn(lane_rows=[("u1", "alice", "4663", "0xAAA", "axiom_confirmed")],
                 armed=[("u1", "alice", "4663")])
    assert w._require_addresses(conn) == {"0xaaa": ("u1", "alice")}


def test_readiness_uses_current_proof_and_chain_authority():
    conn = _conn(lane_rows=[('u1','alice','4663','0xAAA','axiom_confirmed')],
                 armed=[('u1','alice','4663')])
    conn.execute("UPDATE axiom_verified_addresses SET detail='{}'")
    blocked = w.address_readiness(conn)
    assert blocked['status'] == 'blocked'
    assert blocked['reason'] == 'no verified addresses'
    assert blocked['eligible_addresses'] == 0
    conn.execute("UPDATE axiom_verified_addresses SET detail=?", ('{"proof_version":"wallet-paid-v2"}',))
    assert w.address_readiness(conn)['status'] == 'eligible'
    conn.execute("UPDATE memecoin_wallets SET real_chains='56'")
    assert w.address_readiness(conn)['status'] == 'blocked'


def test_the_watcher_no_longer_resolves_addresses_from_fomo():
    """The fomo account is banned and its profile address is refuted; the path is gone, not idle."""
    assert not hasattr(w, "sync_addresses")
    assert not hasattr(w, "_stored_addresses")
    assert not hasattr(w, "get_profile")
    assert not hasattr(w, "FomoAuth")


def test_a_detection_row_carries_this_chain_and_the_columns_the_lane_reads():
    """The stage-2 lane reads network_id, mint, source_size_usd, source_price and detected_at.
    A row missing its chain would be routed as Solana and buy nothing."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE onchain_wallet_detections (
            id INTEGER PRIMARY KEY AUTOINCREMENT, signature TEXT UNIQUE NOT NULL,
            source_user_id TEXT NOT NULL, handle TEXT, address TEXT NOT NULL,
            network_id TEXT NOT NULL DEFAULT 'solana', mint TEXT NOT NULL, token_delta REAL,
            sol_spent REAL, source_size_usd REAL, source_price REAL, block_time INTEGER,
            detected_at TEXT NOT NULL, detect_latency_s REAL, feed_event_id TEXT,
            feed_lag_vs_onchain_s REAL, created_at TEXT NOT NULL);
    """)
    w._record(conn, tx_hash="0xDEADBEEF", wallet="0x38d5a4f4", source_user_id="u1",
              handle="CryoPalmar",
              buy={"mint": "0xcc", "token_delta": 1200.0, "source_size_usd": 412.5,
                   "source_price": 0.34375},
              log={"blockTimestamp": hex(1789560000)}, now="2026-09-16T12:00:10+00:00")
    row = conn.execute("SELECT * FROM onchain_wallet_detections").fetchone()
    assert row["network_id"] == "4663"
    assert row["signature"] == "4663:0xdeadbeef:0xcc"
    assert row["address"] == "0x38d5a4f4" and row["mint"] == "0xcc"
    assert row["token_delta"] == 1200.0 and row["source_size_usd"] == 412.5
    assert row["source_price"] == 0.34375 and row["block_time"] == 1789560000
    assert row["detected_at"] == "2026-09-16T12:00:10+00:00"
    assert row["detect_latency_s"] == 10.0
    assert row["sol_spent"] is None          # a Solana column; an EVM row leaves it unknown


def test_push_refresh_removes_the_last_revoked_address(monkeypatch):
    from sniper.services import memecoin_onchain_push as push
    conn = _conn(lane_rows=[("u1", "alice", "4663", "0xAAA", "axiom_confirmed")],
                 armed=[("u1", "alice", "4663")])
    monkeypatch.setattr(w, "get_connection", lambda _: conn)
    monkeypatch.setattr(w, "_roster_connection", lambda: conn)     # the refresh's own handle
    monkeypatch.setattr(w.m, "ensure_memecoin_tables", lambda _: None)
    observed = []
    def stream(_url, **callbacks):
        observed.append(callbacks["addresses_fn"]())
        conn.execute("UPDATE axiom_verified_addresses SET verify_status='REFUTED'")
        conn.commit()
        observed.append(callbacks["addresses_fn"]())
    monkeypatch.setattr(push, "run_push_stream", stream)
    w.run_push("wss://offline.test")
    assert observed == [["0xaaa"], []]


def test_the_watcher_waits_for_a_proof_instead_of_exiting(monkeypatch):
    """A later eligible proof must start it WITHOUT a deploy, so an empty set sleeps and re-reads.

    It used to raise SystemExit, which the watchdog answered by relaunching every 60 s until
    9c20efd1 stopped launching it at all. Waiting is the shape that recovers by itself.
    """
    conn = _conn(lane_rows=[("u1", "alice", "4663", "0xAAA", "unverified")],
                 armed=[("u1", "alice", "4663")])
    slept, logged = [], []
    states = iter([{}, {}, {"0xaaa": ("u1", "alice")}])
    monkeypatch.setattr(w, "_verified_addresses", lambda _c: next(states))
    watched = w._require_addresses(conn, sleep_fn=slept.append, log_fn=logged.append,
                                   retry_s=30.0)
    assert watched == {"0xaaa": ("u1", "alice")}
    assert slept == [30.0, 30.0]
    assert len(logged) == 1                      # says it once, not once per retry
    assert "no verified address for chain 4663" in logged[0]
