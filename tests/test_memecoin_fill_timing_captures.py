"""Zero-read captures for fill timing (spec 2026-10-02-memecoin-fill-timing-design.md rev 2, 2.1): the
source slot/block on the detection, the Solana head slot at our decision, and our landing slot/block
on the confirmation. Each adds a value we already hold to a write or mark that already happens."""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as db

NOW = "2026-10-02T12:00:00+00:00"

# The detections table as it stood before this change (the CREATE in ensure_memecoin_tables plus the
# ALTERed columns, without source_slot): a watcher or runner on new code must still record a buy here.
_OLD_TABLE = """
CREATE TABLE onchain_wallet_detections (
    id INTEGER PRIMARY KEY AUTOINCREMENT, signature TEXT UNIQUE NOT NULL, source_user_id TEXT NOT NULL,
    handle TEXT, address TEXT NOT NULL, network_id TEXT NOT NULL DEFAULT 'solana', mint TEXT NOT NULL,
    token_delta REAL, sol_spent REAL, source_size_usd REAL, source_price REAL, source_valuation_json TEXT,
    block_time INTEGER, detected_at TEXT NOT NULL, detect_latency_s REAL, feed_event_id TEXT,
    feed_lag_vs_onchain_s REAL, created_at TEXT NOT NULL, token_amount_atomic TEXT,
    quote_amount_atomic TEXT, source_observation_key TEXT, source_observation_state TEXT);
"""


def _record(conn, **kw):
    return db.record_onchain_detection(conn, signature="SIG", source_user_id="u", handle="h", address="A",
                                       mint="M", detected_at=NOW, created_at=NOW, **kw)


def test_the_detection_stores_the_source_slot():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(conn)
    assert _record(conn, source_slot=452585024)
    assert conn.execute("SELECT source_slot FROM onchain_wallet_detections").fetchone()[0] == 452585024


def test_a_pre_migration_table_still_records_the_detection():
    """Gamma M2: an INSERT naming a column that is not there yet would drop EVERY detection."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(_OLD_TABLE)
    assert _record(conn, source_slot=452585024)
    assert conn.execute("SELECT signature FROM onchain_wallet_detections").fetchone()[0] == "SIG"


def test_the_solana_watcher_writes_the_tx_slot_and_migrates_an_old_table():
    from scripts.research import onchain_wallet_watch as watcher
    assert "source_slot" in watcher._DETECTION_INSERT
    columns = watcher._DETECTION_INSERT.split("(")[1].split(")")[0].split(",")
    assert len(columns) == watcher._DETECTION_INSERT.count("?")
    wdb = sqlite3.connect(":memory:")
    wdb.executescript(_OLD_TABLE)
    watcher.ensure_detection_columns(wdb)
    watcher.ensure_detection_columns(wdb)                     # idempotent
    assert "source_slot" in {r[1] for r in wdb.execute("PRAGMA table_info(onchain_wallet_detections)")}


def test_the_evm_watcher_writes_the_log_block_number():
    from scripts.research import onchain_evm_wallet_watch as evm
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(conn)
    buy = {"mint": "0x" + "ab" * 20, "token_delta": 1.0, "source_size_usd": 10.0, "source_price": 1.0}
    evm._record(conn, tx_hash="0x" + "1" * 64, wallet="0x" + "cd" * 20, source_user_id="u", handle="h",
                buy=buy, log={"blockNumber": "0x3e8", "blockTimestamp": "0x6a7c2b71"}, now=NOW)
    assert conn.execute("SELECT source_slot FROM onchain_wallet_detections").fetchone()[0] == 1000


def test_the_runner_marks_the_solana_head_at_its_decision(monkeypatch):
    """Spec rev 3: the watcher's logs context.slot IS the tx's own slot, so recv-S is measured from the
    runner's own Solana head (the slot signal's head) when the attempt row is written."""
    from sniper.services import memecoin_slot_signal as sig
    from sniper.services.memecoin_real_runner import _decision_head_fields
    monkeypatch.setattr(sig, "HEAD", {"slot": 452585030, "mono": 100.0})
    assert _decision_head_fields("solana", now_mono=100.25) == {"head_slot": 452585030, "head_age_ms": 250.0}
    assert _decision_head_fields("4663", now_mono=100.25) == {}
    monkeypatch.setattr(sig, "HEAD", {"slot": None, "mono": None})
    assert _decision_head_fields("solana", now_mono=100.25) == {}


# --- landing captures (spec 2.1 item 3, Gamma Q1) ---------------------------------------------------

def _receipt(status="0x1", block="0x3fb"):
    return {"status": status, "blockNumber": block, "logs": []}


def test_eth_confirm_and_receipt_state_carry_the_block_and_stay_plain_strings():
    import json
    from sniper.execution import evm_real_seams as seams
    http = lambda *a, **k: {"result": _receipt()}
    orig = seams._rpc
    try:
        seams._rpc = lambda url, method, params, http=None: {"result": _receipt()}
        st = seams.eth_confirm("0xabc", "https://x.example", max_attempts=1, poll_s=0, sleep=lambda s: None)
        rs = seams.eth_receipt_state("0xabc", "https://x.example")
        seams._rpc = lambda url, method, params, http=None: {"result": _receipt(status="0x0", block="0x10")}
        failed = seams.eth_confirm("0xabc", "https://x.example", max_attempts=1, poll_s=0, sleep=lambda s: None)
        seams._rpc = lambda url, method, params, http=None: {"result": None}
        missing = seams.eth_receipt_state("0xabc", "https://x.example")
    finally:
        seams._rpc = orig
    assert st == "confirmed" and st.block_number == 1019 and rs == "confirmed" and rs.block_number == 1019
    assert failed == "failed" and failed.block_number == 16
    assert missing == "not_found" and getattr(missing, "block_number", None) is None
    conn = sqlite3.connect(":memory:")
    assert conn.execute("SELECT ?", (st,)).fetchone()[0] == "confirmed"
    assert json.dumps({"outcome": st}) == '{"outcome": "confirmed"}' and st in ("confirmed", "failed")


def test_confirm_receipt_across_pool_passes_the_block_through():
    from sniper.execution import evm_real_seams as seams
    orig = seams.eth_receipt_state
    try:
        seams.eth_receipt_state = lambda *a, **k: seams.ReceiptState("confirmed", block_number=77)
        st = seams.confirm_receipt_across_pool("0xabc", "https://x.example", candidates=["https://x.example"])
    finally:
        seams.eth_receipt_state = orig
    assert st == "confirmed" and st.block_number == 77


def test_the_fill_timing_facts_read_a_fast_lane_receipt_mark():
    import json
    from sniper.services import memecoin_fill_timing as ft
    trace = json.dumps({"substage_events": [{"name": "rh_fast_lane_receipt", "block": 1019,
                                             "block_time": 1790932883}]})
    f = ft.facts({"network_id": "4663", "terminal_status": "filled", "trace_json": trace},
                 {"source_slot": 1000, "block_time": 1790932881}, None, None, None, None)
    assert ft.gap_of(f) == 19 and f["gap_s"] == 2.0
