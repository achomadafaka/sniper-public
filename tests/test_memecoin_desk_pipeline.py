"""Offline desk funnel over separate real temporary SQLite databases."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from sniper.persistence import db_memecoin as ledger
from sniper.persistence import db_memecoin_desk as store
from sniper.services import memecoin_manual_intents as intents
from sniper.settings import memecoin as cfg
from test_memecoin_desk_contract import candidate, evidence, policy, verdict, AT, BOUNDS

try:
    from sniper.services import memecoin_desk as desk
except ImportError:
    desk = None

START = AT
RESPONSE = "2026-10-21T12:00:01+00:00"
EXPIRES = "2026-10-21T12:00:21+00:00"
LATER = "2026-10-21T12:00:30+00:00"
POLICY = {"clip_usd": 25, "max_open": 5, "daily_halt_usd": 200,
          "cumulative_kill_usd": -200, "hard_filter": policy(), "rail_bounds": BOUNDS,
          "enter_ttl_max_s": 30, "boundary_max_s": 120, "wait_max_s": 300}


def api():
    assert desk is not None, "offline desk facade is missing"
    return desk


@pytest.fixture
def databases(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_CHAINS", frozenset({"solana"}))
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_CLIP_USD", 25.0)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_MAX_CONCURRENT", 5)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_DAILY_LOSS_USD", 200.0)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_RAIL_BOUNDS", BOUNDS)
    monkeypatch.setattr(intents, "send_wake", lambda *_: None)  # Never wake the real runner.
    slow = store.connect(tmp_path / "desk.db")
    funded = sqlite3.connect(tmp_path / "ledger.db")
    funded.row_factory = sqlite3.Row
    ledger.ensure_memecoin_tables(funded)
    yield slow, funded
    slow.close()
    funded.close()


def provision(db):
    return api().provision_cohort(*db, producer="U", policy=POLICY,
                                  prompt_version="desk-judge-v6", created_at=AT)


def ready(db):
    cohort = provision(db)
    ticket = api().ingest_candidate(db[0], candidate(), cohort_id=cohort, now_iso=AT)
    api().evaluate_candidate(db[0], ticket["ticket_id"], evidence=evidence(), policy=policy(), now_iso=AT)
    return cohort, ticket["ticket_id"]


def decide(db, ticket, raw=None, **kwargs):
    return api().record_decision(*db, ticket, verdict() if raw is None else raw,
        bounds=kwargs.pop("bounds", BOUNDS), elapsed_ms=kwargs.pop("elapsed_ms", 1000),
        call_started_at=kwargs.pop("call_started_at", START), response_at=kwargs.pop("response_at", RESPONSE),
        now_iso=kwargs.pop("now_iso", RESPONSE), expires_at=kwargs.pop("expires_at", None),
        model_cost_usd=kwargs.pop("model_cost_usd", 0),
        cost_provenance=kwargs.pop("cost_provenance", "test:cached-covered"), **kwargs)


def test_cohort_provision_heals_ledger_first_partial_write(databases):
    slow, funded = databases
    cohort = ledger.register_desk_cohort(funded, producer="U", policy=POLICY,
        prompt_version="desk-judge-v6", created_at=AT)
    assert provision(databases) == cohort
    assert provision(databases) == cohort
    assert store.get_cohort(slow, cohort)["policy"] == json.loads(json.dumps(POLICY))


def test_ingestion_records_only_a_candidate_in_dedicated_store(databases):
    cohort = provision(databases)
    ticket = api().ingest_candidate(databases[0], candidate(), cohort_id=cohort, now_iso=AT)
    assert ticket["is_primary"] is True and ticket["state"] == "open"
    assert store.list_outbox(databases[0]) == []
    assert intents.pending_count(databases[1]) == 0
    assert not databases[1].execute("SELECT name FROM sqlite_master WHERE name='desk_ticket_heads'").fetchall()


def test_ingestion_never_implicitly_provisions_a_cohort(databases):
    with pytest.raises(ValueError, match="cohort_unknown"):
        api().ingest_candidate(databases[0], candidate(), cohort_id="missing", now_iso=AT)


def test_later_sighting_is_retained_without_another_judge_ticket(databases):
    cohort = provision(databases)
    first = api().ingest_candidate(databases[0], candidate(), cohort_id=cohort, now_iso=AT)
    later = candidate()
    later["ticket_id"] = "desk:U:later"
    seen = api().ingest_candidate(databases[0], later, cohort_id=cohort, now_iso=RESPONSE)
    assert seen["is_primary"] is False and seen["ticket_id"] == first["ticket_id"]
    with pytest.raises(ValueError, match="ticket_unknown"):
        api().evaluate_candidate(databases[0], "desk:U:later", evidence=evidence(), policy=policy(), now_iso=RESPONSE)


def test_hard_filter_cannot_use_a_policy_other_than_the_frozen_cohort(databases):
    cohort = provision(databases)
    ticket = api().ingest_candidate(databases[0], candidate(), cohort_id=cohort, now_iso=AT)
    with pytest.raises(ValueError, match="cohort_policy"):
        api().evaluate_candidate(databases[0], ticket["ticket_id"], evidence=evidence(),
                                 policy={**policy(), "top10_pct_max": 100}, now_iso=AT)


def test_judge_cannot_bypass_the_hard_filter(databases):
    cohort = provision(databases)
    ticket = api().ingest_candidate(databases[0], candidate(), cohort_id=cohort, now_iso=AT)
    with pytest.raises(ValueError, match="hard_filter_required"):
        decide(databases, ticket["ticket_id"])
    assert store.list_outbox(databases[0]) == []


def test_missing_safety_is_a_persisted_skip_with_no_model_stage(databases):
    cohort = provision(databases)
    ticket = api().ingest_candidate(databases[0], candidate(), cohort_id=cohort, now_iso=AT)
    result = api().evaluate_candidate(databases[0], ticket["ticket_id"], evidence={}, policy=policy(), now_iso=AT)
    assert result["output"]["reason"] == "unknown:evidence:identity"
    with pytest.raises(ValueError, match="hard_filter_required"):
        decide(databases, ticket["ticket_id"])
    assert len(store.get_ticket(databases[0], ticket["ticket_id"])["stages"]) == 1


def test_enter_handoff_freezes_exact_identity_rails_and_original_expiry(databases):
    cohort, ticket = ready(databases)
    result = decide(databases, ticket)
    assert result["action"] == "ENTER"
    queued = store.list_outbox(databases[0])
    assert len(queued) == 1
    request = queued[0]
    assert request["requested_at"] == RESPONSE and request["expires_at"] == EXPIRES
    assert request["payload"]["desk_metadata"] == {"cohort_id": cohort, "producer": "U",
        "opportunity_id": ticket, "snapshot_hash": candidate()["snapshot"]["hash"],
        "verdict_price": api().verdict_price(candidate(), clip_usd=25)}
    assert request["payload"]["desk_metadata"]["verdict_price"]["cap_pct"] == 3 + 1.25 + 25 / 5000 * 100
    assert request["payload"]["rails"] == {"tp_pct": 60, "sl_pct": 20, "trail_pct": None}
    assert request["payload"]["size_usd"] == 25
    dispatched = api().dispatch_outbox(*databases, now_iso=RESPONSE)
    assert dispatched[0]["state"] == "sent"
    row = intents.read_status(databases[1], request["intent_id"])
    assert row["requested_at"] == RESPONSE and row["expires_at"] == EXPIRES
    assert row["size_usd"] == 25 and row["tp_pct"] == 60
    assert api().dispatch_outbox(*databases, now_iso=LATER) == []
    assert intents.pending_count(databases[1]) == 1


@pytest.mark.parametrize("change", [
    {"enter_ttl_max_s": None}, {"boundary_max_s": None}, {"wait_max_s": None},
    {"enter_ttl_max_s": True}, {"enter_ttl_max_s": 2}, {"enter_ttl_max_s": 31},
    {"boundary_max_s": 4}, {"boundary_max_s": 121},
    {"wait_max_s": 9}, {"wait_max_s": 301}, {"enter_ttl_s": 20},
])
def test_timing_policy_is_validated_before_either_cohort_commit(databases, change):
    frozen = {**POLICY, **change}
    for key in tuple(frozen):
        if frozen[key] is None:
            del frozen[key]
    with pytest.raises(ValueError, match="timing|policy"):
        api().provision_cohort(*databases, producer="U", policy=frozen,
            prompt_version="desk-judge-v6", created_at=AT)
    assert databases[0].execute("SELECT COUNT(*) FROM desk_cohorts").fetchone()[0] == 0
    assert databases[1].execute("SELECT COUNT(*) FROM memecoin_desk_cohorts").fetchone()[0] == 0


@pytest.mark.parametrize("ttl, next_check", [(3, 5), (7, 17), (30, 120)])
def test_model_timing_reaches_the_intent_and_frozen_audit(databases, monkeypatch, ttl, next_check):
    _, ticket = ready(databases)
    raw = verdict(enter_ttl_s=ttl, next_check_s=next_check)
    result = decide(databases, ticket, raw)
    expected_expiry = (datetime.fromisoformat(RESPONSE) + timedelta(seconds=ttl)).isoformat()
    assert result["enter_ttl_s"] == ttl and result["next_check_s"] == next_check
    vet = next(s for s in store.get_ticket(databases[0], ticket)["stages"] if s["stage"] == "vet")
    assert vet["inputs"]["timing_caps"] == {k: POLICY[k] for k in
        ("enter_ttl_max_s", "boundary_max_s", "wait_max_s")}
    assert vet["inputs"]["expires_at"] == expected_expiry
    assert vet["output"]["result"]["next_check_s"] == next_check
    original = intents.enqueue
    sent = []
    def observe(conn, **kwargs):
        sent.append((kwargs["ttl_s"], kwargs["now_iso"]))
        return original(conn, **kwargs)
    monkeypatch.setattr(intents, "enqueue", observe)
    assert api().dispatch_outbox(*databases, now_iso=RESPONSE)[0]["state"] == "sent"
    request = store.list_intents(databases[0])[0]
    assert sent == [(ttl, RESPONSE)]
    assert intents.read_status(databases[1], request["intent_id"])["expires_at"] == expected_expiry


@pytest.mark.parametrize("action", ["E", "W", "S"])
@pytest.mark.parametrize("field", ["enter_ttl_s", "next_check_s"])
def test_missing_model_timing_is_a_charged_durable_skip(databases, action, field):
    cohort, ticket = ready(databases)
    raw = verdict(action, enter_ttl_s=20, next_check_s=30,
                  e=["tr", ">=", 2, 1, 10] if action == "W" else None)
    del raw[field]
    result = decide(databases, ticket, raw, model_cost_usd=.02)
    assert result["action"] == "SKIP" and result["reason"] == "verdict_keys"
    assert store.get_ticket(databases[0], ticket)["stages"][-1]["verdict"] == "SKIP"
    assert ledger.desk_cohort_accounting(databases[1], cohort)["known_cost_usd"] == .02
    assert store.list_outbox(databases[0]) == []


@pytest.mark.parametrize("change", [{"enter_ttl_s": True}, {"enter_ttl_s": 31},
    {"next_check_s": None}, {"next_check_s": 121}])
def test_bad_model_timing_is_a_charged_durable_skip(databases, change):
    cohort, ticket = ready(databases)
    raw = verdict(enter_ttl_s=20, next_check_s=30)
    raw.update(change)
    result = decide(databases, ticket, raw, model_cost_usd=.02)
    assert result["action"] == "SKIP" and result["reason"] == "timing_out_of_bounds"
    assert ledger.desk_cohort_accounting(databases[1], cohort)["known_cost_usd"] == .02
    assert store.list_outbox(databases[0]) == []


@pytest.mark.parametrize("change", [{"enter_ttl_s": 8}, {"next_check_s": 12}])
def test_model_timing_uses_the_smaller_frozen_cohort_caps(databases, change):
    frozen = {**POLICY, "enter_ttl_max_s": 7, "boundary_max_s": 11, "wait_max_s": 90}
    cohort = api().provision_cohort(*databases, producer="U", policy=frozen,
        prompt_version="desk-judge-v6", created_at=AT)
    ticket = api().ingest_candidate(databases[0], candidate(), cohort_id=cohort, now_iso=AT)["ticket_id"]
    api().evaluate_candidate(databases[0], ticket, evidence=evidence(), policy=policy(), now_iso=AT)
    raw = verdict(enter_ttl_s=7, next_check_s=11)
    raw.update(change)
    result = decide(databases, ticket, raw, model_cost_usd=.02)
    assert result["action"] == "SKIP" and result["reason"] == "timing_out_of_bounds"
    assert ledger.desk_cohort_accounting(databases[1], cohort)["known_cost_usd"] == .02
    assert store.list_outbox(databases[0]) == []


@pytest.mark.parametrize("cap, expiry, reason", [(90, 90, "cond_unavailable"),
    (90, 91, "cond_out_of_range"), (300, 300, "cond_unavailable")])
def test_wait_uses_its_frozen_cap_without_enabling_a_bar_executor(databases, cap, expiry, reason):
    frozen = {**POLICY, "wait_max_s": cap}
    cohort = api().provision_cohort(*databases, producer="U", policy=frozen,
        prompt_version="desk-judge-v6", created_at=AT)
    ticket = api().ingest_candidate(databases[0], candidate(), cohort_id=cohort, now_iso=AT)["ticket_id"]
    api().evaluate_candidate(databases[0], ticket, evidence=evidence(), policy=policy(), now_iso=AT)
    result = decide(databases, ticket, verdict("W", e=["tr", ">=", 2, 1, expiry]), model_cost_usd=.02)
    assert result["action"] == "SKIP" and result["reason"] == reason
    assert ledger.desk_cohort_accounting(databases[1], cohort)["known_cost_usd"] == .02
    assert store.list_outbox(databases[0]) == []


@pytest.mark.parametrize("expiry_assertion", [EXPIRES, START, "bad timestamp", True])
def test_expiry_assertion_cannot_override_model_timing_or_erase_the_charge(databases, expiry_assertion):
    cohort, ticket = ready(databases)
    result = decide(databases, ticket, verdict(enter_ttl_s=7, next_check_s=17),
                    expires_at=expiry_assertion, model_cost_usd=.02)
    assert result["action"] == "SKIP" and result["reason"] == "expiry_conflict"
    vet = store.get_ticket(databases[0], ticket)["stages"][-1]
    assert vet["stage"] == "vet" and vet["verdict"] == "SKIP"
    assert vet["inputs"]["expires_at"] == "2026-10-21T12:00:08+00:00"
    assert ledger.desk_cohort_accounting(databases[1], cohort)["known_cost_usd"] == .02
    assert store.list_outbox(databases[0]) == []


def test_matching_expiry_assertion_and_omission_replay_the_same_frozen_request(databases):
    _, ticket = ready(databases)
    raw = verdict(enter_ttl_s=7, next_check_s=17)
    first = decide(databases, ticket, raw, expires_at="2026-10-21T12:00:08+00:00")
    assert decide(databases, ticket, raw, now_iso=LATER) == first
    assert len(store.list_outbox(databases[0])) == 1


def test_recovery_uses_model_ttl_from_the_original_response_clock(databases, monkeypatch):
    _, ticket = ready(databases)
    original = store.record_stage
    def crash(conn, **kwargs):
        if kwargs["stage"] == "size":
            raise RuntimeError("before timed outbox")
        return original(conn, **kwargs)
    monkeypatch.setattr(store, "record_stage", crash)
    with pytest.raises(RuntimeError, match="before timed outbox"):
        decide(databases, ticket, verdict(enter_ttl_s=7, next_check_s=17))
    monkeypatch.setattr(store, "record_stage", original)
    assert api().dispatch_outbox(*databases, now_iso="2026-10-21T12:00:06+00:00")[0]["state"] == "sent"
    request = store.list_intents(databases[0])[0]
    assert request["requested_at"] == RESPONSE
    assert request["expires_at"] == "2026-10-21T12:00:08+00:00"
    assert decide(databases, ticket, verdict(enter_ttl_s=7, next_check_s=17), now_iso=LATER)["action"] == "ENTER"
    assert intents.pending_count(databases[1]) == 1


@pytest.mark.parametrize("raw,reason", [(verdict("S"), "model_skip"),
    (verdict("W", e=["tr", ">=", 2, 1, 10]), "cond_unavailable"),
    (verdict(k="wrong"), "key_mismatch"), ("bad json", "malformed_json")])
def test_invalid_or_nonenter_decision_never_creates_an_outbox(databases, raw, reason):
    _, ticket = ready(databases)
    result = decide(databases, ticket, raw)
    assert result["action"] == "SKIP" and result["reason"] == reason
    assert store.list_outbox(databases[0]) == []
    assert intents.pending_count(databases[1]) == 0


def test_call_deadline_does_not_restart_when_recording_a_late_result(databases):
    _, ticket = ready(databases)
    assert decide(databases, ticket, elapsed_ms=12001)["reason"] == "judge_timeout"
    assert store.list_outbox(databases[0]) == []


@pytest.mark.parametrize("change", ["verdict", "cost", "expiry", "response", "bounds"])
def test_replay_cannot_change_a_frozen_verdict_or_call_metadata(databases, change):
    _, ticket = ready(databases)
    decide(databases, ticket)
    kwargs = {}
    raw = None
    if change == "verdict": raw = verdict(r=[70, 20, None])
    elif change == "cost": kwargs["model_cost_usd"] = 0.01
    elif change == "expiry": kwargs["expires_at"] = LATER
    elif change == "response": kwargs.update(response_at="2026-10-21T12:00:02+00:00", now_iso=LATER)
    else: kwargs["bounds"] = {**BOUNDS, "tp_pct": (15, 200)}
    with pytest.raises(ValueError, match="conflict|cohort_policy"):
        decide(databases, ticket, raw, **kwargs)
    assert len(store.list_outbox(databases[0])) == 1


def test_identical_decision_replay_retains_one_outbox(databases):
    _, ticket = ready(databases)
    first = decide(databases, ticket)
    assert decide(databases, ticket, now_iso=LATER) == first
    assert len(store.list_outbox(databases[0])) == 1
    assert len(store.get_ticket(databases[0], ticket)["stages"]) == 3


def test_unknown_model_charge_blocks_dispatch_until_exactly_priced(databases):
    cohort, ticket = ready(databases)
    decide(databases, ticket, model_cost_usd=None, cost_provenance="test:unpriced")
    out = api().dispatch_outbox(*databases, now_iso=RESPONSE)
    assert out[0]["reason"] == "cohort_accounting_unknown"
    assert store.list_outbox(databases[0])[0]["state"] == "pending"
    assert intents.pending_count(databases[1]) == 0
    ledger.record_desk_cohort_cost(databases[1], cohort_id=cohort, event_id="desk-judge:" + ticket,
        usd=0, kind="model", occurred_at=RESPONSE, provenance="test:cached-covered")
    assert api().dispatch_outbox(*databases, now_iso=RESPONSE)[0]["state"] == "sent"


def test_skipped_model_call_still_counts_toward_cohort_kill(databases):
    cohort, ticket = ready(databases)
    decide(databases, ticket, verdict("S"), model_cost_usd=200, cost_provenance="test:bill")
    assert ledger.desk_cohort_killed(databases[1], cohort)
    assert store.list_outbox(databases[0]) == []


def test_crash_after_enqueue_commit_replays_existing_intent_after_expiry(databases, monkeypatch):
    _, ticket = ready(databases)
    decide(databases, ticket)
    request = store.list_outbox(databases[0])[0]
    real_ack = store.resolve_outbox
    def crash(*args, **kwargs):
        raise RuntimeError("crash after ledger commit")
    monkeypatch.setattr(store, "resolve_outbox", crash)
    with pytest.raises(RuntimeError, match="after ledger commit"):
        api().dispatch_outbox(*databases, now_iso=RESPONSE)
    assert intents.pending_count(databases[1]) == 1
    monkeypatch.setattr(store, "resolve_outbox", real_ack)
    replay = api().dispatch_outbox(*databases, now_iso=LATER)
    assert replay[0]["state"] == "sent"
    assert intents.read_status(databases[1], request["intent_id"])["expires_at"] == EXPIRES
    assert intents.pending_count(databases[1]) == 1


def test_crash_before_enqueue_never_extends_original_expiry(databases, monkeypatch):
    _, ticket = ready(databases)
    decide(databases, ticket)
    def crash(*args, **kwargs):
        raise RuntimeError("crash before enqueue")
    monkeypatch.setattr(intents, "enqueue", crash)
    with pytest.raises(RuntimeError, match="before enqueue"):
        api().dispatch_outbox(*databases, now_iso=RESPONSE)
    assert api().dispatch_outbox(*databases, now_iso=LATER)[0]["state"] == "expired"
    assert intents.pending_count(databases[1]) == 0


def test_unresolved_fill_stays_active_and_can_later_reconcile_terminal(databases):
    _, ticket = ready(databases)
    decide(databases, ticket)
    request = store.list_outbox(databases[0])[0]
    api().dispatch_outbox(*databases, now_iso=RESPONSE)
    databases[1].execute("UPDATE memecoin_manual_intents SET state='unresolved',result_status='submitted_unknown' WHERE intent_id=?",
                        (request["intent_id"],))
    databases[1].commit()
    out = api().reconcile_intents(*databases, now_iso=LATER)
    assert out[0]["state"] == "unresolved"
    assert store.get_ticket(databases[0], ticket)["state"] == "unresolved"
    assert not [s for s in store.get_ticket(databases[0], ticket)["stages"] if s["stage"] == "fill"]
    databases[1].execute("UPDATE memecoin_manual_intents SET state='refused',result_status='refused',reason='confirmed failed',resolved_at=? WHERE intent_id=?",
                        (LATER, request["intent_id"]))
    databases[1].commit()
    assert api().reconcile_intents(*databases, now_iso=LATER)[0]["state"] == "refused"
    stages = store.get_ticket(databases[0], ticket)["stages"]
    assert [s["verdict"] for s in stages if s["stage"] == "fill"] == ["SKIP"]
    assert api().reconcile_intents(*databases, now_iso=LATER) == []


@pytest.mark.parametrize("kwargs", [{"call_started_at": LATER}, {"response_at": LATER}])
def test_impossible_call_chronology_cannot_queue(databases, kwargs):
    _, ticket = ready(databases)
    with pytest.raises(ValueError, match="chronology"):
        decide(databases, ticket, **kwargs)
    assert store.list_outbox(databases[0]) == []


def test_cli_requires_explicit_desk_path_and_only_ingests_bounded_jsonl(databases, tmp_path):
    cohort = provision(databases)
    script = Path("scripts/memecoin_desk_ingest.py").resolve()
    assert script.exists(), "offline ingestion CLI is missing"
    path = tmp_path / "input.jsonl"
    path.write_text(json.dumps(candidate()) + "\n" + json.dumps(candidate()) + "\n")
    result = subprocess.run([sys.executable, str(script), "--desk-db", str(tmp_path / "desk.db"),
        "--cohort-id", cohort, "--input", str(path), "--now-iso", AT, "--max-rows", "1"],
        text=True, capture_output=True, timeout=10)
    assert result.returncode != 0 and "row_limit" in result.stdout + result.stderr
    assert intents.pending_count(databases[1]) == 0
    no_path = subprocess.run([sys.executable, str(script)], text=True, capture_output=True, timeout=10)
    assert no_path.returncode != 0 and "--desk-db" in no_path.stderr


def test_wall_deadline_cannot_be_hidden_by_a_small_supplied_elapsed(databases):
    _, ticket = ready(databases)
    result = decide(databases, ticket, elapsed_ms=1, response_at="2026-10-21T12:00:13+00:00", now_iso="2026-10-21T12:00:14+00:00")
    assert result["action"] == "SKIP" and result["reason"] == "judge_timeout"
    assert store.list_outbox(databases[0]) == []


@pytest.mark.parametrize("raw", [b"not-json", verdict(r=[float("nan"), 20, None])])
def test_malformed_response_can_be_persisted_as_a_skip_without_json_hash_failure(databases, raw):
    _, ticket = ready(databases)
    result = decide(databases, ticket, raw)
    assert result["action"] == "SKIP"
    assert store.list_outbox(databases[0]) == []


@pytest.mark.parametrize("elapsed", [True, float("nan"), float("inf"), -1, 10 ** 400])
def test_invalid_monotonic_elapsed_is_a_durable_skip_not_a_storage_failure(databases, elapsed):
    _, ticket = ready(databases)
    result = decide(databases, ticket, elapsed_ms=elapsed)
    assert result["action"] == "SKIP" and result["reason"] == "invalid_elapsed_ms"
    assert store.get_ticket(databases[0], ticket)["stages"][-1]["latency_ms"] is None


def test_expense_survives_a_crash_while_recording_the_model_stage(databases, monkeypatch):
    cohort, ticket = ready(databases)
    real_record = store.record_stage
    def crash(conn, **kwargs):
        if kwargs["stage"] == "vet":
            raise RuntimeError("crash at vet commit")
        return real_record(conn, **kwargs)
    monkeypatch.setattr(store, "record_stage", crash)
    with pytest.raises(RuntimeError, match="vet commit"):
        decide(databases, ticket, model_cost_usd=None, cost_provenance="test:unpriced")
    assert ledger.desk_cohort_refusal(databases[1], cohort_id=cohort, producer="U") == "cohort_accounting_unknown"


def test_dispatch_recovers_a_committed_enter_without_original_response(databases, monkeypatch):
    _, ticket = ready(databases)
    real_record = store.record_stage
    def crash(conn, **kwargs):
        if kwargs["stage"] == "size":
            raise RuntimeError("crash before outbox")
        return real_record(conn, **kwargs)
    monkeypatch.setattr(store, "record_stage", crash)
    with pytest.raises(RuntimeError, match="before outbox"):
        decide(databases, ticket)
    monkeypatch.setattr(store, "record_stage", real_record)
    assert store.list_outbox(databases[0]) == []
    assert api().dispatch_outbox(*databases, now_iso=RESPONSE)[0]["state"] == "sent"
    request = store.list_intents(databases[0])[0]
    assert intents.read_status(databases[1], request["intent_id"])["expires_at"] == EXPIRES


def test_h_filter_cannot_freeze_an_early_ungated_pass(databases):
    from sniper.services.memecoin_desk_contract import build_h_candidate
    from test_memecoin_desk_contract import receipt_inputs, NOW
    h = receipt_inputs()
    h["receipt"]["blockTime"] = NOW - 1
    c = build_h_candidate(**h)
    cohort = api().provision_cohort(*databases, producer="H", policy=POLICY,
        prompt_version="desk-judge-v6", created_at=AT)
    ticket = api().ingest_candidate(databases[0], c, cohort_id=cohort, now_iso=AT)
    with pytest.raises(ValueError, match="activity_window_pending"):
        api().evaluate_candidate(databases[0], ticket["ticket_id"], evidence=evidence(c), policy=policy(), now_iso=AT)
    assert store.get_ticket(databases[0], ticket["ticket_id"])["stages"] == []
    result = api().evaluate_candidate(databases[0], ticket["ticket_id"], evidence=evidence(c),
        policy=policy(), now_iso="2026-10-21T12:00:04+00:00")
    assert result["verdict"] == "PASS" and result["gate_state"] is None


def test_model_audit_retains_exact_bounded_response_and_typed_sheet(databases):
    _, ticket = ready(databases)
    raw = '{"k":"desk:U:fixture","v":"E","e":null,"r":[60,20,null],"c":1,"enter_ttl_s":20,"next_check_s":30}'
    result = decide(databases, ticket, raw, model="test-model")
    vet = next(s for s in store.get_ticket(databases[0], ticket)["stages"] if s["stage"] == "vet")
    assert vet["output"]["raw"] == raw
    assert vet["output"]["result"]["action"] == "ENTER"
    assert vet["inputs"]["sheet"]["mcap_usd"] == 41000
    assert vet["inputs"]["sheet"]["opportunity_id"] == ticket
    assert "mint" not in vet["inputs"]["sheet"] and vet["model"] == "test-model"


def test_oversized_model_output_cannot_enter_or_expand_the_audit_without_bound(databases):
    _, ticket = ready(databases)
    raw = json.dumps(verdict()) + " " * 8192
    result = decide(databases, ticket, raw)
    assert result["action"] == "SKIP" and result["reason"] == "response_too_large"
    vet = next(s for s in store.get_ticket(databases[0], ticket)["stages"] if s["stage"] == "vet")
    assert vet["output"]["raw"] is None and vet["output"]["raw_error"] == "response_too_large"
    assert store.list_outbox(databases[0]) == []


def test_facade_intent_reaches_frozen_funded_contract_and_unknown_never_rebuys(databases, monkeypatch):
    from sniper.services import memecoin_arming
    from sniper.services import memecoin_real_runner as runner
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(memecoin_arming.cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(runner, "_alert_entry_result", lambda *a, **k: None)
    cohort, ticket = ready(databases)
    decide(databases, ticket)
    request = store.list_outbox(databases[0])[0]
    api().dispatch_outbox(*databases, now_iso=RESPONSE)
    submits = []
    def execute(conn, pending_id, mint, size, now_iso, **kwargs):
        submits.append(pending_id)
        return {"status": "unknown", "reason": "confirmation unknown", "sig": "fixture-signed"}
    contexts = {"solana": {"usdc_fn": lambda: 300.0, "sol_fn": lambda: 1.0,
        "token_safety_fn": lambda mint, **kw: (True, "ok"), "execute_fn": execute, "kill_path": None}}
    stats = lambda mint, network_id=None: {"market_cap": 60000.0, "price": 0.0001, "liquidity_usd": 20000.0}
    buy = runner.make_real_buy_fn(contexts, stats_fn=stats)
    intents.drain(databases[1], buy, now_fn=lambda: RESPONSE)
    assert len(submits) == 1
    pending = dict(databases[1].execute("SELECT * FROM memecoin_pending_entries WHERE id=?", (submits[0],)).fetchone())
    frozen = json.loads(pending["funded_contract_json"])
    assert frozen["source"]["desk"]["cohort_id"] == cohort
    assert frozen["source"]["desk"]["opportunity_id"] == ticket
    assert (frozen["policy"]["tp_pct"], frozen["policy"]["stop_pct"], frozen["policy"]["trail_pct"]) == (60, 20, 0)
    ledger.validated_pending_funded_contract(databases[1], pending)
    intents.drain(databases[1], buy, now_fn=lambda: LATER)
    assert len(submits) == 1
    assert api().reconcile_intents(*databases, now_iso=LATER)[0]["state"] == "unresolved"
    # A confirmed recovery books the same frozen contract; no new execution is involved.
    position_id = ledger.open_position(databases[1], source_user_id=None, source_handle="fixture",
        mint=pending["mint"], ticker=None, entry_price=0.0001, entry_notional=25,
        entry_fill_source="chain", entry_lag_s=0, their_size_usd=0, opened_at=LATER,
        execution_mode="real", network_id="solana", signal_source="desk",
        strategy=pending["strategy"], funded_contract_json=pending["funded_contract_json"],
        funded_contract_hash=pending["funded_contract_hash"], funded_contract_version=pending["funded_contract_version"])
    recovered = ledger.get_position(databases[1], position_id)
    assert recovered["desk_cohort_id"] == cohort and recovered["funded_contract_json"] == pending["funded_contract_json"]
    assert intents.read_status(databases[1], request["intent_id"])["operation_state"] == "unresolved"


def test_provision_rejects_two_handles_for_the_same_database_file(databases, tmp_path):
    second = sqlite3.connect(tmp_path / "desk.db")
    second.row_factory = sqlite3.Row
    try:
        with pytest.raises(ValueError, match="separate_databases_required"):
            api().provision_cohort(databases[0], second, producer="U", policy=POLICY,
                prompt_version="desk-judge-v6", created_at=AT)
    finally:
        second.close()


@pytest.mark.parametrize("value", [float("nan"), {"receipt": "untrusted provider body"}])
def test_malformed_safety_proof_is_a_durable_skip_without_raw_receipt_storage(databases, value):
    cohort = provision(databases)
    ticket = api().ingest_candidate(databases[0], candidate(), cohort_id=cohort, now_iso=AT)
    ev = evidence()
    ev["checks"]["holders"]["top10_pct"] = value
    result = api().evaluate_candidate(databases[0], ticket["ticket_id"], evidence=ev, policy=policy(), now_iso=AT)
    assert result["verdict"] == "SKIP" and result["inputs"]["evidence"] is None
    assert store.list_outbox(databases[0]) == []


def filled_ticket(db):
    cohort, ticket = ready(db)
    decide(db, ticket)
    request = store.list_outbox(db[0])[0]
    api().dispatch_outbox(*db, now_iso=RESPONSE)
    frozen = json.dumps({"source": {"desk": request["payload"]["desk_metadata"]}})
    pid = ledger.open_position(db[1], source_user_id=None, source_handle="fixture", mint=candidate()["mint"],
        ticker=None, entry_price=0.0001, entry_notional=25, entry_fill_source="chain", entry_lag_s=0,
        their_size_usd=0, opened_at=RESPONSE, execution_mode="real", network_id="solana",
        signal_source="desk", funded_contract_json=frozen)
    intents.resolve(db[1], request["intent_id"], state="filled", now_iso=RESPONSE,
                    position_id=pid, result_status="filled", resolved_size_usd=25)
    api().reconcile_intents(*db, now_iso=RESPONSE)
    assert store.get_ticket(db[0], ticket)["state"] == "filled"
    return cohort, ticket, pid


def new_trigger(name, at, ordinal):
    from datetime import datetime
    from test_memecoin_desk_contract import rehash
    c = candidate()
    c.update(ticket_id=name, emitted_at=at, trigger_ordinal=ordinal)
    epoch = datetime.fromisoformat(at).timestamp()
    c["snapshot"]["emission_at"] = epoch
    for row in c["snapshot"]["fields"].values():
        row["known_at"] = epoch
    return rehash(c)


def test_closed_funded_position_releases_ticket_then_applies_existing_cooldown(databases):
    cohort, ticket, pid = filled_ticket(databases)
    fill = next(s for s in store.get_ticket(databases[0], ticket)["stages"] if s["stage"] == "fill")
    ledger.close_position(databases[1], pid, LATER, 2.0, "trail", recorded_at=LATER)
    assert api().reconcile_intents(*databases, now_iso=LATER)[0]["state"] == "closed"
    head = store.get_ticket(databases[0], ticket)
    assert head["state"] == "closed"
    assert next(s for s in head["stages"] if s["stage"] == "fill") == fill
    assert [s["stage"] for s in head["stages"]].count("close") == 1
    assert api().reconcile_intents(*databases, now_iso=LATER) == []
    before = "2026-10-21T12:01:00+00:00"
    seen = api().ingest_candidate(databases[0], new_trigger("desk:U:before-cooldown", before, 1),
        cohort_id=cohort, now_iso=before)
    assert seen["is_primary"] is False and seen["reason"] == "cooldown"
    after = "2026-10-21T12:15:31+00:00"
    admitted = api().ingest_candidate(databases[0], new_trigger("desk:U:after-cooldown", after, 2),
        cohort_id=cohort, now_iso=after)
    assert admitted["is_primary"] is True and admitted["ticket_id"] == "desk:U:after-cooldown"


@pytest.mark.parametrize("bad", ["missing_position", "unconfirmed_close", "still_open", "missing_time", "future_time", "wrong_identity"])
def test_unknown_or_mismatched_close_never_releases_the_coin(databases, bad, monkeypatch):
    cohort, ticket, pid = filled_ticket(databases)
    if bad == "missing_position":
        monkeypatch.setattr(ledger, "get_position", lambda *args: None)
    elif bad == "unconfirmed_close":
        assert ledger.claim_close(databases[1], pid)
    elif bad == "still_open":
        databases[1].execute("UPDATE memecoin_positions SET closed_at=? WHERE id=?", (LATER, pid))
    elif bad == "missing_time":
        databases[1].execute("UPDATE memecoin_positions SET is_open=0,closed_at=NULL WHERE id=?", (pid,))
    elif bad == "future_time":
        databases[1].execute("UPDATE memecoin_positions SET is_open=0,closed_at='2026-10-21T13:00:00+00:00' WHERE id=?", (pid,))
    else:
        databases[1].execute("UPDATE memecoin_positions SET is_open=0,closed_at=?,desk_snapshot_hash=? WHERE id=?", (LATER, "0" * 64, pid))
    databases[1].commit()
    api().reconcile_intents(*databases, now_iso=LATER)
    assert store.get_ticket(databases[0], ticket)["state"] == "filled"
    assert not [s for s in store.get_ticket(databases[0], ticket)["stages"] if s["stage"] == "close"]
    seen = api().ingest_candidate(databases[0], new_trigger("desk:U:still-held", LATER, 3),
        cohort_id=cohort, now_iso=LATER, cooldown_s=0)
    assert seen["is_primary"] is False and seen["reason"] == "active_ticket"


def test_ingest_refuses_u_mint_as_pool_before_writing_any_ticket(databases):
    cohort = provision(databases)
    c = candidate()
    c["pool"] = c["mint"]
    with pytest.raises(ValueError, match="candidate_pool_mint"):
        api().ingest_candidate(databases[0], c, cohort_id=cohort, now_iso=AT)
    for table in ("desk_ticket_heads", "desk_candidate_emissions", "desk_snapshots", "desk_outbox"):
        assert databases[0].execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert intents.pending_count(databases[1]) == 0


def ready_h(db, count=1, *, now_iso=AT):
    from sniper.services.memecoin_desk_contract import build_h_candidate
    from test_memecoin_desk_contract import receipt_inputs, NOW
    c = build_h_candidate(**receipt_inputs())
    cohort = api().provision_cohort(*db, producer="H", policy=POLICY,
        prompt_version="desk-judge-v6", created_at=AT)
    ticket = api().ingest_candidate(db[0], c, cohort_id=cohort, now_iso=AT)
    ev = evidence(c)
    if count is not None:
        ev["checks"]["activity"] = {"count": count, "source_time": NOW - 6,
            "evaluated_at": NOW, "known_at": NOW, "completeness": "complete", "whole_pool": True}
    filtered = api().evaluate_candidate(db[0], ticket["ticket_id"], evidence=ev, policy=policy(), now_iso=now_iso)
    return cohort, ticket["ticket_id"], c, ev, filtered


def admissions(conn, ticket):
    return [dict(row) for row in conn.execute(
        "SELECT arm,decision,decided_at FROM desk_arm_admissions WHERE ticket_id=? ORDER BY arm", (ticket,))]


def test_h_gate_skip_preserves_judge_and_counterfactual_admissions_without_f_intent(databases):
    cohort, ticket, c, ev, filtered = ready_h(databases)
    assert filtered["verdict"] == "PASS" and filtered["gate_state"] == "skip"
    assert filtered["reason"] == "no_activity_after_fill"
    assert store.get_ticket(databases[0], ticket)["state"] == "open"
    assert admissions(databases[0], ticket) == [{"arm": "F", "decision": "not_selected", "decided_at": AT}]
    result = decide(databases, ticket, verdict(k=ticket), model_cost_usd=0.02, cost_provenance="test:metered")
    assert result["action"] == "ENTER"
    store.record_admission(databases[0], ticket_id=ticket, arm="SJ", decision="taken", decided_at=RESPONSE)
    store.record_admission(databases[0], ticket_id=ticket, arm="SR", decision="taken", decided_at=RESPONSE)
    assert [(row["arm"], row["decision"]) for row in admissions(databases[0], ticket)] == [
        ("F", "not_selected"), ("SJ", "taken"), ("SR", "taken")]
    assert ledger.desk_cohort_accounting(databases[1], cohort)["known_cost_usd"] == 0.02
    assert store.list_outbox(databases[0]) == []
    assert api().dispatch_outbox(*databases, now_iso=RESPONSE) == []
    assert intents.pending_count(databases[1]) == 0
    assert [s["stage"] for s in store.get_ticket(databases[0], ticket)["stages"]] == ["hard_filter", "vet", "size"]
    size = store.get_ticket(databases[0], ticket)["stages"][-1]
    assert size["verdict"] == "SKIP" and size["reason"] == "activity_gate_skip"
    assert store.get_ticket(databases[0], ticket)["state"] == "skipped"


@pytest.mark.parametrize("count,gate", [(2, "pass"), (None, None)])
def test_h_pass_and_ungated_tickets_keep_their_f_enter_path(databases, count, gate):
    cohort, ticket, c, ev, filtered = ready_h(databases, count)
    assert filtered["verdict"] == "PASS" and filtered["gate_state"] == gate
    decide(databases, ticket, verdict(k=ticket))
    assert len(store.list_outbox(databases[0])) == 1
    assert api().dispatch_outbox(*databases, now_iso=RESPONSE)[0]["state"] == "sent"
    assert admissions(databases[0], ticket) == []


@pytest.mark.parametrize("when", ["before", "after"])
def test_h_gate_exclusion_recovers_its_original_clock_after_admission_crash(databases, monkeypatch, when):
    real_record = store.record_admission
    def crash(conn, **kwargs):
        if kwargs["arm"] == "F":
            if when == "after":
                real_record(conn, **kwargs)
            raise RuntimeError("F admission write crash")
        return real_record(conn, **kwargs)
    monkeypatch.setattr(store, "record_admission", crash)
    with pytest.raises(RuntimeError, match="admission write crash"):
        ready_h(databases)
    monkeypatch.setattr(store, "record_admission", real_record)
    from sniper.services.memecoin_desk_contract import build_h_candidate
    from test_memecoin_desk_contract import receipt_inputs, NOW
    c = build_h_candidate(**receipt_inputs())
    ev = evidence(c)
    ev["checks"]["activity"] = {"count": 1, "source_time": NOW - 6,
        "evaluated_at": NOW, "known_at": NOW, "completeness": "complete", "whole_pool": True}
    filtered = api().evaluate_candidate(databases[0], c["ticket_id"], evidence=ev, policy=policy(), now_iso=LATER)
    assert filtered["ts"] == AT
    decide(databases, c["ticket_id"], verdict(k=c["ticket_id"]), now_iso=LATER)
    assert admissions(databases[0], c["ticket_id"])[0]["decided_at"] == AT
    assert store.list_outbox(databases[0]) == []
    assert intents.pending_count(databases[1]) == 0


def test_gate_skip_record_decision_heals_missing_f_exclusion_without_reevaluation(databases, monkeypatch):
    real_record = store.record_admission
    monkeypatch.setattr(store, "record_admission", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError):
        ready_h(databases)
    monkeypatch.setattr(store, "record_admission", real_record)
    from sniper.services.memecoin_desk_contract import build_h_candidate
    from test_memecoin_desk_contract import receipt_inputs
    ticket = build_h_candidate(**receipt_inputs())["ticket_id"]
    decide(databases, ticket, verdict(k=ticket), now_iso=LATER)
    assert admissions(databases[0], ticket) == [{"arm": "F", "decision": "not_selected", "decided_at": AT}]
    assert api().dispatch_outbox(*databases, now_iso=LATER) == []


def test_safety_refusal_still_blocks_gate_skip_from_the_judge(databases):
    from sniper.services.memecoin_desk_contract import build_h_candidate
    from test_memecoin_desk_contract import receipt_inputs, NOW
    c = build_h_candidate(**receipt_inputs())
    cohort = api().provision_cohort(*databases, producer="H", policy=POLICY,
        prompt_version="desk-judge-v6", created_at=AT)
    ticket = api().ingest_candidate(databases[0], c, cohort_id=cohort, now_iso=AT)
    ev = evidence(c)
    ev["checks"]["token_safety"]["authority_freeze"] = "open"
    ev["checks"]["activity"] = {"count": 1, "source_time": NOW - 6,
        "evaluated_at": NOW, "known_at": NOW, "completeness": "complete", "whole_pool": True}
    result = api().evaluate_candidate(databases[0], ticket["ticket_id"], evidence=ev, policy=policy(), now_iso=AT)
    assert result["verdict"] == "SKIP" and result["gate_state"] is None
    with pytest.raises(ValueError, match="hard_filter_required"):
        decide(databases, ticket["ticket_id"], verdict(k=ticket["ticket_id"]))
    assert admissions(databases[0], ticket["ticket_id"]) == []


def test_gated_f_terminal_decision_releases_coin_after_cooldown_and_keeps_shadow_admissions(databases):
    cohort, ticket, c, ev, filtered = ready_h(databases)
    decide(databases, ticket, verdict(k=ticket))
    assert store.get_ticket(databases[0], ticket)["state"] == "skipped"
    before = "2026-10-21T12:00:30+00:00"
    retry = dict(c, ticket_id="desk:H:before-cooldown", opportunity_id="desk:H:before-cooldown", trigger_ordinal=1)
    assert api().ingest_candidate(databases[0], retry, cohort_id=cohort, now_iso=before)["reason"] == "cooldown"
    after = "2026-10-21T12:15:02+00:00"
    retry = dict(c, ticket_id="desk:H:after-cooldown", opportunity_id="desk:H:after-cooldown", trigger_ordinal=2)
    admitted = api().ingest_candidate(databases[0], retry, cohort_id=cohort, now_iso=after)
    assert admitted["is_primary"] is True and admitted["ticket_id"] == "desk:H:after-cooldown"
    for arm in ("SJ", "SR"):
        store.record_admission(databases[0], ticket_id=ticket, arm=arm, decision="taken", decided_at=RESPONSE)
    assert [a["decision"] for a in admissions(databases[0], ticket)] == ["not_selected", "taken", "taken"]


def test_gated_f_dispatch_heals_crash_after_vet_without_enqueuing(databases, monkeypatch):
    cohort, ticket, c, ev, filtered = ready_h(databases)
    real_record = store.record_stage
    def crash(conn, **kwargs):
        if kwargs["stage"] == "size":
            raise RuntimeError("gated size crash")
        return real_record(conn, **kwargs)
    monkeypatch.setattr(store, "record_stage", crash)
    with pytest.raises(RuntimeError, match="gated size crash"):
        decide(databases, ticket, verdict(k=ticket), model_cost_usd=0.01, cost_provenance="test:metered")
    monkeypatch.setattr(store, "record_stage", real_record)
    assert api().dispatch_outbox(*databases, now_iso=LATER) == []
    head = store.get_ticket(databases[0], ticket)
    assert head["state"] == "skipped" and head["stages"][-1]["ts"] == RESPONSE
    assert admissions(databases[0], ticket)[0]["decided_at"] == AT
    assert intents.pending_count(databases[1]) == 0
    assert ledger.desk_cohort_accounting(databases[1], cohort)["known_cost_usd"] == 0.01


def legacy_gated_request(db):
    cohort, ticket, c, ev, filtered = ready_h(db)
    head = store.get_ticket(db[0], ticket)
    result = {"action": "ENTER", "reason": "enter", "rails": {"tp_pct": 60, "sl_pct": 20, "trail_pct": None},
              "condition": None, "reason_code": 1}
    metadata = {key: head[key] for key in ("cohort_id", "producer", "opportunity_id", "snapshot_hash")}
    payload = {"mint": head["mint"], "network_id": "solana", "size_usd": 25,
        "rails": result["rails"], "rails_mode": "custom", "signal_source": "desk",
        "desk_ticket_id": ticket, "desk_metadata": metadata, "origin_event_id": "desk:" + ticket}
    request = {"intent_id": "desk-intent:legacy-gated", "requested_at": RESPONSE,
               "expires_at": EXPIRES, "payload": payload}
    store.record_stage(db[0], ticket_id=ticket, stage="vet", seat="judge", verdict="ENTER",
        reason="legacy_enter", inputs={"opportunity_id": ticket, "case": "legacy_gated"},
        output={"result": result}, ts=RESPONSE)
    store.record_stage(db[0], ticket_id=ticket, stage="size", seat="code", verdict="ENTER",
        reason="legacy_enter", inputs={"opportunity_id": ticket, "request": request}, output=result,
        ts=RESPONSE, outbox=request)
    return cohort, ticket, request


def legacy_untimed_request(db, *, outbox):
    cohort, ticket = ready(db)
    head = store.get_ticket(db[0], ticket)
    result = {"action": "ENTER", "reason": "enter", "rails": {"tp_pct": 60, "sl_pct": 20, "trail_pct": None},
              "condition": None, "reason_code": 1}
    metadata = {key: head[key] for key in ("cohort_id", "producer", "opportunity_id", "snapshot_hash")}
    request = {"intent_id": "desk-intent:legacy-untimed", "requested_at": RESPONSE,
        "expires_at": EXPIRES, "payload": {"mint": head["mint"], "network_id": "solana", "size_usd": 25,
            "rails": result["rails"], "rails_mode": "custom", "signal_source": "desk",
            "desk_ticket_id": ticket, "desk_metadata": metadata, "origin_event_id": "desk:" + ticket}}
    store.record_stage(db[0], ticket_id=ticket, stage="vet", seat="judge", verdict="ENTER",
        reason="legacy_enter", inputs={"response_at": RESPONSE, "expires_at": EXPIRES},
        output={"result": result}, ts=RESPONSE, response_at=RESPONSE)
    if outbox:
        store.record_stage(db[0], ticket_id=ticket, stage="size", seat="code", verdict="ENTER",
            reason="legacy_enter", inputs={"request": request}, output=result, ts=RESPONSE, outbox=request)
    return cohort, ticket, request


@pytest.mark.parametrize("outbox", [False, True])
def test_legacy_unsent_timing_contract_refuses_without_inventing_a_ttl(databases, outbox):
    _, ticket, request = legacy_untimed_request(databases, outbox=outbox)
    api().dispatch_outbox(*databases, now_iso=RESPONSE)
    head = store.get_ticket(databases[0], ticket)
    assert head["state"] == "refused"
    assert intents.read_status(databases[1], request["intent_id"]) is None
    assert store.list_outbox(databases[0]) == []
    if not outbox:
        assert head["stages"][-1]["reason"] == "timing_contract_missing"


def test_legacy_existing_unknown_is_acknowledged_before_new_timing_rules(databases):
    cohort, ticket, request = legacy_untimed_request(databases, outbox=True)
    intents.enqueue(databases[1], **request["payload"], intent_id=request["intent_id"], now_iso=RESPONSE, ttl_s=20)
    intents.resolve(databases[1], request["intent_id"], state="unresolved", now_iso=RESPONSE,
                    result_status="submitted_unknown", reason="legacy confirmation unknown")
    ledger.record_desk_cohort_cost(databases[1], cohort_id=cohort, event_id="test:kill", usd=200,
        kind="model", occurred_at=RESPONSE, provenance="test:bill")
    assert api().dispatch_outbox(*databases, now_iso=LATER)[0]["state"] == "sent"
    existing = intents.read_status(databases[1], request["intent_id"])
    assert existing["operation_state"] == "unresolved" and existing["expires_at"] == EXPIRES
    assert databases[1].execute("SELECT COUNT(*) FROM memecoin_manual_intents").fetchone()[0] == 1


def test_courier_refuses_legacy_gated_outbox_before_any_new_ledger_request(databases):
    cohort, ticket, request = legacy_gated_request(databases)
    result = api().dispatch_outbox(*databases, now_iso=RESPONSE)
    assert result[0]["state"] == "refused" and result[0]["reason"] == "activity_gate_skip"
    assert intents.read_status(databases[1], request["intent_id"]) is None
    assert admissions(databases[0], ticket) == [{"arm": "F", "decision": "not_selected", "decided_at": AT}]
    assert api().dispatch_outbox(*databases, now_iso=LATER) == []


def test_courier_acknowledges_existing_legacy_unknown_before_gate_expiry_or_cohort_kill(databases):
    cohort, ticket, request = legacy_gated_request(databases)
    intents.enqueue(databases[1], **request["payload"], intent_id=request["intent_id"], now_iso=RESPONSE, ttl_s=20)
    intents.resolve(databases[1], request["intent_id"], state="unresolved", now_iso=RESPONSE,
                    result_status="submitted_unknown", reason="legacy confirmation unknown")
    ledger.record_desk_cohort_cost(databases[1], cohort_id=cohort, event_id="test:kill", usd=200,
        kind="model", occurred_at=RESPONSE, provenance="test:bill")
    result = api().dispatch_outbox(*databases, now_iso=LATER)
    assert result[0]["state"] == "sent"
    existing = intents.read_status(databases[1], request["intent_id"])
    assert existing["operation_state"] == "unresolved" and existing["expires_at"] == EXPIRES
    assert databases[1].execute("SELECT COUNT(*) FROM memecoin_manual_intents").fetchone()[0] == 1


def test_gate_skip_f_exclusion_precedes_a_terminal_judge_commit(databases, monkeypatch):
    real_admission = store.record_admission
    monkeypatch.setattr(store, "record_admission", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("F crash")))
    with pytest.raises(RuntimeError, match="F crash"):
        ready_h(databases)
    monkeypatch.setattr(store, "record_admission", real_admission)
    from sniper.services.memecoin_desk_contract import build_h_candidate
    from test_memecoin_desk_contract import receipt_inputs
    ticket = build_h_candidate(**receipt_inputs())["ticket_id"]
    real_stage = store.record_stage
    def crash_after_vet(conn, **kwargs):
        result = real_stage(conn, **kwargs)
        if kwargs["stage"] == "vet":
            raise RuntimeError("after terminal vet commit")
        return result
    monkeypatch.setattr(store, "record_stage", crash_after_vet)
    with pytest.raises(RuntimeError, match="after terminal vet commit"):
        decide(databases, ticket, verdict("S", k=ticket))
    assert admissions(databases[0], ticket) == [
        {"arm": "F", "decision": "not_selected", "decided_at": AT}]
    assert store.get_ticket(databases[0], ticket)["state"] == "skipped"
    assert intents.pending_count(databases[1]) == 0


def model_ready(db, basis=None):
    frozen = POLICY if basis is None else {**POLICY, 'model_billing_basis': basis}
    cohort = api().provision_cohort(*db, producer='U', policy=frozen,
        prompt_version='desk-judge-v6', created_at=AT)
    ticket = api().ingest_candidate(db[0], candidate(), cohort_id=cohort, now_iso=AT)['ticket_id']
    api().evaluate_candidate(db[0], ticket, evidence=evidence(), policy=policy(), now_iso=AT)
    return cohort, ticket


def model_proof(ticket, **changes):
    proof = {'ticket_id': ticket, 'provider': 'claude_sdk', 'model': 'claude-haiku-5-5',
        'session_id': 'session-1', 'result_id': 'result-1', 'response_at': RESPONSE,
        'usage': {'input_tokens': 12, 'output_tokens': 40, 'cache_read_input_tokens': 4000},
        'is_error': False, 'billing_basis': 'subscription_flat', 'notional_api_equivalent_usd': .07}
    return {**proof, **changes}


def model_decision(db, ticket, proof, **changes):
    return api().record_decision(*db, ticket, verdict(), bounds=BOUNDS, elapsed_ms=1000,
        call_started_at=START, response_at=RESPONSE, now_iso=RESPONSE, expires_at=EXPIRES,
        model='claude-haiku-5-5', model_cost_receipt=proof, **changes)


def test_subscription_receipt_automatically_prices_marginal_zero_and_preserves_estimate(databases):
    cohort, ticket = model_ready(databases, 'subscription_flat')
    proof = model_proof(ticket)
    assert model_decision(databases, ticket, proof)['action'] == 'ENTER'
    model_decision(databases, ticket, proof)
    row = next(r for r in store.get_ticket(databases[0], ticket)['stages'] if r['stage'] == 'vet')
    assert row['cost_usd'] == 0
    assert row['inputs']['model_cost_evidence']['receipt']['notional_api_equivalent_usd'] == .07
    assert ledger.desk_cohort_accounting(databases[1], cohort)['known_cost_usd'] == 0
    assert ledger.desk_cohort_accounting(databases[1], cohort)['unknown_costs'] == 0
    assert len(store.list_outbox(databases[0])) == 1
    changed = {**proof, 'notional_api_equivalent_usd': .08}
    with pytest.raises(ValueError, match='stage_conflict'):
        model_decision(databases, ticket, changed)


def test_subscription_receipt_without_frozen_basis_remains_unknown_cash(databases):
    cohort, ticket = model_ready(databases)
    model_decision(databases, ticket, model_proof(ticket))
    assert ledger.desk_cohort_accounting(databases[1], cohort)['unknown_costs'] == 1
    assert ledger.desk_cohort_accounting(databases[1], cohort)['net_usd'] is None


def test_error_result_is_not_enter_but_actual_bill_still_counts(databases):
    cohort, ticket = model_ready(databases, 'subscription_flat')
    proof = model_proof(ticket, is_error=True, billing_basis='billed_receipt', billed_usd=.025,
                        charge_id='charge-actual')
    result = model_decision(databases, ticket, proof)
    assert result['action'] == 'SKIP'
    assert store.list_outbox(databases[0]) == []
    assert ledger.desk_cohort_accounting(databases[1], cohort)['known_cost_usd'] == .025


def test_supplied_receipt_cannot_conflict_with_scalar_cost(databases):
    cohort, ticket = model_ready(databases, 'subscription_flat')
    with pytest.raises(ValueError, match='model_cost_conflict'):
        model_decision(databases, ticket, model_proof(ticket), model_cost_usd=.07)
    assert ledger.desk_cohort_accounting(databases[1], cohort)['known_cost_usd'] == 0
    assert not store.list_outbox(databases[0])


def test_malformed_model_billing_receipt_leaves_unknown_incurred_charge(databases):
    cohort, ticket = model_ready(databases, 'subscription_flat')
    proof = model_proof(ticket, usage={'input_tokens': True}, session_id='private-proof-sentinel')
    with pytest.raises(ValueError, match='model_cost_receipt_invalid'):
        model_decision(databases, ticket, proof)
    assert ledger.desk_cohort_accounting(databases[1], cohort)['unknown_costs'] == 1
    assert not store.list_outbox(databases[0])
    head = store.get_ticket(databases[0], ticket)
    assert head['state'] == 'skipped'
    vet = head['stages'][-1]
    assert vet['stage'] == 'vet' and vet['verdict'] == 'SKIP' and vet['reason'] == 'judge_malformed_result'
    assert vet['ts'] == vet['response_at'] == RESPONSE
    assert vet['output']['result'] == {'action': 'SKIP', 'reason': 'judge_malformed_result',
        'rails': None, 'condition': None, 'reason_code': None, 'enter_ttl_s': None, 'next_check_s': None}
    assert 'private-proof-sentinel' not in json.dumps(vet)
    with pytest.raises(ValueError, match='stage_conflict'):
        model_decision(databases, ticket, model_proof(ticket))
    assert ledger.desk_cohort_accounting(databases[1], cohort)['unknown_costs'] == 1
    assert store.get_ticket(databases[0], ticket)['stages'] == head['stages']
    before, after = '2026-10-21T12:01:00+00:00', '2026-10-21T12:15:02+00:00'
    seen = api().ingest_candidate(databases[0], new_trigger('desk:U:malformed-cooldown', before, 1),
        cohort_id=cohort, now_iso=before)
    assert seen['is_primary'] is False and seen['reason'] == 'cooldown'
    admitted = api().ingest_candidate(databases[0], new_trigger('desk:U:after-malformed', after, 2),
        cohort_id=cohort, now_iso=after)
    assert admitted['is_primary'] is True and admitted['ticket_id'] == 'desk:U:after-malformed'


@pytest.mark.parametrize('kwargs', [{'model_cost_usd': float('nan')}, {'model_cost_usd': True},
                                    {'cost_provenance': 'bad provenance'}])
def test_malformed_scalar_beside_model_receipt_cannot_erase_incurred_call(databases, kwargs):
    cohort, ticket = model_ready(databases, 'subscription_flat')
    with pytest.raises(ValueError):
        model_decision(databases, ticket, model_proof(ticket), **kwargs)
    assert ledger.desk_cohort_accounting(databases[1], cohort)['unknown_costs'] == 1
    assert not store.list_outbox(databases[0])
    assert store.get_ticket(databases[0], ticket)['state'] == 'skipped'


@pytest.mark.parametrize('kwargs,reason', [({'model_cost_usd': float('nan')}, 'model_cost'),
    ({'model_cost_usd': True}, 'model_cost'), ({'cost_provenance': 'private secret'}, 'cost_provenance'),
    ({'model': 'https://private.example/secret'}, 'model_identifier')])
def test_invalid_scalar_without_a_receipt_freezes_unknown_cost_and_terminal_skip(databases, kwargs, reason):
    cohort, ticket = ready(databases)
    with pytest.raises(ValueError, match='^' + reason + '$'):
        decide(databases, ticket, **kwargs)
    assert ledger.desk_cohort_accounting(databases[1], cohort)['unknown_costs'] == 1
    head = store.get_ticket(databases[0], ticket)
    assert head['state'] == 'skipped' and head['stages'][-1]['reason'] == 'judge_malformed_result'
    assert 'private secret' not in json.dumps(head['stages'])
    assert 'https://private.example/secret' not in json.dumps(head['stages'])
    assert store.list_outbox(databases[0]) == [] and intents.pending_count(databases[1]) == 0


def test_invalid_proof_retry_heals_marker_before_vet_crash_once(databases, monkeypatch):
    cohort, ticket = model_ready(databases, 'subscription_flat')
    proof = model_proof(ticket, usage={'input_tokens': True})
    original = store.record_stage
    def crash(conn, **kwargs):
        if kwargs['stage'] == 'vet':
            raise RuntimeError('after malformed cost commit')
        return original(conn, **kwargs)
    monkeypatch.setattr(store, 'record_stage', crash)
    with pytest.raises(RuntimeError, match='after malformed cost commit'):
        model_decision(databases, ticket, proof)
    assert ledger.desk_cohort_accounting(databases[1], cohort)['unknown_costs'] == 1
    assert store.get_ticket(databases[0], ticket)['state'] == 'open'
    monkeypatch.setattr(store, 'record_stage', original)
    for reason in ('model_cost_previously_refused', 'model_cost_receipt_invalid'):
        with pytest.raises(ValueError, match='^' + reason + '$'):
            api().record_decision(*databases, ticket, verdict(), bounds=BOUNDS, elapsed_ms=1000,
                call_started_at=START, response_at=RESPONSE, now_iso=LATER,
                model='claude-haiku-5-5', model_cost_receipt=proof)
    head = store.get_ticket(databases[0], ticket)
    assert head['state'] == 'skipped'
    assert [row['stage'] for row in head['stages']] == ['hard_filter', 'vet']
    assert head['stages'][-1]['response_at'] == head['stages'][-1]['ts'] == RESPONSE
    assert databases[1].execute('SELECT COUNT(*) FROM memecoin_desk_cohort_costs WHERE cohort_id=?',
                              (cohort,)).fetchone()[0] == 1
    assert ledger.desk_cohort_accounting(databases[1], cohort)['unknown_costs'] == 1
    assert store.list_outbox(databases[0]) == []


def test_invalid_proof_cannot_rewrite_an_existing_valid_vet_or_charge(databases):
    cohort, ticket = model_ready(databases, 'subscription_flat')
    model_decision(databases, ticket, model_proof(ticket))
    before = store.get_ticket(databases[0], ticket)['stages']
    with pytest.raises(ValueError, match='^model_cost_receipt_invalid$'):
        model_decision(databases, ticket, model_proof(ticket, usage={'input_tokens': True}))
    assert store.get_ticket(databases[0], ticket)['stages'] == before
    assert ledger.desk_cohort_accounting(databases[1], cohort)['unknown_costs'] == 0
    assert len(store.list_outbox(databases[0])) == 1


@pytest.mark.parametrize('replacement_at', [RESPONSE, '2026-10-21T12:00:02+00:00'])
def test_valid_replacement_cannot_reroll_a_refused_cost_after_marker_only_crash(databases, monkeypatch, replacement_at):
    cohort, ticket = model_ready(databases, 'subscription_flat')
    original = store.record_stage
    def crash(conn, **kwargs):
        if kwargs['stage'] == 'vet':
            raise RuntimeError('after invalid marker')
        return original(conn, **kwargs)
    monkeypatch.setattr(store, 'record_stage', crash)
    with pytest.raises(RuntimeError, match='after invalid marker'):
        model_decision(databases, ticket, model_proof(ticket, usage={'input_tokens': True}))
    before = tuple(databases[1].execute('SELECT kind,usd,occurred_at,provenance FROM memecoin_desk_cohort_costs '
        'WHERE cohort_id=? AND event_id=?', (cohort, 'desk-judge:' + ticket)).fetchone())
    monkeypatch.setattr(store, 'record_stage', original)
    with pytest.raises(ValueError, match='^model_cost_previously_refused$'):
        api().record_decision(*databases, ticket, verdict(), bounds=BOUNDS, elapsed_ms=2000,
            call_started_at=START, response_at=replacement_at, now_iso=LATER, model='claude-haiku-5-5',
            model_cost_receipt=model_proof(ticket, response_at=replacement_at))
    marker = ledger.get_desk_cohort_cost(databases[1], cohort, 'desk-judge:' + ticket)
    assert tuple(marker[key] for key in ('kind', 'usd', 'occurred_at', 'provenance')) == before
    assert ledger.get_desk_cohort_cost(databases[1], cohort, 'desk-judge:absent') is None
    head = store.get_ticket(databases[0], ticket)
    assert head['state'] == 'skipped'
    assert head['stages'][-1]['ts'] == head['stages'][-1]['response_at'] == RESPONSE
    assert head['stages'][-1]['inputs']['cost_provenance'] == before[3]
    assert ledger.desk_cohort_accounting(databases[1], cohort)['unknown_costs'] == 1
    assert store.list_outbox(databases[0]) == [] and intents.pending_count(databases[1]) == 0


def test_previously_refused_marker_can_be_priced_without_reopening_the_decision(databases):
    cohort, ticket = model_ready(databases, 'subscription_flat')
    cost = dict(cohort_id=cohort, event_id='desk-judge:' + ticket, kind='model',
                occurred_at=RESPONSE, provenance='model-invalid:' + 'a' * 64)
    ledger.record_desk_cohort_cost(databases[1], **cost, usd=None)
    ledger.record_desk_cohort_cost(databases[1], **cost, usd=.025)
    before = ledger.get_desk_cohort_cost(databases[1], cohort, cost['event_id'])
    with pytest.raises(ValueError, match='^model_cost_previously_refused$'):
        model_decision(databases, ticket, model_proof(ticket))
    assert ledger.get_desk_cohort_cost(databases[1], cohort, cost['event_id']) == before
    assert store.get_ticket(databases[0], ticket)['state'] == 'skipped'
    state = ledger.desk_cohort_accounting(databases[1], cohort)
    assert state['known_cost_usd'] == .025 and state['unknown_costs'] == 0
    assert store.list_outbox(databases[0]) == []


@pytest.mark.parametrize("producer,signal,reason", [("U", "flow", "kill:flow_missing_universe")])
def test_frozen_fact_refusals_persist_before_any_judge_or_entry(databases, producer, signal, reason):
    from sniper.services.memecoin_desk_contract import build_h_candidate
    from sniper.services.memecoin_desk_universe import Field
    from test_memecoin_desk_contract import NOW, receipt_inputs

    facts = {}
    if signal == "flow":
        facts["flow_60s"] = Field(None, "pumpfun_grpc:bars", NOW, 7, "missing")
    elif signal == "warning":
        facts["crawler_intent"] = Field("warning", "crawler:cached", NOW, 7, "complete")
    else:
        facts["crawler_flags"] = Field({"copycat_ticker": False, "bought_hype_pattern": False,
            "suspicious_text": False, "rug_history_claim": True}, "crawler:cached", NOW, 7, "complete")
    if producer == "H":
        args = receipt_inputs()
        healthy = build_h_candidate(**args)
        args["fields"].update(facts)
        c = build_h_candidate(**args)
    else:
        healthy, c = candidate(), candidate(**facts)
    cohort = api().provision_cohort(*databases, producer=producer, policy=POLICY,
        prompt_version="desk-judge-v6", created_at=AT)
    ticket = api().ingest_candidate(databases[0], c, cohort_id=cohort, now_iso=AT)["ticket_id"]
    frozen = store.get_ticket(databases[0], ticket)["candidate"]
    ev = evidence(frozen)
    # A caller's later repair cannot replace the already stored primary emission.
    c["snapshot"] = healthy["snapshot"]
    first = api().evaluate_candidate(databases[0], ticket, evidence=ev, policy=policy(), now_iso=AT)
    second = api().evaluate_candidate(databases[0], ticket, evidence=ev, policy=policy(), now_iso=LATER)
    assert first == second
    assert first["stage"] == "hard_filter" and first["verdict"] == "SKIP" and first["reason"] == reason
    assert first["inputs"]["snapshot_hash"] == frozen["snapshot"]["hash"]
    assert first["inputs"]["evidence"] == ev
    head = store.get_ticket(databases[0], ticket)
    assert head["candidate"] == frozen and head["state"] == "skipped"
    assert [row["stage"] for row in head["stages"]] == ["hard_filter"]
    with pytest.raises(ValueError, match="hard_filter_required"):
        decide(databases, ticket, verdict(k=ticket))
    assert api().dispatch_outbox(*databases, now_iso=LATER) == []
    assert store.list_outbox(databases[0]) == [] and intents.pending_count(databases[1]) == 0
    assert ledger.desk_cohort_accounting(databases[1], cohort)["unknown_costs"] == 0


def test_roster_null_flow_remains_eligible_for_a_judge(databases):
    cohort, ticket, c, ev, filtered = ready_h(databases, count=None)
    assert "flow_60s" not in c["snapshot"]["fields"]
    assert filtered["verdict"] == "PASS" and filtered["gate_state"] is None
    assert decide(databases, ticket, verdict(k=ticket))["action"] == "ENTER"
    assert len(store.list_outbox(databases[0])) == 1
