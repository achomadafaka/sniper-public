"""Signal-receipt bloat (Gamma a61a5bb27): 99.6% of memecoin_signal_receipts rows were
empty_decode_unknown, each carrying a 7.1 KB roster_json in evidence_json.

1. The drain keeps the empty-decode row (identity before network I/O) but not the roster copy;
   roster_generation still names the roster.
2. The fill-timing resolver never pages through empty decodes, never resets its cursor to 0, and
   reports full coverage without assuming dense rowids.
3. Retention of old empty rows runs only with the runner stopped.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from scripts.research import fill_timing_resolver as r
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_fill_timing as ft
from sniper.services.memecoin_feed_arrivals import ReceiptJournal
from sniper.services.memecoin_slot_skips import flush_journal

ROSTER_JSON = json.dumps({"owners": [["W" * 44, ["u", "h"]]] * 60, "armed": ["u"] * 60})


@pytest.fixture
def dbs(tmp_path):
    ledger = sqlite3.connect(tmp_path / "ledger.db")
    ledger.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(ledger)
    return ledger, ft.connect(tmp_path / "timing.db")


def _journal(run="source:R1"):
    j = ReceiptJournal(producer_run_id=run, boot_id="B", clock_domain="C")
    j.provenance_fn = lambda: {"roster_generation": "G1", "config_generation": "C1",
                               "roster_json": ROSTER_JSON}
    return j


def _record(j, state, sig="S", mint=None):
    ev = j.identity(10.0, 1)
    j.record_source(network_id="solana", source_sig=sig, source_wallet="W", mint=mint, pool=None,
                    venue=None, feed="f", recv_monotonic=10.0, connection_generation=1,
                    roster_generation=ev.get("roster_generation"),
                    config_generation=ev.get("config_generation"), decode_state=state,
                    evidence=dict(ev))


def _rows(timing):
    timing.row_factory = sqlite3.Row
    return [dict(x) for x in timing.execute(
        "SELECT rowid, decode_state, roster_generation, evidence_json FROM memecoin_signal_receipts "
        "ORDER BY rowid")]


# ------------------------------------------------------------------------- 1. writer

def test_an_empty_decode_keeps_its_row_but_not_the_roster_copy(dbs):
    _, timing = dbs
    j = _journal()
    _record(j, "empty_decode_unknown")
    _record(j, "decoded", sig="S2", mint="M")
    flush_journal(timing, j.snapshot(), 0)
    empty, decoded = _rows(timing)
    assert empty["decode_state"] == "empty_decode_unknown" and empty["roster_generation"] == "G1"
    assert "roster_json" not in json.loads(empty["evidence_json"])
    assert len(empty["evidence_json"]) < 1024 < len(ROSTER_JSON)
    assert json.loads(decoded["evidence_json"])["roster_json"] == ROSTER_JSON


# ------------------------------------------------------------------------- 2. resolver

def _seed(timing, states, run="source:R1"):
    j = _journal(run)
    for i, state in enumerate(states):
        _record(j, state, sig=f"S{i}", mint="M" if state == "decoded" else None)
    flush_journal(timing, j.snapshot(), 0)


def _cursor(timing):
    raw = timing.execute("SELECT value FROM memecoin_fill_timing_cursor WHERE name=?",
                         ("execution_source_receipts_v1",)).fetchone()
    return json.loads(raw[0]) if raw else {}


def test_the_resolver_never_scans_empty_decodes(dbs):
    ledger, timing = dbs
    _seed(timing, ["empty_decode_unknown"] * 50 + ["decoded"] * 2)
    out = r.resolve_execution_pass(ledger, timing, max_rows=512)
    assert out["scanned"] == 2
    assert all(res["decode_state"] == "decoded" for res in out["results"])


def test_a_caught_up_cursor_stays_at_the_frontier(dbs):
    ledger, timing = dbs
    _seed(timing, ["decoded"] * 3)
    r.resolve_execution_pass(ledger, timing, max_rows=512)
    frontier = timing.execute("SELECT MAX(rowid) FROM memecoin_signal_receipts").fetchone()[0]
    r.resolve_execution_pass(ledger, timing, max_rows=512)   # nothing new
    assert _cursor(timing)["cursor"] == frontier


def test_full_coverage_does_not_assume_dense_rowids(dbs):
    ledger, timing = dbs
    _seed(timing, ["empty_decode_unknown", "decoded", "empty_decode_unknown", "decoded"])
    out = r.resolve_execution_pass(ledger, timing, max_rows=512)
    assert out["scanned"] == 2 and out["pagination"]["full_universe_covered"] is True
    # A pass that did not start from the beginning never claims the whole universe.
    _seed(timing, ["decoded"], run="source:R2")
    later = r.resolve_execution_pass(ledger, timing, max_rows=512)
    assert later["pagination"]["full_universe_covered"] is False


def test_one_pass_reads_a_bounded_rowid_window(dbs, monkeypatch):
    ledger, timing = dbs
    monkeypatch.setattr(r, "RECEIPT_SCAN_WINDOW_ROWS", 10)
    _seed(timing, ["empty_decode_unknown"] * 25 + ["decoded"])
    first = r.resolve_execution_pass(ledger, timing, max_rows=512)
    assert first["scanned"] == 0 and _cursor(timing)["cursor"] == 10
    r.resolve_execution_pass(ledger, timing, max_rows=512)
    third = r.resolve_execution_pass(ledger, timing, max_rows=512)
    assert third["scanned"] == 1


# ------------------------------------------------------------------------- 3. retention

def test_prune_keeps_the_latest_run_and_every_decoded_row(dbs):
    _, timing = dbs
    _seed(timing, ["empty_decode_unknown"] * 3 + ["decoded"], run="source:OLD")
    _seed(timing, ["empty_decode_unknown"] * 2, run="source:NEW")
    deleted = ft.prune_empty_receipts(timing, batch=2)
    assert deleted == 3
    left = timing.execute("SELECT producer_run_id, decode_state FROM memecoin_signal_receipts "
                          "ORDER BY rowid").fetchall()
    assert [tuple(x) for x in left] == [("source:OLD", "decoded"),
                                        ("source:NEW", "empty_decode_unknown"),
                                        ("source:NEW", "empty_decode_unknown")]


@pytest.mark.parametrize("age, live", [(5.0, True), (119.9, True), (None, True), (120.1, False)])
def test_prune_refuses_while_the_runner_is_live(age, live):
    from scripts import prune_empty_receipts as script
    assert script.runner_live(age) is live


def test_even_the_dry_run_refuses_before_touching_a_live_file(monkeypatch):
    from scripts import prune_empty_receipts as script
    from sniper.services import memecoin_fill_timing as ft_mod
    monkeypatch.setattr(script, "heartbeat_age_s", lambda: 3.0)
    monkeypatch.setattr(ft_mod, "connect", lambda *a, **k: pytest.fail("opened the live file"))
    assert script.main([]) == 2
