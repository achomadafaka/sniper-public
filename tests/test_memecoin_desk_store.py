"""Dedicated desk persistence: immutable evidence, coin ownership and durable handoff."""
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest

from sniper.persistence import db_memecoin_desk as desk

T0 = "2026-10-21T12:00:00+00:00"


def at(seconds):
    return (datetime.fromisoformat(T0) + timedelta(seconds=seconds)).isoformat()


def candidate(key="u-1", *, producer="U", mint="MintA", seconds=0, value=41000):
    snapshot = {"emission_at": datetime.fromisoformat(at(seconds)).timestamp(),
                "fields": {"mcap_usd": {"value": value, "source": "pumpfun_grpc:curve_reserves",
                                        "known_at": datetime.fromisoformat(at(seconds)).timestamp(),
                                        "slot": 7, "completeness": "complete"}}, "missing": []}
    snapshot["hash"] = hashlib.sha256(json.dumps(
        snapshot, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"opportunity_id": key, "ticket_id": key, "producer": producer,
            "network_id": "solana", "mint": mint, "pool": "CurveA", "emitted_at": at(seconds),
            "snapshot": snapshot, "snapshot_hash": snapshot["hash"]}


def cohort(conn, producer="U"):
    return desk.register_cohort(conn, cohort_id=f"cohort-{producer}", producer=producer,
                                prompt_version="desk-judge-v6", policy={"clip_usd": 25, "max": 5},
                                created_at=T0)


def opening(conn, key="u-1", **kwargs):
    producer = kwargs.get("producer", "U")
    return desk.record_candidate(conn, candidate(key, **kwargs), cohort_id=f"cohort-{producer}",
                                 now_iso=at(kwargs.get("seconds", 0)), cooldown_s=900)


def stage(conn, ticket_id="u-1", **kwargs):
    args = {"ticket_id": ticket_id, "stage": "chief", "seat": "JUDGE", "verdict": "ENTER",
            "reason": "momentum", "inputs": {"mcap_usd": 41000}, "ts": at(5),
            "output": {"action": "ENTER"}, "response_at": at(4)}
    args.update(kwargs)
    return desk.record_stage(conn, **args)


def request(intent_id="intent-1"):
    return {"intent_id": intent_id, "requested_at": at(5), "expires_at": at(35),
            "payload": {"origin_event_id": "desk:u-1", "clip_usd": 25, "mint": "MintA"}}


@pytest.fixture
def conn(tmp_path):
    c = desk.connect(tmp_path / "desk.sqlite")
    cohort(c)
    cohort(c, "H")
    yield c
    c.close()


def test_cohort_configuration_is_canonical_and_immutable(conn):
    original = cohort(conn)
    same = desk.register_cohort(conn, cohort_id="cohort-U", producer="U",
                               prompt_version="desk-judge-v6", policy={"max": 5, "clip_usd": 25},
                               created_at=T0)
    assert same == original
    with pytest.raises(ValueError, match="cohort_conflict"):
        desk.register_cohort(conn, cohort_id="cohort-U", producer="U", prompt_version="v7",
                             policy={"clip_usd": 25, "max": 5}, created_at=T0)
    assert conn.execute("SELECT COUNT(*) FROM desk_cohorts").fetchone()[0] == 2


def test_candidate_requires_registered_matching_cohort(conn):
    with pytest.raises(ValueError, match="cohort_unknown"):
        desk.record_candidate(conn, candidate(), cohort_id="unknown", now_iso=T0, cooldown_s=900)
    with pytest.raises(ValueError, match="producer_mismatch"):
        desk.record_candidate(conn, candidate(), cohort_id="cohort-H", now_iso=T0, cooldown_s=900)
    assert conn.execute("SELECT COUNT(*) FROM desk_candidate_emissions").fetchone()[0] == 0


def test_second_producer_sighting_preserves_opener_and_retains_its_own_emission(conn):
    first = opening(conn)
    later = opening(conn, "h-2", producer="H", seconds=1, value=42000)
    assert first["is_primary"] is True
    assert (later["ticket_id"], later["opportunity_id"], later["producer"], later["cohort_id"]) == (
        "u-1", "u-1", "U", "cohort-U")
    assert later["is_primary"] is False and later["reason"] == "active_ticket"
    rows = conn.execute("SELECT opportunity_id, ticket_id, producer, cohort_id, is_primary "
                        "FROM desk_candidate_emissions ORDER BY emitted_at").fetchall()
    assert [tuple(row) for row in rows] == [("u-1", "u-1", "U", "cohort-U", 1),
                                          ("h-2", "u-1", "H", "cohort-H", 0)]
    assert conn.execute("SELECT COUNT(*) FROM desk_snapshots").fetchone()[0] == 2


def test_identical_emission_replay_cannot_reopen_and_conflicting_replay_refuses(conn):
    first = opening(conn)
    replay = desk.record_candidate(conn, candidate(), cohort_id="cohort-U", now_iso=at(1000), cooldown_s=900)
    assert replay["ticket_id"] == first["ticket_id"]
    assert replay["is_primary"] is False and replay["reason"] == "duplicate"
    with pytest.raises(ValueError, match="emission_conflict"):
        opening(conn, value=42000)
    assert conn.execute("SELECT COUNT(*) FROM desk_candidate_emissions").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM desk_snapshots").fetchone()[0] == 1


def test_snapshot_hash_cannot_alias_other_bytes(conn):
    opening(conn)
    changed = candidate("u-2", seconds=1)
    changed["snapshot"]["fields"]["mcap_usd"]["value"] = 99999
    with pytest.raises(ValueError, match="snapshot_hash"):
        desk.record_candidate(conn, changed, cohort_id="cohort-U", now_iso=at(1), cooldown_s=900)
    assert conn.execute("SELECT COUNT(*) FROM desk_candidate_emissions").fetchone()[0] == 1


def test_candidate_accepts_normalized_snapshot_hash_without_convenience_key(conn):
    raw = candidate()
    del raw["snapshot_hash"]
    result = desk.record_candidate(conn, raw, cohort_id="cohort-U", now_iso=T0, cooldown_s=900)
    assert result["snapshot_hash"] == raw["snapshot"]["hash"]


def test_two_connections_racing_for_coin_create_one_primary(tmp_path):
    path = tmp_path / "race.sqlite"
    setup = desk.connect(path)
    cohort(setup)
    cohort(setup, "H")
    setup.close()
    barrier = Barrier(2)

    def enter(producer):
        c = desk.connect(path)
        try:
            barrier.wait(timeout=5)
            return opening(c, producer.lower() + "-race", producer=producer)
        finally:
            c.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        result = list(workers.map(enter, ("U", "H")))
    assert sorted(r["is_primary"] for r in result) == [False, True]
    assert result[0]["ticket_id"] == result[1]["ticket_id"]
    verify = desk.connect(path)
    assert verify.execute("SELECT COUNT(*) FROM desk_ticket_heads").fetchone()[0] == 1
    assert verify.execute("SELECT COUNT(*) FROM desk_candidate_emissions").fetchone()[0] == 2
    verify.close()


@pytest.mark.parametrize("state", ["open", "waiting", "queued", "unresolved", "filled"])
def test_active_ticket_never_reopens_after_cooldown(conn, state):
    opening(conn)
    conn.execute("UPDATE desk_ticket_heads SET state=? WHERE ticket_id='u-1'", (state,))
    conn.commit()
    later = opening(conn, "u-2", seconds=2000)
    assert later["ticket_id"] == "u-1" and later["is_primary"] is False


def test_terminal_coin_keeps_seat_cooldown_without_sighting_extending_it(conn):
    opening(conn)
    stage(conn, verdict="SKIP", reason="weak", ts=at(100), response_at=at(99))
    early = opening(conn, "u-2", seconds=999)
    assert early["is_primary"] is False and early["reason"] == "cooldown"
    later = opening(conn, "u-3", seconds=1000)
    assert later["is_primary"] is True and later["ticket_id"] == "u-3"
    assert conn.execute("SELECT COUNT(*) FROM desk_candidate_emissions").fetchone()[0] == 3


def test_coin_identity_includes_chain(conn):
    opening(conn)
    second = candidate("eth-2")
    second["network_id"] = "1"
    result = desk.record_candidate(conn, second, cohort_id="cohort-U", now_iso=T0, cooldown_s=900)
    assert result["is_primary"] is True


def test_stage_is_one_judgment_with_an_immutable_response_clock(conn):
    opening(conn)
    row = stage(conn, latency_ms=37, cost_usd=0)
    assert stage(conn, latency_ms=37, cost_usd=0) == row
    assert row["response_at"] == at(4) and row["ts"] == at(5)
    with pytest.raises(ValueError, match="stage_conflict"):
        stage(conn, verdict="SKIP", latency_ms=37, cost_usd=0)
    with pytest.raises(ValueError, match="stage_conflict"):
        stage(conn, response_at=at(8), latency_ms=37, cost_usd=0)
    assert desk.get_ticket(conn, "u-1")["stages"][0]["verdict"] == "ENTER"


def test_identical_coin_seat_inputs_cannot_be_asked_again_in_new_episode(conn):
    opening(conn)
    stage(conn, verdict="SKIP", reason="weak")
    opening(conn, "u-2", seconds=1000)
    with pytest.raises(ValueError, match="inputs_already_judged"):
        stage(conn, "u-2", ts=at(1005), response_at=at(1004))
    assert conn.execute("SELECT COUNT(*) FROM desk_tickets").fetchone()[0] == 1


def test_null_cost_and_latency_stay_unknown_and_gate_metadata_is_retained(conn):
    opening(conn, producer="H")
    row = stage(conn, stage="hard_filter", seat="code:safety", verdict="PASS", output=None,
                response_at=None, gate_state="pass", gate_inputs={"swaps_since_source_5s": 2},
                gate_evaluated_at=at(5))
    assert row["cost_usd"] is None and row["latency_ms"] is None
    assert row["gate_state"] == "pass" and row["gate_evaluated_at"] == at(5)
    assert json.loads(row["gate_inputs_json"]) == {"swaps_since_source_5s": 2}
    assert desk.get_ticket(conn, "u-1")["state"] == "open"


def test_stage_and_outbox_rollback_together_on_sql_failure(conn):
    opening(conn)
    conn.execute("CREATE TRIGGER reject_outbox BEFORE INSERT ON desk_outbox "
                 "BEGIN SELECT RAISE(ABORT, 'injected_outbox_failure'); END")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="injected_outbox_failure"):
        stage(conn, stage="size", seat="code:size", outbox=request())
    assert conn.execute("SELECT COUNT(*) FROM desk_tickets").fetchone()[0] == 0
    assert desk.list_outbox(conn) == []
    assert desk.get_ticket(conn, "u-1")["state"] == "open"


def test_outbox_request_is_fixed_and_pending_only_across_reopen(tmp_path):
    path = tmp_path / "desk.sqlite"
    c = desk.connect(path)
    cohort(c)
    opening(c)
    row = stage(c, stage="size", seat="code:size", outbox=request())
    assert row["intent_id"] == "intent-1"
    c.close()
    c = desk.connect(path)
    queued = desk.list_outbox(c)[0]
    assert queued["payload"] == request()["payload"]
    assert queued["expires_at"] == at(35) and queued["requested_at"] == at(5)
    assert desk.get_ticket(c, "u-1")["state"] == "queued"
    assert stage(c, stage="size", seat="code:size", outbox=request()) == row
    changed = request()
    changed["expires_at"] = at(999)
    with pytest.raises(ValueError, match="outbox_conflict"):
        stage(c, stage="size", seat="code:size", outbox=changed)
    assert desk.list_outbox(c)[0]["expires_at"] == at(35)
    desk.resolve_outbox(c, "u-1", state="sent", intent_id="intent-1", now_iso=at(6))
    assert desk.list_outbox(c) == []
    assert desk.get_ticket(c, "u-1")["state"] == "unresolved"
    with pytest.raises(ValueError, match="outbox_resolution_conflict"):
        desk.resolve_outbox(c, "u-1", state="pending", now_iso=at(7))
    c.close()


def test_resolution_and_head_change_are_atomic(conn):
    opening(conn)
    stage(conn, stage="size", seat="code:size", outbox=request())
    conn.execute("CREATE TRIGGER reject_head BEFORE UPDATE ON desk_ticket_heads "
                 "BEGIN SELECT RAISE(ABORT, 'injected_head_failure'); END")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="injected_head_failure"):
        desk.resolve_outbox(conn, "u-1", state="expired", now_iso=at(40))
    assert desk.list_outbox(conn)[0]["state"] == "pending"
    assert desk.get_ticket(conn, "u-1")["state"] == "queued"


def test_unknown_fill_stays_active_and_position_link_is_retained(conn):
    opening(conn)
    stage(conn, stage="fill", seat="code:fill", verdict="HOLD", output={"state": "unresolved"},
          intent_id="intent-1", wait_state="triggered_submitted_unknown", trigger_at=at(6))
    assert desk.get_ticket(conn, "u-1")["state"] == "unresolved"
    assert opening(conn, "h-2", producer="H", seconds=2000)["ticket_id"] == "u-1"
    row = desk.get_ticket(conn, "u-1")["stages"][0]
    assert row["wait_state"] == "triggered_submitted_unknown" and row["response_at"] == at(4)
    assert row["trigger_at"] == at(6) and row["intent_id"] == "intent-1"


def test_arm_admissions_have_independent_denominators_and_immutable_decisions(conn):
    opening(conn)
    expected = [("F", "latched"), ("SJ", "taken"), ("SR", "skipped_full")]
    for arm, decision in expected:
        first = desk.record_admission(conn, ticket_id="u-1", arm=arm, decision=decision, decided_at=at(6))
        assert desk.record_admission(conn, ticket_id="u-1", arm=arm, decision=decision, decided_at=at(6)) == first
    rows = conn.execute("SELECT arm, decision FROM desk_arm_admissions ORDER BY arm").fetchall()
    assert [tuple(row) for row in rows] == expected
    with pytest.raises(ValueError, match="admission_conflict"):
        desk.record_admission(conn, ticket_id="u-1", arm="F", decision="taken", decided_at=at(6))
    with pytest.raises(ValueError, match="arm"):
        desk.record_admission(conn, ticket_id="u-1", arm="OTHER", decision="taken", decided_at=at(6))


def test_store_never_commits_a_callers_existing_transaction(conn):
    conn.execute("BEGIN")
    conn.execute("CREATE TABLE caller_wip (id INTEGER)")
    with pytest.raises(ValueError, match="transaction_active"):
        opening(conn)
    assert conn.in_transaction is True
    conn.rollback()
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='caller_wip'").fetchone() is None


@pytest.mark.parametrize("kwargs", [{"now_iso": "2026-10-21T12:00:00"}, {"cooldown_s": -1},
                                    {"cooldown_s": float("nan")}])
def test_invalid_clocks_and_cooldowns_do_not_write_evidence(conn, kwargs):
    args = {"cohort_id": "cohort-U", "now_iso": T0, "cooldown_s": 900}
    args.update(kwargs)
    with pytest.raises(ValueError):
        desk.record_candidate(conn, candidate(), **args)
    assert conn.execute("SELECT COUNT(*) FROM desk_candidate_emissions").fetchone()[0] == 0


def test_terminal_ticket_cannot_queue_a_new_enter(conn):
    opening(conn)
    stage(conn, verdict="SKIP", reason="weak")
    with pytest.raises(ValueError, match="ticket_terminal"):
        stage(conn, stage="size", seat="code:size", outbox=request())
    assert desk.get_ticket(conn, "u-1")["state"] == "skipped"
    assert desk.list_outbox(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM desk_tickets").fetchone()[0] == 1


def test_stale_skip_cannot_release_an_unknown_or_filled_coin(conn):
    opening(conn)
    stage(conn, stage="fill", seat="code:fill", verdict="HOLD", output={"state": "unresolved"})
    with pytest.raises(ValueError, match="ticket_active_execution"):
        stage(conn, stage="hard_filter", seat="code:late", verdict="SKIP", reason="late")
    assert desk.get_ticket(conn, "u-1")["state"] == "unresolved"
    assert opening(conn, "h-2", producer="H", seconds=2000)["is_primary"] is False


def test_stale_outbox_expiry_cannot_release_a_recorded_fill(conn):
    opening(conn)
    stage(conn, stage="size", seat="code:size", outbox=request())
    stage(conn, stage="fill", seat="code:fill", verdict="PASS", output={"state": "filled"}, position_id=42)
    with pytest.raises(ValueError, match="ticket_active_execution"):
        desk.resolve_outbox(conn, "u-1", state="expired", now_iso=at(40))
    assert desk.get_ticket(conn, "u-1")["state"] == "filled"
    assert desk.get_ticket(conn, "u-1")["stages"][-1]["position_id"] == 42
    assert desk.list_outbox(conn)[0]["state"] == "pending"


def test_only_size_enter_can_create_a_handoff(conn):
    opening(conn)
    with pytest.raises(ValueError, match="outbox_stage"):
        stage(conn, stage="hard_filter", seat="code:safety", verdict="SKIP", outbox=request())
    assert desk.list_outbox(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM desk_tickets").fetchone()[0] == 0


def test_ticket_returns_primary_candidate_after_cross_producer_sighting(conn):
    opening(conn)
    opening(conn, "h-2", producer="H", seconds=1, value=42000)
    stored = desk.get_ticket(conn, "u-1")["candidate"]
    expected = candidate()
    del expected["snapshot_hash"]
    assert stored == expected
    assert stored["producer"] == "U" and stored["snapshot"]["fields"]["mcap_usd"]["value"] == 41000


def test_list_intents_reads_sent_requests_for_reconciliation_without_pending_or_refused(conn):
    opening(conn)
    stage(conn, stage="size", seat="code:size", outbox=request())
    opening(conn, "u-2", mint="MintB")
    second = request("intent-2")
    stage(conn, "u-2", stage="size", seat="code:size", outbox=second)
    opening(conn, "u-3", mint="MintC")
    third = request("intent-3")
    stage(conn, "u-3", stage="size", seat="code:size", outbox=third)
    desk.resolve_outbox(conn, "u-1", state="sent", now_iso=at(6))
    desk.resolve_outbox(conn, "u-3", state="refused", now_iso=at(6))
    sent = desk.list_intents(conn, limit=1)
    assert [(row["ticket_id"], row["intent_id"], row["state"]) for row in sent] == [
        ("u-1", "intent-1", "sent")]
    assert sent[0]["payload"] == request()["payload"] and sent[0]["expires_at"] == at(35)
    assert [row["ticket_id"] for row in desk.list_outbox(conn)] == ["u-2"]
    assert desk.list_intents(conn, limit=0) == []


def test_settled_intents_do_not_starve_later_reconciliation(conn):
    opening(conn)
    stage(conn, stage="size", seat="code:size", outbox=request())
    desk.resolve_outbox(conn, "u-1", state="sent", now_iso=at(6))
    stage(conn, stage="fill", seat="code:fill", verdict="PASS", output={"state": "filled"}, position_id=42)
    opening(conn, "u-2", mint="MintB", seconds=1)
    later = request("intent-2")
    later["requested_at"] = at(7)
    stage(conn, "u-2", stage="size", seat="code:size", outbox=later)
    desk.resolve_outbox(conn, "u-2", state="sent", now_iso=at(8))
    assert [row["ticket_id"] for row in desk.list_intents(conn, limit=1)] == ["u-2"]


def test_get_cohort_returns_the_frozen_parsed_policy(conn):
    stored = desk.get_cohort(conn, "cohort-U")
    assert stored["policy"] == {"clip_usd": 25, "max": 5}
    assert (stored["producer"], stored["prompt_version"], stored["created_at"]) == (
        "U", "desk-judge-v6", T0)
    assert desk.get_cohort(conn, "unknown") is None


def test_unfinished_enter_decisions_are_recoverable_without_the_original_model_answer(conn):
    opening(conn)
    stage(conn, stage="vet", seat="judge", inputs={"response_hash": "fixed", "expires_at": at(35)},
          output={"action": "ENTER", "rails": {"tp_pct": 60, "sl_pct": 20, "trail_pct": None}})
    opening(conn, "u-2", mint="MintB")
    stage(conn, "u-2", stage="vet", seat="judge", verdict="SKIP", reason="weak")
    heads = desk.list_undispatched_decisions(conn, limit=1)
    assert [head["ticket_id"] for head in heads] == ["u-1"]
    assert heads[0]["stages"][0]["inputs"]["expires_at"] == at(35)
    assert heads[0]["stages"][0]["output"]["rails"] == {"tp_pct": 60, "sl_pct": 20, "trail_pct": None}
    assert desk.list_undispatched_decisions(conn, limit=0) == []
    stage(conn, stage="size", seat="code:size", outbox=request())
    assert desk.list_undispatched_decisions(conn) == []


@pytest.mark.parametrize("state", ["unresolved", "filled", "killed", "skipped", "refused", "expired", "closed"])
def test_recovery_never_reopens_owned_or_terminal_decisions(conn, state):
    opening(conn)
    stage(conn, stage="vet", seat="judge")
    conn.execute("UPDATE desk_ticket_heads SET state=? WHERE ticket_id='u-1'", (state,))
    conn.commit()
    assert desk.list_undispatched_decisions(conn) == []


@pytest.mark.parametrize("table", ["memecoin_positions", "memecoin_pending_entries", "memecoin_manual_intents"])
def test_connect_refuses_a_funded_database_before_changing_journal_or_schema(tmp_path, table):
    path = tmp_path / "funded.sqlite"
    original = sqlite3.connect(path)
    original.execute(f"CREATE TABLE {table} (id INTEGER)")
    original.commit()
    assert original.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    original.close()
    with pytest.raises(ValueError, match="funded_database"):
        desk.connect(path)
    verify = sqlite3.connect(path)
    assert verify.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert verify.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [(table,)]
    verify.close()


def close_stage(conn, ticket_id="u-1", **kwargs):
    proof = {"state": "closed", "position_id": 42, "is_open": 0, "closed_at": at(100)}
    args = {"stage": "close", "seat": "engine", "verdict": "EXIT", "reason": "position_closed",
            "inputs": {"position_id": 42}, "output": proof, "ts": at(100), "response_at": None,
            "position_id": 42, "ticket_state": "closed"}
    args.update(kwargs)
    return stage(conn, ticket_id, **args)


def test_filled_ticket_reader_is_bounded_and_links_the_immutable_fill_position(conn):
    opening(conn)
    stage(conn, stage="fill", seat="engine", verdict="PASS", output={"state": "filled"}, position_id=42)
    opening(conn, "u-2", mint="MintB")
    stage(conn, "u-2", stage="fill", seat="engine", verdict="PASS", output={"state": "filled"}, position_id=43)
    opening(conn, "u-3", mint="MintC")
    heads = desk.list_filled_tickets(conn, limit=1)
    assert [(head["ticket_id"], head["position_id"]) for head in heads] == [("u-1", 42)]
    assert heads[0]["stages"][0]["position_id"] == 42
    assert desk.list_filled_tickets(conn, limit=0) == []


def test_confirmed_close_releases_the_coin_after_cooldown_without_changing_its_fill(conn):
    opening(conn)
    fill = stage(conn, stage="fill", seat="engine", verdict="PASS", output={"state": "filled"}, position_id=42)
    closed = close_stage(conn)
    assert close_stage(conn) == closed
    head = desk.get_ticket(conn, "u-1")
    assert head["state"] == "closed" and head["stages"][0] == fill
    assert head["stages"][1]["verdict"] == "EXIT"
    assert desk.list_filled_tickets(conn) == []
    assert opening(conn, "u-2", seconds=999)["reason"] == "cooldown"
    assert opening(conn, "u-3", seconds=1000)["is_primary"] is True


@pytest.mark.parametrize("kwargs", [
    {"position_id": None}, {"position_id": 43}, {"position_id": True},
    {"seat": "code"}, {"verdict": "SKIP"}, {"ticket_state": None},
    {"stage": "risk"}, {"output": None},
    {"output": {"state": "closed", "position_id": 42, "is_open": 1, "closed_at": at(100)}},
    {"output": {"state": "closed", "position_id": 42, "is_open": None, "closed_at": at(100)}},
    {"output": {"state": "closed", "position_id": 43, "is_open": 0, "closed_at": at(100)}},
    {"output": {"state": "closed", "position_id": 42, "is_open": 0, "closed_at": None}},
    {"output": {"state": "closed", "position_id": 42, "is_open": 0, "closed_at": "2026-10-21T12:00:00"}},
])
def test_missing_mismatched_or_non_engine_close_proof_cannot_release_a_fill(conn, kwargs):
    opening(conn)
    stage(conn, stage="fill", seat="engine", verdict="PASS", output={"state": "filled"}, position_id=42)
    with pytest.raises(ValueError):
        close_stage(conn, **kwargs)
    assert desk.get_ticket(conn, "u-1")["state"] == "filled"
    assert len(desk.get_ticket(conn, "u-1")["stages"]) == 1


def test_close_proof_without_a_confirmed_fill_cannot_release_an_unknown_position(conn):
    opening(conn)
    stage(conn, stage="fill", seat="engine", verdict="HOLD", output={"state": "unresolved"})
    with pytest.raises(ValueError, match="close_proof"):
        close_stage(conn)
    assert desk.get_ticket(conn, "u-1")["state"] == "unresolved"
    assert desk.list_filled_tickets(conn) == []


def test_outbox_acknowledgment_cannot_substitute_for_confirmed_position_close(conn):
    opening(conn)
    stage(conn, stage="size", seat="code:size", outbox=request())
    with pytest.raises(ValueError, match="close_proof"):
        desk.resolve_outbox(conn, "u-1", state="sent", now_iso=at(6), ticket_state="closed")
    assert desk.get_ticket(conn, "u-1")["state"] == "queued"
    assert desk.list_outbox(conn)[0]["state"] == "pending"
