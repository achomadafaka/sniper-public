"""The BSC card anchors on the source block's MILLISECOND timestamp (7eb5c92c, 2026-10-17, Alpha).

A block timestamp is whole seconds, so "+0.3s our detection" on 56 carries up to 1 s of error on a
chain with sub-second blocks. BSC headers carry milliTimestamp; the EVM watcher stores it per detection
as push_head (head_at = host wall seconds at the head, head_minus_block_ms = head_at*1000 - header ms),
so the header ms is head_at*1000 - head_minus_block_ms. All 16 56 detections since 10-16 carry it;
4663 and 8453 heads carry no ms, so those cards keep the coarse whole-second line byte for byte.
Identity guard: an ms stamp whose second is not the row's block_time is not this block's, and is
ignored. Display only. Kill knob: MEMECOIN_CARD_BLOCK_MS_ENABLED=0."""
import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from sniper.persistence import db_memecoin as m  # noqa: E402
from sniper.services import memecoin_entry_view as ev  # noqa: E402

MINT = "0x9f69a7ea14871c2b3ce90d977a5d145955ea3a69"
UID = "b3a9a5b7-c3c5-5e46-a89c-a787e369629d"
BLOCK_TIME = 1792149685                                   # 2026-10-16T11:21:25Z
SIG = f"56:0xown:{MINT}"


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    row = dict(attempt_id="a1", origin_event_id=f"bscfast:{SIG}", network_id="56", mint=MINT,
               source_user_id=UID, source_created_at="2026-10-16T11:21:25.287781+00:00",
               source_timestamp_trust="observed_detection",
               observed_at="2026-10-16T11:21:25.300000+00:00",
               decision_started_at="2026-10-16T11:21:25.310000+00:00")
    c.execute(f"INSERT INTO memecoin_entry_attempts({','.join(row)}) VALUES "
              f"({','.join('?' for _ in row)})", tuple(row.values()))
    yield c
    c.close()


def _detect(c, valuation, *, block_time=BLOCK_TIME):
    c.execute("INSERT INTO onchain_wallet_detections (signature, source_user_id, address, network_id, "
              "mint, block_time, detected_at, created_at, source_valuation_json) "
              "VALUES (?,?,?,?,?,?,?,?,?)",
              (SIG, UID, "0xwallet", "56", MINT, block_time, "2026-10-16T11:21:25.287781+00:00",
               "2026-10-16T11:21:25.287781+00:00",
               valuation if valuation is None or isinstance(valuation, str) else json.dumps(valuation)))


def _head(block_ms, head_at=1792149685.198):
    return {"push_head": {"head_at": head_at, "log_minus_head_ms": 89.8,
                          "head_minus_block_ms": round(head_at * 1000.0 - block_ms, 1),
                          "log_minus_block_ms": 184.8}}


def _timing(c):
    return ev.entry_view(c, attempt_id="a1")["chain_timing"]


def test_a_bsc_card_anchors_on_the_header_millisecond_stamp(conn):
    _detect(conn, _head(1792149685103))
    t = _timing(conn)
    assert t["basis"] == "chain" and t["anchor_at"] == "2026-10-16T11:21:25.103000+00:00"
    assert re.fullmatch(r"chain (2026-10-16 )?11:21:25\.103 · \+0\.18s our detection", t["text"]), t["text"]


def test_the_processed_leg_is_measured_from_runner_receipt(conn):
    from sniper.services.memecoin_telegram import render_entry_notification
    from test_memecoin_notification_lifecycle import view as card_view
    _detect(conn, _head(1792149685103))
    v = card_view("refused", reason="x", decision_started_at="2026-10-16T11:21:25.310000+00:00",
                  chain_timing=_timing(conn))
    v["observed_at"] = "2026-10-16T11:21:25.300000+00:00"
    line = next(x for x in render_entry_notification(v).splitlines() if x.startswith("Times (UTC)"))
    assert "11:21:25.103 · +0.18s our detection · +0.0s processed" in line, line


@pytest.mark.parametrize("valuation", [
    None,                                             # an old row: no valuation at all
    {"reads": "x"},                                   # no push_head (4663 / 8453 heads carry no ms)
    {"push_head": {"head_at": 1792149685.198, "log_minus_head_ms": 89.8}},   # head seen, no ms
    "{not json",                                      # a broken valuation never breaks the card
    _head(1792149684903),                             # ms of another second: not this block's
    {"push_head": {"head_at": "x", "head_minus_block_ms": 95.0}},
])
def test_without_a_matching_millisecond_stamp_the_coarse_line_is_kept(conn, valuation):
    _detect(conn, valuation)
    t = _timing(conn)
    assert t["anchor_at"] == "2026-10-16T11:21:25+00:00"
    assert t["text"].endswith("11:21:25 · +0.3s our detection"), t["text"]


def test_the_kill_knob_restores_the_whole_second_anchor(conn, monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_CARD_BLOCK_MS_ENABLED", False)
    _detect(conn, _head(1792149685103))
    t = _timing(conn)
    assert t["anchor_at"] == "2026-10-16T11:21:25+00:00"
    assert t["text"].endswith("11:21:25 · +0.3s our detection"), t["text"]
