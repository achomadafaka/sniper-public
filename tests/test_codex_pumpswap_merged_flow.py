"""Raw program logs through isolated research storage, admission and descriptive features; offline."""
import base64
import importlib.util
import json
import sqlite3
import struct
from datetime import datetime, timezone
from pathlib import Path

import pytest
from solders.pubkey import Pubkey

from sniper.execution.pumpswap_buy import _BUY_EVENT_FIELDS, CREATE_POOL_EVENT_DISCRIMINATOR
from sniper.feeds.yellowstone import geyser_pb2 as pb
from sniper.persistence import db_memecoin_desk as D
from sniper.services import memecoin_desk as desk
from sniper.services import memecoin_desk_contract as K
from sniper.services import memecoin_desk_pumpswap as PS
from sniper.services import memecoin_desk_safety as S
from sniper.services import memecoin_desk_universe as U
from sniper.services.memecoin_rabbit_dflow_amm import encode_buy_event

ROOT = Path(__file__).resolve().parents[1]
NOW = 1_800_000_000.0
MINT = U.b58encode(bytes([1]) * 32)
ACTOR = U.b58encode(bytes([2]) * 32)
POOL = S.pumpswap_pool_address(MINT)


def script(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


W = script("codex_merged_universe_worker", "scripts/memecoin_desk_universe_worker.py")
F = script("codex_merged_pool_features", "scripts/research/desk_pumpswap_features.py")


def create_line():
    # The existing shared CreatePool parser defines these offsets; other bytes convey no metadata.
    raw = bytearray(205)
    raw[:8] = CREATE_POOL_EVENT_DISCRIMINATOR
    struct.pack_into("<qH", raw, 8, int(NOW), 0)
    authority = Pubkey.find_program_address(
        [b"pool-authority", bytes(Pubkey.from_string(MINT))], Pubkey.from_string(U.PUMP_PROGRAM))[0]
    raw[18:50] = bytes(authority)
    raw[50:82] = bytes(Pubkey.from_string(MINT))
    raw[82:114] = bytes(Pubkey.from_string(K.WSOL_MINT))
    raw[173:205] = bytes(Pubkey.from_string(POOL))
    return "Program data: " + base64.b64encode(raw).decode()


def buy_line(cap_usd, *, ix='buy'):
    event = {name: (ACTOR if kind == "pubkey" else False if kind == "bool"
                    else "buy" if kind == "string" else 0) for name, kind in _BUY_EVENT_FIELDS}
    event.update(timestamp=int(NOW), pool=POOL, user=ACTOR, coin_creator=MINT,
                 ix_name=ix,
                 base_amount_out=100, pool_base_token_reserves=10**15 + 100,
                 pool_quote_token_reserves=round(cap_usd / 150 * 10**9) - 1002,
                 quote_amount_in=1000, quote_amount_in_with_lp_fee=1002, user_quote_amount_in=1004,
                 lp_fee_basis_points=20, lp_fee=2, protocol_fee_basis_points=5, protocol_fee=1,
                 coin_creator_fee_basis_points=5, coin_creator_fee=1)
    line=encode_buy_event(event)
    return "Program data: " + base64.b64encode(base64.b64decode(line[14:]) + bytes(16)).decode()


def frame(number, *lines, malformed_stack=False):
    update = pb.SubscribeUpdate()
    update.filters.append("pumpswap")
    update.transaction.slot = 10 + number
    info = update.transaction.transaction
    info.index = 1
    info.signature = bytes([number + 1]) * 64
    info.transaction.message.account_keys.extend([U.b58decode(ACTOR), U.b58decode(PS.AMM_PROGRAM)])
    info.meta.log_messages.extend([f"Program {PS.AMM_PROGRAM} invoke [1]", *lines])
    if not malformed_stack:
        info.meta.log_messages.append(f"Program {PS.AMM_PROGRAM} success")
    return update.SerializeToString()


def harness(tmp_path, monkeypatch):
    for key, value in (("CAPTURE", "1"), ("EMISSIONS", "1"),
                       ("BAND_LO_USD", "30000"), ("BAND_HI_USD", "100000")):
        monkeypatch.setenv("MEMECOIN_DESK_PUMPSWAP_" + key, value)
    clock = [NOW]
    monkeypatch.setattr(W.time, "time", lambda: clock[0])
    # Explicit independent synthetic facts; no account enrichment or guessed production metadata.
    metadata = {"pool": POOL, "mint": MINT, "quote_mint": K.WSOL_MINT, "base_decimals": 6,
                "supply_atomic": str(10**15), "virtual_quote_atomic": "0", "known_at": NOW - 10,
                "slot": 1, "source": "fixture:independent_pool_facts"}
    worker = W.Worker(W.knobs({}), base=tmp_path, sol_price_fn=lambda: None,
                      alert_fn=lambda *_: None, pool_metadata_fn=lambda _: metadata)
    worker.sol_usd, worker.sol_at = 150, NOW

    def play(number, *lines, malformed_stack=False):
        clock[0] = NOW + number
        raw = frame(number, *lines, malformed_stack=malformed_stack)
        worker.process(raw, received_at=clock[0] - 0.01)
        worker.commit_files()
        return raw

    return worker, play


def emissions(base):
    return [json.loads(line) for path in sorted((base / "candidate_emissions_ps").glob("*.jsonl"))
            for line in path.read_text().splitlines()]


def crossing(base, monkeypatch):
    worker, play = harness(base, monkeypatch)
    play(0, create_line())
    play(1, buy_line(20_000))
    raw = play(2, buy_line(40_000))
    return worker, play, raw, emissions(base)[0]


def at(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def test_raw_wire_to_causal_pool_candidate_desk_and_separate_descriptive_registry(tmp_path, monkeypatch):
    worker, _, raw, candidate = crossing(tmp_path, monkeypatch)
    assert K.validate_candidate(candidate)["venue"] == "pumpswap"
    assert S.verify_u_emission(candidate, now=NOW + 3, max_age_s=5) == candidate
    state = candidate["snapshot"]["fields"]["pool_state"]["value"]
    assert state["map_known_at"] == NOW and state["metadata_known_at"] == NOW - 10
    assert state["metadata_source"] == "fixture:independent_pool_facts"
    assert state["base_reserve_atomic"] == str(10**15)
    assert candidate["snapshot"]["fields"]["mcap_usd"]["value"] == pytest.approx(40_000)

    source = sqlite3.connect(tmp_path / "pumpswap" / "tape.db")
    assert source.execute("SELECT canonical,mint,quote_mint FROM pools").fetchone() == (1, MINT, K.WSOL_MINT)
    rows = source.execute("SELECT mint,map_status,map_known_at,received_at,processed_at,"
                          "typeof(pre_base_reserve_atomic),quote_atomic FROM swaps_ps ORDER BY slot").fetchall()
    assert rows == [(MINT, "mapped", NOW, NOW + 0.99, NOW + 1, "text", "1004"),
                    (MINT, "mapped", NOW, NOW + 1.99, NOW + 2, "text", "1004")]
    source.close()
    worker.process(raw, received_at=NOW + 2.5)
    worker.commit_files()
    assert len(emissions(tmp_path)) == 1 and worker.stats["ps_duplicate"] == 1

    conn = D.connect(tmp_path / "desk.db")
    D.register_cohort(conn, cohort_id="pool-cohort", producer="U", prompt_version="pool-fixture-v1",
                      policy={"venue": "pumpswap", "clip_usd": 25}, created_at=at(NOW - 1))
    head = desk.ingest_candidate(conn, candidate, cohort_id="pool-cohort", now_iso=at(NOW + 3))
    assert head["is_primary"] and head["candidate"]["snapshot"] == candidate["snapshot"]
    again = desk.ingest_candidate(conn, candidate, cohort_id="pool-cohort", now_iso=at(NOW + 4))
    assert not again["is_primary"] and again["reason"] == "duplicate"
    assert conn.execute("SELECT COUNT(*) FROM desk_candidate_emissions").fetchone()[0] == 1
    conn.close()

    (tmp_path / "ranker").mkdir()
    curve_registry = tmp_path / "ranker" / "candidates.db"
    curve_registry.write_bytes(b"curve-registry-must-not-be-opened")
    assert F.cycle(tmp_path, cutoff=NOW + 10, cutoff_slot=20) == {
        "n_candidates": 1, "n_descriptive": 1, "n_unscored": 0}
    ranked = sqlite3.connect(tmp_path / "ranker_ps" / "candidates.db")
    original = ranked.execute("SELECT first_rank_at,status,features_json,score,shortlisted FROM first_features").fetchone()
    features = json.loads(original[2])
    assert original[:2] == (NOW + 10, "descriptive") and original[-2:] == (None, None)
    assert features["n_swaps"] == 2 and features["distinct_buyers"] == 1
    assert features["buy_sol"] == pytest.approx(2008 / 1e9)
    F.cycle(tmp_path, cutoff=NOW + 11, cutoff_slot=21)
    assert ranked.execute("SELECT first_rank_at,status,features_json,score,shortlisted FROM first_features").fetchone() == original
    ranked.close()
    assert curve_registry.read_bytes() == b"curve-registry-must-not-be-opened"
    assert not (tmp_path / "candidate_emissions").exists()
    assert list((tmp_path / "pumpswap").glob("*.db")) == [tmp_path / "pumpswap" / "tape.db"]


def test_raw_mixed_malformed_event_and_bad_stack_reset_crossing_and_persist_typed_gaps(tmp_path, monkeypatch):
    worker, play = harness(tmp_path, monkeypatch)
    play(0, create_line())
    play(1, buy_line(20_000))
    malformed = "Program data: " + base64.b64encode(bytes.fromhex("67f4521f2cf57777")).decode()
    play(2, buy_line(40_000), malformed)
    assert not worker.ps_book._inside and not emissions(tmp_path)
    play(3, buy_line(40_000))
    assert not emissions(tmp_path)  # The post-gap inside observation only seeds.
    play(4, buy_line(20_000))
    play(5, buy_line(40_000), malformed_stack=True)
    assert not worker.ps_book._inside and not emissions(tmp_path)
    play(6, buy_line(40_000))
    assert not emissions(tmp_path)
    play(7, buy_line(20_000))
    play(8, buy_line(40_000))
    assert len(emissions(tmp_path)) == 1 and worker.stats["ps_decode_loss"] == 2
    source = sqlite3.connect(tmp_path / "pumpswap" / "tape.db")
    assert source.execute("SELECT cause FROM gaps_ps ORDER BY at").fetchall() == [("decode_loss",), ("decode_loss",)]
    assert source.execute("SELECT COUNT(*) FROM swaps_ps").fetchone()[0] == 6
    source.close()
    assert F.cycle(tmp_path, cutoff=NOW + 10, cutoff_slot=20) == {
        "n_candidates": 1, "n_descriptive": 0, "n_unscored": 1}
    registry = sqlite3.connect(tmp_path / "ranker_ps" / "candidates.db")
    assert registry.execute("SELECT reason,score,shortlisted FROM first_features").fetchone() == ("tape_gap", None, None)
    registry.close()


def test_raw_pool_candidate_wrong_venue_cohort_refuses_before_evidence_writes(tmp_path, monkeypatch):
    _, _, _, candidate = crossing(tmp_path, monkeypatch)
    conn = D.connect(tmp_path / "desk.db")
    D.register_cohort(conn, cohort_id="curve-cohort", producer="U", prompt_version="curve-fixture-v1",
                      policy={"clip_usd": 25}, created_at=at(NOW - 1))
    with pytest.raises(ValueError, match="venue_mismatch"):
        desk.ingest_candidate(conn, candidate, cohort_id="curve-cohort", now_iso=at(NOW + 3))
    for table in ("desk_snapshots", "desk_ticket_heads", "desk_candidate_emissions"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    conn.close()
