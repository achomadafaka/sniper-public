"""The fast-lane coverage census classifier (scripts/research/fast_lane_coverage_census.py), loaded by path."""
import importlib.util
import sqlite3

import pytest
from datetime import timedelta
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "fast_lane_coverage_census", Path(__file__).resolve().parents[1] / "scripts/research/fast_lane_coverage_census.py")
census = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(census)


def test_a_lane_that_attempted_is_never_skipped_as_the_feeds():
    # 2EUKLvNQ (DegenCapitalLLC, 2026-09-28 18:22:54Z) read "d, J7 first by +0.17 s": the on-chain
    # lane's "deferred slot_lane_unresolved" line (waiting on the slot lane) beside a slot-lane chain
    # failure and an on-chain chase refusal. A lane that attempted did not skip.
    t = census.utc("2026-09-28T11:00:00+00:00")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE memecoin_entry_attempts (network_id, mint, source_user_id, signal_source, "
                 "terminal_status, terminal_reason, origin_event_id, decision_started_at)")
    conn.execute("CREATE TABLE onchain_wallet_detections (network_id, source_user_id, mint, signature, "
                 "detected_at)")
    item = {"net": "solana", "t0": t, "observed": t, "uid": "u", "mint": "MintAAAAxyz", "handle": "h",
            "origin": "swap:raybot:SIGx:u:mint"}
    conn.execute("INSERT INTO onchain_wallet_detections VALUES ('solana','u','MintAAAAxyz','SIGx',?)",
                 ((t + timedelta(seconds=0.17)).isoformat(),))
    lines = {("h", "sigx"): [(t + timedelta(seconds=0.2), "deferred slot_lane_unresolved")]}
    assert census.classify(item, conn, {}, lines)[0] == "d", "no attempt: still the skip"
    conn.execute("INSERT INTO memecoin_entry_attempts VALUES ('solana','MintAAAAxyz','u','onchain_watch',"
                 "'refused','exact quote is +36% above source price (cap 30%)','onchain:SIGx',?)",
                 (t.isoformat()[:19],))
    bucket, detail = census.classify(item, conn, {}, lines)
    assert bucket == "c" and "onchain_watch refused" in detail, (bucket, detail)


def _race_ledger(position_source, closed_at=None):
    t = census.utc("2026-09-28T11:00:00+00:00")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE memecoin_entry_attempts (network_id, mint, source_user_id, signal_source, "
                 "terminal_status, terminal_reason, origin_event_id, decision_started_at)")
    conn.execute("CREATE TABLE onchain_wallet_detections (network_id, source_user_id, mint, signature, "
                 "detected_at)")
    conn.execute("CREATE TABLE memecoin_positions (execution_mode, network_id, mint, signal_source, "
                 "opened_at, closed_at)")
    conn.execute("INSERT INTO memecoin_positions VALUES ('real','solana','MintAAAAxyz',?,?,?)",
                 (position_source, (t - timedelta(seconds=3)).isoformat(), closed_at))
    conn.execute("INSERT INTO memecoin_entry_attempts VALUES ('solana','MintAAAAxyz','u','onchain_watch',"
                 "'refused','one position per coin: open funded position','onchain:SIGx',?)",
                 ((t + timedelta(seconds=1)).isoformat(),))
    item = {"net": "solana", "t0": t, "observed": t, "uid": "u", "mint": "MintAAAAxyz", "handle": "h",
            "origin": "swap:raybot:SIGx:u:mint"}
    return census.classify(item, conn, {}, {})


def test_a_lane_that_lost_the_race_to_j7s_own_fill_is_a_d():
    # Gamma 2026-09-28: the refusal happened BECAUSE J7's fill held the coin; with J7 off, the lane
    # would have taken it.
    bucket, detail = _race_ledger("follow_activity")
    assert bucket == "d" and "lost the race to J7" in detail and "4.0 s earlier" in detail


@pytest.mark.parametrize("source,closed", [("slot_lane", None), ("follow_activity", "2026-09-28T10:59:58+00:00")])
def test_another_lanes_position_or_a_closed_j7_one_keeps_the_refusal_a_c(source, closed):
    assert _race_ledger(source, closed)[0] == "c"


def test_source_sig_reads_every_origin_shape():
    """A pump origin carries the signature FIRST (`pump:<sig>:<address>:buy:<mint>`); the census read
    parts[2], an address, so Thepennyflippe read 0 of 11 lane sightings by tx (Beta, 2026-09-29)."""
    sig = "4rqPMyTaCpqXYid9oik9Sas1JiYL8qkHjSZ83LW2mSx9vZLEtppatVxdGm2pg389ezZxdnrsVE7K5C8naJm9NCGN"
    assert census.source_sig("pump:%s:CAPn1yH4oSywsxGU456jfgTrSSUidf9jgeAnHceNUJdw:buy:Mint" % sig) == sig
    assert census.source_sig("swap:raybot:%s:u:mint" % sig) == sig
    assert census.source_sig("onchain:%s" % sig) == sig
    assert census.source_sig("onchain:4663:0xabc:0xmint") == "0xabc"
    assert census.source_sig("swap:2c8736eb-b173-43a3-a0e8-1137178a5044") is None
    assert census.source_sig("pump:") is None
