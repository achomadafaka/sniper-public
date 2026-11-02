"""Submitted native buys retain their hop read target even when no position was booked."""
import json
import sqlite3

import pytest

from sniper.persistence import db_core
from sniper.services import memecoin_bsc_fast_lane as bsc
from sniper.services import memecoin_bsc_v2_live as tape

MINT = "0x679fd94f1526282f198ae3ef01ca6e27d5787777"
QUOTE = "0x02fca66c1d1afb4e2a7884261eb00f63598a7436"
POOL = "0xc0e296c0d1525cb3bf847193b8f6f19d97d83eb9"
HOP = "0x217e9ed14c797df4597512d2245c00ca93e5a567"
SOURCE = "0x2d5b7bd975f8b2b0fb01b745a9c943af5fba0dbe15ff0fe04ad2919f3d1022dc"
OUR_TX = "0x89391032f6a5ca8a771ea90178d405f39031968f2588b65752af6d4217a7c120"
OLD_QUOTE = "0x" + "9b" * 20
OLD_HOP = bsc.our_pair(bsc.WBNB, OLD_QUOTE)


@pytest.fixture
def files(tmp_path, monkeypatch):
    ledger_path = tmp_path / "ledger.db"
    with sqlite3.connect(ledger_path) as ledger:
        ledger.execute("CREATE TABLE memecoin_positions (id INTEGER PRIMARY KEY, network_id TEXT, "
                       "execution_mode TEXT, evm_route_json TEXT)")
        ledger.execute("INSERT INTO memecoin_positions VALUES (1,'56','real',?)", (
            json.dumps({"protocol": "pcs_mixed_v3_v2", "base_token": OLD_QUOTE}),))
    monkeypatch.setattr(db_core, "get_readonly_connection", lambda: sqlite3.connect(
        f"file:{ledger_path}?mode=ro", uri=True, timeout=0.1))
    monkeypatch.setattr(tape, "state_root", lambda: tmp_path)
    native_path = tmp_path / "data" / "memecoin_bsc_fast_lane.db"
    native_path.parent.mkdir()
    with sqlite3.connect(native_path) as native:
        native.executescript(bsc._SCHEMA + bsc._LIVE_SCHEMA)
    return native_path


def add_native(path, *, source=SOURCE, our_tx=OUR_TX, mint=MINT, pool=POOL,
               quote=QUOTE, mode="live", event_source=None, event_mint=None,
               include_event=True):
    with sqlite3.connect(path) as native:
        native.execute("INSERT INTO bsc_lane_live (source_tx,our_tx,mint,reason) VALUES (?,?,?,'reverted')",
                       (source, our_tx, mint))
        if include_event:
            native.execute("INSERT INTO bsc_lane_events (tx_hash,mint,pool,quote,mode,reason) "
                           "VALUES (?,?,?,?,?,'reverted')",
                           (source if event_source is None else event_source,
                            mint if event_mint is None else event_mint, pool, quote, mode))


def test_submitted_failed_native_route_is_pinned_without_a_position(files):
    add_native(files)
    assert tape.read_route_pins() == {OLD_HOP: OLD_QUOTE, HOP: QUOTE}


def test_missing_old_native_database_keeps_position_pins_without_creating_it(files):
    files.unlink()
    assert tape.read_route_pins() == {OLD_HOP: OLD_QUOTE}
    assert not files.exists()


def test_events_only_native_database_is_optional_before_the_first_live_attempt(files):
    files.unlink()
    with sqlite3.connect(files) as native:
        native.executescript(bsc._SCHEMA)
    assert tape.read_route_pins() == {OLD_HOP: OLD_QUOTE}


def test_an_inaccessible_native_path_keeps_the_previous_pins(files, monkeypatch):
    class UnreadablePath:
        def __truediv__(self, _part):
            return self

        def exists(self):
            raise PermissionError("native path denied")

        def stat(self):
            raise PermissionError("native path denied")

    monkeypatch.setattr(tape, "state_root", UnreadablePath)
    assert tape.read_route_pins() is None


@pytest.mark.parametrize("change", [
    {"our_tx": None}, {"our_tx": "invalid"}, {"our_tx": "0x" + "0" * 64},
    {"source": "0x" + "0" * 64}, {"source": "0x" + "z" * 64},
    {"mint": "0x" + "0" * 40}, {"mint": "0x" + "g" * 40},
    {"quote": "0x" + "0" * 40}, {"quote": "0x" + "g" * 40},
    {"quote": MINT}, {"quote": bsc.WBNB}, {"pool": "0x" + "1" * 40},
])
def test_invalid_or_non_native_bindings_add_no_hop(files, change):
    add_native(files, **change)
    assert tape.read_route_pins() == {OLD_HOP: OLD_QUOTE}


@pytest.mark.parametrize("change", [
    {"mode": "runner_shadow"}, {"event_source": "0x" + "1" * 64},
    {"event_mint": "0x" + "1" * 40}, {"include_event": False},
])
def test_partial_native_audit_keeps_the_previous_pin_set(files, tmp_path, change):
    add_native(files, **change)
    tape_db = tape.connect(tmp_path / "tape.db")
    try:
        tape.remember_hop_pins(tape_db, {HOP: QUOTE}, 1.0)
        pins = tape.read_route_pins()
        assert pins is None
        assert tape.apply_route_pins(tape_db, pins, 2.0) is False
        assert tape.discovered_hop_pairs(tape_db)[HOP] == QUOTE
    finally:
        tape_db.close()


@pytest.mark.parametrize("failure", ["corrupt", "missing_schema"])
def test_unreadable_native_audit_keeps_the_previous_pin_set(files, failure):
    if failure == "corrupt":
        files.write_bytes(b"not a SQLite database")
    else:
        with sqlite3.connect(files) as native:
            native.execute("DROP TABLE bsc_lane_events")
    assert tape.read_route_pins() is None


def test_only_the_latest_256_submitted_native_rows_are_pinned(files):
    older_quote = "0x" + "8" * 40
    add_native(files, source="0x" + "2" * 64, quote=older_quote,
               pool=bsc.our_pair(MINT, older_quote))
    for index in range(256):
        add_native(files, source="0x%064x" % (index + 1), our_tx="0x%064x" % (index + 1000))
    assert tape.read_route_pins() == {OLD_HOP: OLD_QUOTE, HOP: QUOTE}


def test_unsubmitted_rows_do_not_evict_a_submitted_hop_from_the_window(files):
    add_native(files)
    with sqlite3.connect(files) as native:
        native.executemany("INSERT INTO bsc_lane_live (source_tx,our_tx,mint,reason) "
                           "VALUES (?,?,?,'rejected')", [(SOURCE, None, MINT)] * 300)
    assert tape.read_route_pins() == {OLD_HOP: OLD_QUOTE, HOP: QUOTE}


def test_multiple_matching_events_cannot_multiply_the_bounded_native_rows(files):
    add_native(files)
    with sqlite3.connect(files) as native:
        native.executemany("INSERT INTO bsc_lane_events (tx_hash,mint,pool,quote,mode,reason) "
                           "VALUES (?,?,?,?,?,'reverted')",
                           [(SOURCE, MINT, POOL, QUOTE, "live")] * 300)
    assert tape.read_route_pins() == {OLD_HOP: OLD_QUOTE, HOP: QUOTE}
