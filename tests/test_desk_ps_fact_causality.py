"""Creation evidence and all PumpSwap facts keep the frozen receipt/slot cuts."""
import sqlite3

import pytest

from sniper.services import memecoin_desk_structure_tape as ST
from test_desk_ps_model_facts import NOW, CUT, ROWS, api, candidate, facts, tape


def birth_block(tmp, *, slot, occurred, received, primary=True):
    path = tmp / "blocks" / (ST.utc_day(received) + ".db")
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE IF NOT EXISTS blocks(slot INTEGER " + ("PRIMARY KEY" if primary else "")
                   + ",block_time INTEGER,received_at REAL)")
        db.execute("INSERT INTO blocks VALUES(?,?,?)", (slot, occurred, received))
    return path


def launch(tmp, c, *, occurred, received=NOW - 5, slot=10, block_received=None):
    path = tmp / "launches" / (ST.utc_day(received) + ".db")
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE launches(mint TEXT PRIMARY KEY,curve TEXT,creator TEXT,slot INTEGER,signature TEXT,"
                   "block_time INTEGER,received_at REAL,symbol TEXT,name TEXT)")
        db.execute("INSERT INTO launches VALUES(?,?,?,?,?,?,?,?,?)",
                   (c["mint"], "curve", "creator", slot, "launch", occurred, received, "x", "x"))
    if occurred is not None:
        birth_block(tmp, slot=slot, occurred=occurred, received=received if block_received is None else block_received)


def pool_creation(tmp, c, *, occurred, received=NOW - 8, known=NOW - 7, slot=11, block_received=None):
    with sqlite3.connect(tmp / "pumpswap" / "tape.db") as db:
        db.execute("CREATE TABLE pools(pool TEXT,mint TEXT,quote_mint TEXT,canonical INTEGER,network_id TEXT,"
                   "venue TEXT,block_time INTEGER,received_at REAL,known_at REAL,slot INTEGER)")
        db.execute("CREATE INDEX pools_pool ON pools(pool)")
        db.execute("INSERT INTO pools VALUES(?,?,?,1,'solana','pumpswap',?,?,?,?)",
                   (c["pool"], c["mint"], "So11111111111111111111111111111111111111112", occurred, received, known, slot))
    if occurred is not None:
        birth_block(tmp, slot=slot, occurred=occurred, received=known if block_received is None else block_received)


def metadata(offset=0, *, created=None, known=NOW - 1, source="pumpfun"):
    return {"source": source, "created_timestamp": (NOW - offset - 300) * 1000 if created is None else created,
            "creator": "creator", "observed_at": NOW - 20 - offset, "host_received_at": NOW - 20,
            "clock_offset_s": offset, "known_at": known}


def test_retained_launch_helpers_never_read_a_future_row(tmp_path):
    c = candidate()
    launch(tmp_path, c, occurred=NOW - 200, received=NOW)
    assert ST.launch_received_at(tmp_path, c["mint"], NOW) is None
    assert ST.launch_creator(tmp_path, c["mint"], NOW) is None


@pytest.mark.parametrize("offset", [25200, -3600])
def test_true_birth_uses_block_time_provider_birth_is_distinct_and_clock_is_subtracted(tmp_path, offset):
    c, _ = tape(tmp_path)
    utc = NOW - offset
    launch(tmp_path, c, occurred=utc - 200)
    out = facts(c, tmp_path, as_of=NOW + 10, source_metadata=metadata(offset, known=NOW + 4))
    assert out["age_s"] == 210
    assert out["ages"]["coin"]["basis"] == "onchain_creation_block_time"
    assert out["ages"]["coin"]["known_at"] == NOW - 5
    assert out["ages"]["provider_reported"]["age_s"] == 310
    assert out["ages"]["provider_reported"]["basis"] == "pumpfun_created_timestamp_ms"
    assert out["ages"]["provider_reported"]["known_at"] == NOW + 4
    assert out["ages"]["provider_reported"]["source"] == "pumpfun"
    assert out["ages"]["provider_reported"]["observed_at"] == NOW - 20 - offset
    assert out["ages"]["provider_reported"]["clock_offset_s"] == offset
    assert out["creator_prior_launches"] is None
    assert out["pool_age_s"] is None


@pytest.mark.parametrize("change,reason", [({"clock_offset_s": None}, "clock_unknown"),
    ({"created_timestamp": (NOW + 1) * 1000}, "creation_future"),
    ({"known_at": NOW + 1}, "metadata_future"), ({"created_timestamp": -1}, "creation_unknown")])
def test_provider_unknowns_are_reasoned_not_zero(tmp_path, change, reason):
    c, _ = tape(tmp_path)
    meta = metadata()
    meta.update(change)
    out = facts(c, tmp_path, source_metadata=meta)
    assert out["ages"]["provider_reported"]["state"] == "unknown"
    assert out["ages"]["provider_reported"]["reason"] == reason
    assert out["age_s"] is None


def test_listing_first_swap_and_mapping_never_become_coin_or_creator_birth(tmp_path):
    c, _ = tape(tmp_path)
    out = facts(c, tmp_path)
    assert out["age_s"] is None and out["pool_age_s"] is None
    assert out["ages"]["observed"]["age_s"] == 100
    assert out["ages"]["observed"]["basis"] == "captured_receipt_span"
    assert out["ages"]["coin"]["reason"] == "creation_not_retained"
    assert out["creator_history"] == {"state": "unknown", "reason": "history_not_captured"}


def test_claim_lag_does_not_extend_market_rows_past_receipt_cut(tmp_path):
    c, path = tape(tmp_path, ROWS + [(2, 90, 1, 5000, "late")])
    first = facts(c, tmp_path)
    later = facts(c, tmp_path, as_of=NOW + 10)
    assert later["flow_60s"] == first["flow_60s"]
    assert later["structure"] == first["structure"]
    with sqlite3.connect(path) as db:
        db.execute("UPDATE swaps_ps SET slot=? WHERE signature='fixture5'", (CUT,))
    out = facts(c, tmp_path)
    assert out["flow_60s"]["buy_n"] == 1
    assert out["structure"]["price_usd"] == 3


def test_birth_receipt_and_slot_are_both_strictly_before_the_market_cut(tmp_path):
    c, _ = tape(tmp_path)
    launch(tmp_path, c, occurred=NOW - 200, slot=CUT)
    out = facts(c, tmp_path, source_metadata=metadata())
    assert out["age_s"] is None


def test_cut_slot_must_match_refreshed_candidate_and_receipt_cut_cannot_follow_claim(tmp_path):
    c, _ = tape(tmp_path)
    for cut, receipt in [(CUT + 1, NOW), (CUT, NOW + 1)]:
        with pytest.raises(ValueError, match="ps_facts"):
            api().facts_for(c, desk_dir=tmp_path, as_of=NOW, receipt_cut=receipt, cut_slot=cut)


def test_provider_creation_cannot_postdate_the_source_observation(tmp_path):
    c, _ = tape(tmp_path)
    out = facts(c, tmp_path, source_metadata=metadata(created=(NOW - 10) * 1000))
    assert out["ages"]["provider_reported"]["reason"] == "creation_future"


def test_a_proven_pool_creation_is_pool_age_only(tmp_path):
    c, _ = tape(tmp_path)
    pool_creation(tmp_path, c, occurred=NOW - 80)
    out = facts(c, tmp_path, source_metadata=metadata())
    assert out["pool_age_s"] == 80 and out["age_s"] is None
    assert out["ages"]["pool"]["known_at"] == NOW - 7
    assert out["ages"]["pool"]["basis"] == "onchain_pool_creation_block_time"


def test_old_candidate_cannot_be_presented_as_a_later_refreshed_claim(tmp_path):
    c, _ = tape(tmp_path)
    with pytest.raises(ValueError, match="ps_facts"):
        api().facts_for(c, desk_dir=tmp_path, as_of=NOW + 10, receipt_cut=NOW, cut_slot=CUT)


@pytest.mark.parametrize("source", ["pumpfun", "fomo", "dexscreener"])
@pytest.mark.parametrize("offset", [25200, -3600])
@pytest.mark.parametrize("future", [False, True])
def test_all_verified_feed_clocks_support_same_causal_coin_and_pool_birth(tmp_path, source, offset, future):
    c, _ = tape(tmp_path)
    utc = NOW - offset
    launch(tmp_path, c, occurred=utc - 200)
    pool_creation(tmp_path, c, occurred=utc - 80)
    out = facts(c, tmp_path, as_of=NOW + 10,
        source_metadata=metadata(offset, known=NOW + 11 if future else NOW + 4, source=source))
    if future:
        assert out["age_s"] is None and out["pool_age_s"] is None
        assert out["ages"]["coin"]["reason"] == out["ages"]["pool"]["reason"] == "metadata_future"
    else:
        assert out["age_s"] == 210 and out["pool_age_s"] == 90
    if source != "pumpfun":
        provider = out["ages"]["provider_reported"]
        assert provider["state"] == "unknown" and provider["reason"] == "source_unavailable"
        assert provider["age_s"] is None and provider["source"] is None
        assert provider["observed_at"] is None and provider["clock_offset_s"] is None


@pytest.mark.parametrize("kind", ["coin", "pool"])
def test_backfilled_inline_birth_requires_a_block_witness_before_market_cut(tmp_path, kind):
    c, _ = tape(tmp_path)
    create = launch if kind == "coin" else pool_creation
    create(tmp_path, c, occurred=NOW - 200, block_received=NOW + 1)
    out = facts(c, tmp_path, as_of=NOW + 10, source_metadata=metadata())
    assert out["ages"][kind]["state"] == "unknown"
    assert out["ages"][kind]["age_s"] is None


@pytest.mark.parametrize("kind", ["coin", "pool"])
def test_midnight_null_creation_uses_causal_adjacent_day_block_and_actual_known_at(tmp_path, kind):
    c, _ = tape(tmp_path)
    midnight = int(NOW // 86400) * 86400
    receipt, block_receipt, occurred = midnight - 0.5, midnight + 0.5, midnight - 5
    create = launch if kind == "coin" else pool_creation
    kwargs = {"known": receipt} if kind == "pool" else {}
    create(tmp_path, c, occurred=None, received=receipt, slot=10, **kwargs)
    birth_block(tmp_path, slot=10, occurred=occurred, received=block_receipt)
    out = facts(c, tmp_path, source_metadata=metadata())
    assert out["ages"][kind]["age_s"] == NOW - occurred
    assert out["ages"][kind]["occurred_at"] == occurred
    assert out["ages"][kind]["known_at"] == block_receipt


@pytest.mark.parametrize("kind", ["coin", "pool"])
@pytest.mark.parametrize("bad", ["missing", "wrong_slot", "future", "conflict", "unindexed"])
def test_retained_birth_refuses_missing_wrong_future_conflicting_or_unindexed_block(tmp_path, kind, bad):
    c, _ = tape(tmp_path)
    create = launch if kind == "coin" else pool_creation
    create(tmp_path, c, occurred=NOW - 200, slot=10)
    path = tmp_path / "blocks" / (ST.utc_day(NOW) + ".db")
    with sqlite3.connect(path) as db:
        if bad == "missing":
            db.execute("DELETE FROM blocks")
        elif bad == "wrong_slot":
            db.execute("UPDATE blocks SET slot=9")
        elif bad == "future":
            db.execute("UPDATE blocks SET received_at=?", (NOW,))
        elif bad == "conflict":
            db.execute("UPDATE blocks SET block_time=?", (NOW - 201,))
        else:
            db.execute("ALTER TABLE blocks RENAME TO old_blocks")
            db.execute("CREATE TABLE blocks(slot INTEGER,block_time INTEGER,received_at REAL)")
            db.execute("INSERT INTO blocks SELECT * FROM old_blocks")
    out = facts(c, tmp_path, source_metadata=metadata())
    assert out["ages"][kind]["state"] == "unknown"


@pytest.mark.parametrize("kind", ["coin", "pool"])
def test_retained_birth_records_later_block_knowledge_without_relabeling_birth(tmp_path, kind):
    c, _ = tape(tmp_path)
    create = launch if kind == "coin" else pool_creation
    create(tmp_path, c, occurred=NOW - 200, block_received=NOW - 1)
    out = facts(c, tmp_path, source_metadata=metadata())
    assert out["ages"][kind]["age_s"] == 200
    assert out["ages"][kind]["known_at"] == NOW - 1


@pytest.mark.parametrize("kind", ["coin", "pool"])
def test_retained_block_file_day_is_not_a_replacement_for_its_receipt_clock(tmp_path, kind):
    c, _ = tape(tmp_path)
    midnight = int(NOW // 86400) * 86400
    receipt, learned = midnight - 0.5, midnight - 0.1
    create = launch if kind == "coin" else pool_creation
    kwargs = {"known": receipt} if kind == "pool" else {}
    create(tmp_path, c, occurred=None, received=receipt, slot=10, **kwargs)
    path = birth_block(tmp_path, slot=10, occurred=midnight - 5, received=learned)
    path.rename(tmp_path / "blocks" / (ST.utc_day(midnight) + ".db"))
    out = facts(c, tmp_path, source_metadata=metadata())
    assert out["ages"][kind]["age_s"] == NOW - (midnight - 5)
    assert out["ages"][kind]["known_at"] == learned


@pytest.mark.parametrize("kind", ["coin", "pool"])
def test_zero_creation_slot_never_proves_pump_birth(tmp_path, kind):
    c, _ = tape(tmp_path)
    create = launch if kind == "coin" else pool_creation
    create(tmp_path, c, occurred=NOW - 200, slot=0)
    out = facts(c, tmp_path, source_metadata=metadata())
    assert out["ages"][kind]["state"] == "unknown"
