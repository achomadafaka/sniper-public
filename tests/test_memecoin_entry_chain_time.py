"""D-a (2026-10-16, Alpha): the entry card and view anchor on CHAIN time, display only.

CLAWDMKT 0x9f69a7ea…3a69 on 4663: the J7 stamp read 11:21:20.000Z for a source block whose timestamp
is 11:21:25 (block 90038422); our watcher saw it at 11:21:25.288. The card anchored on the J7 stamp
and printed "+5.8s received" for a coin we saw 0.3 s after its block. Pins: the anchor is the source
block's timestamp, else, for a J7 entry, our detection; the J7 stamp is an offset from the anchor;
"processed" is measured from our receipt; a view without chain timing keeps the old line.

Gamma B1/B2 (FAIL on 535253da8): proximity beat identity. An origin naming its transaction reads that
one detection row by signature, never a window; a J7 entry uses a window detection only when it is the
only one there; the J7 receipt alone is never "our detection"."""
import re
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from sniper.persistence import db_memecoin as m  # noqa: E402
from sniper.services import memecoin_entry_view as ev  # noqa: E402
from test_memecoin_notification_lifecycle import view as card_view  # noqa: E402

MINT = "0x9f69a7ea14871c2b3ce90d977a5d145955ea3a69"
UID = "b3a9a5b7-c3c5-5e46-a89c-a787e369629d"
BLOCK_TIME = 1792149685                                   # 2026-10-16T11:21:25Z, block 90038422


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    yield c
    c.close()


def _detect(c, detected_at, *, uid=UID, network="4663", block_time=BLOCK_TIME, sig="s1"):
    c.execute("INSERT INTO onchain_wallet_detections (signature, source_user_id, address, network_id, "
              "mint, block_time, detected_at, created_at) VALUES (?,?,?,?,?,?,?,?)",
              (sig, uid, "0xwallet", network, MINT, block_time, detected_at, detected_at))


def _j7_attempt(c):
    row = dict(attempt_id="a1", origin_event_id="swap:j7", network_id="4663", mint=MINT,
               source_user_id=UID, source_handle="0xLingard", ticker="CLAWDMKT",
               source_created_at="2026-10-16T11:21:20.000Z", source_timestamp_trust="claimed_untrusted",
               observed_at="2026-10-16T11:21:25.826503+00:00",
               decision_started_at="2026-10-16T11:21:27.115337+00:00", terminal_status="refused",
               terminal_reason="copy price minimum is +36.2% above source fill (cap 30.0%)",
               completed_at="2026-10-16T11:21:27.378039+00:00")
    c.execute(f"INSERT INTO memecoin_entry_attempts({','.join(row)}) VALUES "
              f"({','.join('?' for _ in row)})", tuple(row.values()))


def _timing(c):
    return ev.entry_view(c, attempt_id="a1")["chain_timing"]


def test_a_j7_entry_anchors_on_the_source_blocks_timestamp(conn):
    _j7_attempt(conn)
    _detect(conn, "2026-10-16T11:21:25.287781+00:00")
    t = _timing(conn)
    assert t["basis"] == "chain" and t["anchor_at"] == "2026-10-16T11:21:25+00:00"
    assert re.fullmatch(r"chain (2026-10-16 )?11:21:25 · \+0\.3s our detection · -5\.0s J7 stamp",
                        t["text"]), t["text"]


def _tx_attempt(c, origin, network="4663"):
    row = dict(attempt_id="a1", origin_event_id=origin, network_id=network, mint=MINT,
               source_user_id=UID, source_created_at="2026-10-16T11:21:25.287781+00:00",
               source_timestamp_trust="observed_detection",
               observed_at="2026-10-16T11:21:25.300000+00:00",
               decision_started_at="2026-10-16T11:21:25.310000+00:00")
    c.execute(f"INSERT INTO memecoin_entry_attempts({','.join(row)}) VALUES "
              f"({','.join('?' for _ in row)})", tuple(row.values()))


@pytest.mark.parametrize("lane,chain", [("rhfast", "4663"), ("bscfast", "56")])
def test_a_fast_lane_origin_reads_its_own_row_not_an_earlier_buy(conn, lane, chain):
    # Gamma (a): bscfast:56:0x7c035ca5... printed "chain 10:03:33 ... +25.5s processed"; its own
    # block was 10:03:58. A drip buyer's buy 25 s earlier is in the window and is not this one.
    # The lane's detection signature is its origin without the lane prefix.
    _tx_attempt(conn, f"{lane}:{chain}:0xown:{MINT}", network=chain)
    _detect(conn, "2026-10-16T11:21:00.300000+00:00", block_time=BLOCK_TIME - 25, network=chain,
            sig=f"{chain}:0xearly:{MINT}")
    _detect(conn, "2026-10-16T11:21:25.287781+00:00", network=chain, sig=f"{chain}:0xown:{MINT}")
    t = _timing(conn)
    assert t["basis"] == "chain" and t["anchor_at"] == "2026-10-16T11:21:25+00:00"
    assert t["text"].endswith("11:21:25 · +0.3s our detection"), t["text"]


def test_an_onchain_origin_takes_our_detection_from_its_own_row(conn):
    # Gamma (b): onchain:8453:0xbd68f462... had its own block right but "-24.5s our detection"
    # from an earlier buy (true -1.2s).
    _tx_attempt(conn, "onchain:4663:0xown:" + MINT)
    _detect(conn, "2026-10-16T11:21:00.300000+00:00", block_time=BLOCK_TIME - 25,
            sig="4663:0xearly:" + MINT)
    _detect(conn, "2026-10-16T11:21:25.287781+00:00", sig="4663:0xown:" + MINT)
    assert _timing(conn)["text"].endswith("11:21:25 · +0.3s our detection")


def test_a_j7_entry_with_two_buys_in_the_window_has_no_chain_line(conn):
    # Gamma (c): J7 swap:039f3c26 anchored on an earlier buy's block 06:20:17; 2+ buys in the window
    # on 4663 80/419, 56 65/442, 8453 10/52. Another wallet's or chain's row is not a second buy.
    _j7_attempt(conn)
    _detect(conn, "2026-10-16T11:21:25.287781+00:00", sig="first")
    _detect(conn, "2026-10-16T11:21:24.000000+00:00", uid="someone-else", sig="other-wallet")
    _detect(conn, "2026-10-16T11:21:24.500000+00:00", network="56", sig="other-chain")
    assert _timing(conn)["text"].endswith("11:21:25 · +0.3s our detection · -5.0s J7 stamp")
    _detect(conn, "2026-10-16T11:21:03.100000+00:00", block_time=BLOCK_TIME - 22, sig="earlier")
    assert _timing(conn) is None


@pytest.mark.parametrize("detected_at", [None, "2026-10-16T11:20:50.000000+00:00"])
def test_a_j7_entry_with_no_detection_has_no_chain_line(conn, detected_at):
    # Gamma (d) / B2: the J7 receipt alone is not our detection (35.8 s before it is outside 30 s).
    _j7_attempt(conn)
    if detected_at:
        _detect(conn, detected_at)
    assert _timing(conn) is None


@pytest.mark.parametrize("block_time", [None, 0])
def test_a_j7_entry_without_a_block_time_anchors_on_our_detection(conn, block_time):
    _j7_attempt(conn)
    _detect(conn, "2026-10-16T11:21:25.287781+00:00", block_time=block_time)
    t = _timing(conn)
    assert t["basis"] == "our_detection"
    assert re.fullmatch(r"our detection (2026-10-16 )?11:21:25 · -5\.3s J7 stamp", t["text"]), t["text"]
    assert t["our_detection_at"].startswith("2026-10-16T11:21:25.287781")


def test_a_tx_origin_without_its_own_row_or_block_time_keeps_the_old_line(conn):
    _tx_attempt(conn, "onchain:4663:0xown:" + MINT)
    _detect(conn, "2026-10-16T11:21:25.200000+00:00", sig="4663:0xother:" + MINT)
    assert _timing(conn) is None                          # "detected HH:MM:SS" already says it
    _detect(conn, "2026-10-16T11:21:25.287781+00:00", block_time=None, sig="4663:0xown:" + MINT)
    assert _timing(conn) is None


def test_the_card_prints_chain_time_and_measures_processed_from_runner_receipt():
    from sniper.services.memecoin_telegram import render_entry_notification
    v = card_view("refused", reason="copy price minimum is +36.2% above source fill (cap 30.0%)",
                  decision_started_at="2026-10-16T11:21:27.115337+00:00",
                  chain_timing={"anchor_at": "2026-10-16T11:21:25+00:00", "basis": "chain",
                                "text": "chain 11:21:25 · +0.3s our detection · -5.0s J7 stamp"})
    v["observed_at"] = "2026-10-16T11:21:25.826503+00:00"
    line = next(x for x in render_entry_notification(v).splitlines() if x.startswith("Times (UTC)"))
    assert line.startswith("Times (UTC): chain 11:21:25 · +0.3s our detection · -5.0s J7 stamp"
                           " · +1.3s processed"), line
    assert "received" not in line and "source (unverified)" not in line


def test_a_view_without_chain_timing_keeps_the_old_line():
    from sniper.services.memecoin_telegram import render_entry_notification
    v = card_view("refused", reason="x", decision_started_at="2026-09-11T12:00:02.500000+00:00")
    line = next(x for x in render_entry_notification(v).splitlines() if x.startswith("Times (UTC)"))
    # Byte for byte what main rendered for this view before D-a.
    assert line == ("Times (UTC): source 2026-09-11 12:00:01 · +1.0s received · +0.5s processed"
                    " · 1.0s receipt to result"), line


def test_a_broken_stamp_never_breaks_the_view(conn):
    _j7_attempt(conn)
    _detect(conn, "2026-10-16T11:21:25.287781+00:00", block_time=10 ** 15)   # not a timestamp
    assert _timing(conn) is None
