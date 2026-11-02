"""Desk JUDGE caller: claim, call once, freeze the result, record through step 3 (fake model, real DBs)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from contextlib import nullcontext

from sniper.persistence import db_memecoin_desk as store
from sniper.services import memecoin_desk_judge as J
from test_memecoin_desk_pipeline import databases, provision, api, AT, POLICY  # noqa: F401
from test_memecoin_desk_contract import candidate, evidence, policy

T0 = datetime.fromisoformat(AT)


class Clock:
    def __init__(self):
        self.t = T0 + timedelta(seconds=1)

    def __call__(self):
        return self.t


def ready_ttl(db):
    cohort = api().provision_cohort(*db, producer="U", policy={**POLICY, "judge_p_admit": 1.0, "select_version": J.SELECT_VERSION},
                                    prompt_version="desk-judge-v6", created_at=AT)
    ticket = api().ingest_candidate(db[0], candidate(), cohort_id=cohort, now_iso=AT)
    api().evaluate_candidate(db[0], ticket["ticket_id"], evidence=evidence(), policy=policy(), now_iso=AT)
    return ticket["ticket_id"]


def model_returning(text_fn, *, calls, is_error=False, call_error=None, clock=None, seconds=1.5,
                    echo="claude-haiku-5-5"):
    def call(system, user, **_):
        calls.append(json.loads(user))
        if clock is not None:
            clock.t += timedelta(seconds=seconds)
        sheet = json.loads(user)
        return {"text": text_fn(sheet), "is_error": is_error, "call_error": call_error, "session_id": "s1",
                "result_id": "r-%d" % len(calls), "usage": {"input_tokens": 4, "output_tokens": 60,
                "cache_read_input_tokens": 4000, "cache_creation_input_tokens": 0},
                "notional_usd": 0.0004, "model_echo": echo, "billing_basis": "subscription_flat"}
    return call


def enter(sheet):
    return json.dumps({"k": sheet["opportunity_id"], "v": "E", "e": None, "r": [60, 20, 15], "c": 1,
                       "enter_ttl_s": 5, "next_check_s": 15})


def test_one_ticket_is_claimed_called_once_and_recorded(databases):
    ticket = ready_ttl(databases)
    calls, clock = [], Clock()
    out = J.run_once(databases[0], lambda: nullcontext(databases[1]), call_model=model_returning(enter, calls=calls, clock=clock),
                     system_prompt="SYS", worker="w1", now_fn=clock, calls_per_hour=100)
    assert [o["ticket_id"] for o in out] == [ticket] and len(calls) == 1
    vet = [s for s in store.get_ticket(databases[0], ticket)["stages"] if s["stage"] == "vet"][0]
    assert vet["verdict"] == "ENTER" and vet["model"] == "claude-haiku-5-5"
    result = vet["output"]["result"]                                               # the model's own timings
    assert (result["enter_ttl_s"], result["next_check_s"]) == (5, 15)
    outbox = store.list_outbox(databases[0])
    assert len(outbox) == 1
    assert "SYS" not in json.dumps(calls[0]) and "ticker" not in calls[0]          # enum-only sheet
    # a second pass finds nothing: never re-asks
    assert J.run_once(databases[0], lambda: nullcontext(databases[1]), call_model=model_returning(enter, calls=calls),
                      system_prompt="SYS", worker="w1", now_fn=clock, calls_per_hour=100) == []
    assert len(calls) == 1


def test_crash_after_response_is_recorded_from_the_saved_result_without_a_new_call(databases):
    ticket = ready_ttl(databases)
    calls, clock = [], Clock()
    def crash(*a, **k):
        raise RuntimeError("crash after save")
    with pytest.raises(RuntimeError):
        J.run_once(databases[0], lambda: nullcontext(databases[1]), call_model=model_returning(enter, calls=calls, clock=clock),
                   system_prompt="SYS", worker="w1", now_fn=clock, calls_per_hour=100, record_fn=crash)
    assert len(calls) == 1
    J.recover(databases[0], lambda: nullcontext(databases[1]), now_fn=clock)
    assert len(calls) == 1
    assert [s["verdict"] for s in store.get_ticket(databases[0], ticket)["stages"] if s["stage"] == "vet"] == ["ENTER"]


def test_claim_without_saved_result_is_recorded_as_lost_never_reasked(databases):
    ticket = ready_ttl(databases)
    clock = Clock()
    store.claim_judge_call(databases[0], ticket_id=ticket, call_id="c-x", worker="dead", claimed_at=AT)
    J.recover(databases[0], lambda: nullcontext(databases[1]), now_fn=clock)
    vet = [s for s in store.get_ticket(databases[0], ticket)["stages"] if s["stage"] == "vet"][0]
    assert vet["verdict"] == "SKIP" and vet["reason"] == "judge_lost"


@pytest.mark.parametrize("call_error", ["rate_limited", "auth_expired", "timeout", "refusal"])
def test_call_errors_are_distinct_skips(databases, call_error):
    ticket = ready_ttl(databases)
    calls, clock = [], Clock()
    J.run_once(databases[0], lambda: nullcontext(databases[1]),
               call_model=model_returning(lambda s: "", calls=calls, call_error=call_error, clock=clock),
               system_prompt="SYS", worker="w1", now_fn=clock, calls_per_hour=100)
    vet = [s for s in store.get_ticket(databases[0], ticket)["stages"] if s["stage"] == "vet"][0]
    assert vet["verdict"] == "SKIP" and vet["reason"] == "judge_" + call_error


def test_sdk_is_error_forces_skip_even_if_text_says_enter(databases):
    ticket = ready_ttl(databases)
    calls, clock = [], Clock()
    J.run_once(databases[0], lambda: nullcontext(databases[1]), call_model=model_returning(enter, calls=calls, is_error=True, clock=clock),
               system_prompt="SYS", worker="w1", now_fn=clock, calls_per_hour=100)
    vet = [s for s in store.get_ticket(databases[0], ticket)["stages"] if s["stage"] == "vet"][0]
    assert vet["verdict"] == "SKIP" and vet["reason"] == "judge_model_result_error"


@pytest.mark.parametrize("echo,verdict", [("claude-sonnet-5-5", "SKIP"), ("claude-haiku-5-5-anything", "SKIP"),
                                          ("nonstr:list", "SKIP"),
                                          ("claude-haiku-5-5-20261015", "ENTER"), (None, "ENTER")])
def test_another_models_answer_skips_and_a_dated_or_missing_echo_still_enters(databases, echo, verdict):
    # Alpha/Gamma 2026-10-21: one shared echo rule; the proof keeps MODEL so a dated snapshot never breaks
    # step 3's identity check, and another model's ENTER becomes a SKIP with its cost still recorded
    ticket = ready_ttl(databases)
    calls, clock = [], Clock()
    J.run_once(databases[0], lambda: nullcontext(databases[1]),
               call_model=model_returning(enter, calls=calls, clock=clock, echo=echo),
               system_prompt="SYS", worker="w1", now_fn=clock, calls_per_hour=100)
    vet = [s for s in store.get_ticket(databases[0], ticket)["stages"] if s["stage"] == "vet"][0]
    assert vet["verdict"] == verdict
    if verdict == "SKIP":
        assert vet["reason"] == "judge_error" and "billing_error" not in (vet["inputs"] or {})


def test_slow_answer_past_the_deadline_is_a_timeout_skip(databases):
    ticket = ready_ttl(databases)
    calls, clock = [], Clock()
    J.run_once(databases[0], lambda: nullcontext(databases[1]), call_model=model_returning(enter, calls=calls, clock=clock, seconds=13.0),
               system_prompt="SYS", worker="w1", now_fn=clock, calls_per_hour=100)
    vet = [s for s in store.get_ticket(databases[0], ticket)["stages"] if s["stage"] == "vet"][0]
    assert vet["verdict"] == "SKIP" and vet["reason"] == "judge_timeout"


def test_budget_exhausted_skips_without_calling_the_model(databases):
    ticket = ready_ttl(databases)
    calls, clock = [], Clock()
    J.run_once(databases[0], lambda: nullcontext(databases[1]), call_model=model_returning(enter, calls=calls, clock=clock),
               system_prompt="SYS", worker="w1", now_fn=clock, calls_per_hour=0)
    assert calls == []
    vet = [s for s in store.get_ticket(databases[0], ticket)["stages"] if s["stage"] == "vet"][0]
    assert vet["reason"] == "judge_budget_exhausted"


def test_out_of_bounds_timing_is_a_skip_with_no_outbox(databases):
    """The caller forwards the raw verdict; step 3 checks it against the frozen caps (enter_ttl_max_s 30)."""
    ready_ttl(databases)
    late = lambda sheet: json.dumps({"k": sheet["opportunity_id"], "v": "E", "e": None, "r": [60, 20, 15], "c": 1,
                                     "enter_ttl_s": 31, "next_check_s": 15})
    out = J.run_once(databases[0], lambda: nullcontext(databases[1]), call_model=model_returning(late, calls=[]),
                     system_prompt="SYS", worker="w1", now_fn=Clock(), calls_per_hour=100)
    assert [o["reason"] for o in out] == ["timing_out_of_bounds"]
    assert not store.list_outbox(databases[0])


def test_a_ticket_claimed_by_another_worker_is_never_called(databases, monkeypatch):
    ready_ttl(databases)
    calls, clock = [], Clock()
    monkeypatch.setattr(J.store, "claim_judge_call", lambda *a, **k: False)   # lost the race
    assert J.run_once(databases[0], lambda: nullcontext(databases[1]), call_model=model_returning(enter, calls=calls, clock=clock),
                      system_prompt="SYS", worker="w2", now_fn=clock, calls_per_hour=100) == []
    assert calls == []


def make_candidate(i, at):
    from sniper.services.memecoin_desk_universe import Field, build_snapshot, b58encode
    ts = at.timestamp()
    snap = build_snapshot(emission_at=ts, fields={
        "mcap_usd": Field(41000.0, "pumpfun_grpc:curve_reserves", ts, 7, "complete"),
        "liq_usd": Field(5000.0, "pumpfun_grpc:real_quote_reserves", ts, 7, "complete"),
        # U with null flow is a hard-filter kill since 6975587a4; a measured window reaches the seat
        "flow_60s": Field({"buy_n": 3, "sell_n": 1, "buy_usd": 120.0, "sell_usd": 30.0, "makers": 3,
                           "largest_buy": 60.0}, "pumpfun_grpc:trades", ts, 7, "complete")})
    return {"schema": "desk-candidate/v1", "ticket_id": "desk:U:t%03d" % i, "producer": "U", "network_id": "solana",
            "mint": b58encode(bytes([i + 1]) * 31 + b"\x01"), "pool": b58encode(bytes([i + 101]) * 31 + b"\x02"),
            "band_entry_slot": 7, "trigger_signature": b58encode(bytes([i + 1]) * 64), "trigger_ordinal": 0,
            "emitted_at": at.isoformat(), "gate_state": None, "snapshot": snap}


def feed(databases, cohort, clock, i, at, **run):
    c = make_candidate(i, at)
    clock.t = at + timedelta(seconds=1)
    t = api().ingest_candidate(databases[0], c, cohort_id=cohort, now_iso=clock.t.isoformat())
    api().evaluate_candidate(databases[0], t["ticket_id"], evidence=evidence(c), policy=policy(),
                             now_iso=clock.t.isoformat())
    return J.run_once(databases[0], lambda: nullcontext(databases[1]), now_fn=clock, system_prompt="SYS",
                      worker="w", **run)


def test_budget_skips_do_not_starve_the_seat(databases):
    """cph=2, 3 tickets per hour over 2 h, all admitted by the sample: 2 calls in each hour (Gamma)."""
    calls, clock = [], Clock()
    cohort = api().provision_cohort(*databases, producer="U", policy={**POLICY, "judge_p_admit": 1.0, "select_version": J.SELECT_VERSION},
                                    prompt_version="desk-judge-v6", created_at=AT)
    per_hour = []
    for hour in range(2):
        made = len(calls)
        for k in range(3):
            feed(databases, cohort, clock, hour * 3 + k, T0 + timedelta(hours=hour, minutes=10 * k + 1),
                 call_model=model_returning(enter, calls=calls), calls_per_hour=2, admit_fn=lambda t: True)
        per_hour.append(len(calls) - made)
    assert per_hour == [2, 2]



def test_admission_sample_is_the_frozen_hash_rule(databases):
    """admit iff sha256('desk-judge-admit-v1|'+opportunity_id) < p_admit; the rest are budget_skipped
    with F = not_selected and never count as model calls."""
    calls, clock = [], Clock()
    cohort = api().provision_cohort(*databases, producer="U",
                                    policy={**POLICY, "judge_p_admit": 0.5},
                                    prompt_version="desk-judge-v6", created_at=AT)
    out = []
    for k in range(10):
        out += feed(databases, cohort, clock, k, T0 + timedelta(minutes=3 * k + 1),
                    call_model=model_returning(enter, calls=calls), calls_per_hour=100)
    want = {"desk:U:t%03d" % k for k in range(10) if J.hash_unit("desk:U:t%03d" % k) < 0.5}
    assert 0 < len(want) < 10
    assert {o["ticket_id"] for o in out if o["reason"] != "judge_budget_skipped"} == want
    assert len(calls) == len(want)
    for o in out:
        if o["ticket_id"] not in want:
            adm = databases[0].execute("SELECT decision FROM desk_arm_admissions WHERE ticket_id=? AND arm='F'",
                                       (o["ticket_id"],)).fetchone()
            assert adm[0] == "not_selected"


def test_missing_p_admit_refuses_to_run(databases):
    cohort = api().provision_cohort(*databases, producer="U", policy=POLICY,
                                    prompt_version="desk-judge-v6", created_at=AT)
    calls = []
    with pytest.raises(ValueError, match="judge_p_admit"):
        feed(databases, cohort, Clock(), 0, T0 + timedelta(minutes=1),
             call_model=model_returning(enter, calls=calls), calls_per_hour=100)
    assert calls == []


def test_existing_f_row_is_not_rewritten_on_budget_skip(databases):
    """An F not_selected already written at the hard-filter clock (H gate skip) must not crash the pass."""
    calls, clock = [], Clock()
    cohort = api().provision_cohort(*databases, producer="U",
                                    policy={**POLICY, "judge_p_admit": 1e-9},
                                    prompt_version="desk-judge-v6", created_at=AT)
    c = make_candidate(0, T0 + timedelta(minutes=1))
    clock.t = T0 + timedelta(minutes=1, seconds=1)
    t = api().ingest_candidate(databases[0], c, cohort_id=cohort, now_iso=clock.t.isoformat())
    api().evaluate_candidate(databases[0], t["ticket_id"], evidence=evidence(c), policy=policy(),
                             now_iso=clock.t.isoformat())
    store.record_admission(databases[0], ticket_id=t["ticket_id"], arm="F", decision="not_selected",
                           decided_at=clock.t.isoformat())
    clock.t += timedelta(seconds=2)
    out = J.run_once(databases[0], lambda: nullcontext(databases[1]), call_model=model_returning(enter, calls=calls),
                     system_prompt="SYS", worker="w", now_fn=clock, calls_per_hour=100)
    assert [o["reason"] for o in out] == ["judge_budget_skipped"] and calls == []


class FakeGate:
    def __init__(self, state):
        self.state = state

    def decided(self, since_epoch):
        return {i for i, v in self.state.items() if v in ("shortlisted", "cut")}

    def states(self, ids):
        return {i: self.state.get(i, "pending") for i in ids}


def test_the_judge_sees_only_the_runners_shortlist(databases):
    """Cohort U (ranker pre-registration §1-§2): shortlisted is judged; cut is a no-call ranker_cut
    SKIP with F not_selected that spends no budget; pending stays unclaimed and is judged once it
    makes the shortlist."""
    calls, clock = [], Clock()
    cohort = api().provision_cohort(*databases, producer="U", policy={**POLICY, "judge_p_admit": 1.0, "select_version": J.SELECT_VERSION},
                                    prompt_version="desk-judge-v6", created_at=AT)
    state = {"desk:U:t000": "pending", "desk:U:t001": "cut", "desk:U:t002": "shortlisted"}
    run = dict(call_model=model_returning(enter, calls=calls), calls_per_hour=1, shortlist_fn=FakeGate(state))
    out = []
    for k in range(3):
        out += feed(databases, cohort, clock, k, T0 + timedelta(minutes=k + 1), **run)
    reasons = {o["ticket_id"]: o["reason"] for o in out}
    assert reasons["desk:U:t001"] == "judge_ranker_cut" and "desk:U:t000" not in reasons
    assert [c["opportunity_id"] for c in calls] == ["desk:U:t002"]        # the cut made no call
    adm = databases[0].execute("SELECT decision FROM desk_arm_admissions WHERE ticket_id='desk:U:t001' "
                               "AND arm='F'").fetchone()
    assert adm[0] == "not_selected"
    assert store.judge_calls_since(databases[0], since_iso=AT) == 1         # ranker_cut is not budget
    state["desk:U:t000"] = "shortlisted"
    clock.t += timedelta(hours=2)
    later = J.run_once(databases[0], lambda: nullcontext(databases[1]), now_fn=clock, system_prompt="SYS",
                       worker="w", **run)
    assert [o["ticket_id"] for o in later] == ["desk:U:t000"] and len(calls) == 2


def test_the_ranker_gate_reads_the_ranker_candidates(tmp_path):
    import sqlite3
    gate = J.ranker_gate(tmp_path)
    assert gate.states(["desk:U:x"]) == {"desk:U:x": "pending"}            # no ranker yet: wait
    assert gate.decided(0.0) == set()
    (tmp_path / "ranker").mkdir()
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "ranker_worker", Path(__file__).resolve().parents[1] / "scripts" / "memecoin_desk_ranker_worker.py")
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    conn = worker.rw(tmp_path / "ranker" / "candidates.db", worker.DDL_CAND)     # the ranker's own schema
    conn.executemany("INSERT INTO candidates(opportunity_id, first_shortlisted_at, closed_at, ranker_version, "
                     "first_rank_at) VALUES(?,?,?,'u-runners-v1',60.0)",
                     [("a", 60.0, None), ("b", None, 120.0), ("c", None, None), ("d", 60.0, 120.0)])
    conn.commit()
    conn.close()
    got = gate.states(list("abcde"))
    assert [got[i] for i in "abcde"] == ["shortlisted", "cut", "pending", "shortlisted", "pending"]
    assert gate.decided(0.0) == {"a", "b", "d"} and gate.decided(61.0) == set()


def test_a_ticket_the_ranker_never_covered_is_one_ranker_missing_skip(databases, tmp_path):
    """Ranker absent: a U PASS waits unclaimed until it is older than MAX_LIFE + two cycles, then it
    is exactly one no-call SKIP ranker_missing with F not_selected, outside the budget (Gamma)."""
    calls, clock = [], Clock()
    cohort = api().provision_cohort(*databases, producer="U", policy={**POLICY, "judge_p_admit": 1.0, "select_version": J.SELECT_VERSION},
                                    prompt_version="desk-judge-v6", created_at=AT)
    run = dict(call_model=model_returning(enter, calls=calls), calls_per_hour=1,
               shortlist_fn=J.ranker_gate(tmp_path))                    # no ranker/candidates.db
    assert feed(databases, cohort, clock, 0, T0 + timedelta(minutes=1), **run) == []
    clock.t += timedelta(seconds=J.missing_after_s() - 5)
    again = lambda: J.run_once(databases[0], lambda: nullcontext(databases[1]), now_fn=clock,
                               system_prompt="SYS", worker="w", **run)
    assert again() == []                                                 # still inside the ranker's life
    clock.t += timedelta(seconds=10)
    out = again()
    assert [(o["ticket_id"], o["reason"]) for o in out] == [("desk:U:t000", "judge_ranker_missing")]
    saved = databases[0].execute("SELECT result_json FROM desk_judge_calls WHERE ticket_id='desk:U:t000'").fetchone()
    assert json.loads(saved[0])["select_version"] == "desk-judge-select-v1"
    assert again() == [] and calls == []
    assert store.judge_calls_since(databases[0], since_iso=AT) == 0
    adm = databases[0].execute("SELECT decision FROM desk_arm_admissions WHERE ticket_id='desk:U:t000' "
                               "AND arm='F'").fetchone()
    assert adm[0] == "not_selected"


def test_pending_tickets_never_hide_a_shortlisted_one(databases):
    """Codex 5bb94b169: a pass bounded at 5 used to page the oldest waiting tickets; 6 pending ones hid
    a shortlisted 7th forever. The shortlisted one is selected from the ranker's decided set."""
    calls, clock = [], Clock()
    cohort = api().provision_cohort(*databases, producer="U", policy={**POLICY, "judge_p_admit": 1.0, "select_version": J.SELECT_VERSION},
                                    prompt_version="desk-judge-v6", created_at=AT)
    state = {"desk:U:t%03d" % k: "pending" for k in range(6)}
    state["desk:U:t006"] = "shortlisted"
    run = dict(call_model=model_returning(enter, calls=calls), calls_per_hour=100, shortlist_fn=FakeGate(state))
    out = []
    for k in range(7):
        out += feed(databases, cohort, clock, k, T0 + timedelta(minutes=k + 1), **run)
    assert [o["ticket_id"] for o in out] == ["desk:U:t006"] and len(calls) == 1


# ---------------------------------------------------------------- Codex's witnesses on 5bb94b169

def _seed_passed_tickets(conn, n, at0):
    import hashlib
    from sniper.services import memecoin_desk_contract as contract
    from sniper.services import memecoin_desk_universe as U
    store.register_cohort(conn, cohort_id="u-test", producer="U", prompt_version="desk-judge-v6",
                          policy={"judge_p_admit": 1.0, "rail_bounds": {}, "select_version": J.SELECT_VERSION},
                          created_at=at0.isoformat())
    ids = []
    for i in range(n):
        at = at0 + timedelta(milliseconds=i)
        mint = U.b58encode(hashlib.sha256(str(i).encode()).digest())
        signature = U.b58encode(hashlib.sha512(str(i).encode()).digest())
        oid = U.ticket_id("solana", mint, i + 1, signature, 0)
        snapshot = U.build_snapshot(emission_at=at.timestamp(), fields={
            "mcap_usd": U.Field(41000.0, "pumpfun_grpc:curve_reserves", at.timestamp(), i + 1, "complete"),
            "liq_usd": U.Field(5000.0, "pumpfun_grpc:real_quote_reserves", at.timestamp(), i + 1, "complete")})
        candidate = contract.validate_candidate({
            "schema": "desk-candidate/v1", "ticket_id": oid, "producer": "U", "network_id": "solana",
            "mint": mint, "pool": U.bonding_curve(mint), "band_entry_slot": i + 1,
            "trigger_signature": signature, "trigger_ordinal": 0, "emitted_at": at.isoformat(),
            "gate_state": None, "snapshot": snapshot})
        store.record_candidate(conn, candidate, cohort_id="u-test", now_iso=at.isoformat(), cooldown_s=0)
        store.record_stage(conn, ticket_id=oid, stage="hard_filter", seat="code", verdict="PASS",
                           reason="fixture_pass", inputs={"i": i}, ts=at.isoformat())
        ids.append(oid)
    return ids


def _ranker_db(tmp_path, rows):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "ranker_worker", Path(__file__).resolve().parents[1] / "scripts" / "memecoin_desk_ranker_worker.py")
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    conn = worker.rw(tmp_path / "ranker" / "candidates.db", worker.DDL_CAND)
    conn.executemany("INSERT INTO candidates(opportunity_id, first_rank_at, first_shortlisted_at, closed_at, "
                     "ranker_version) VALUES(?,?,?,?,'u-runners-v1')", rows)
    conn.commit()
    conn.close()


def test_501_pending_tickets_never_hide_a_later_shortlisted_one(tmp_path):
    at0 = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
    conn = store.connect(tmp_path / "desk.db")
    ids = _seed_passed_tickets(conn, 501, at0)
    target = ids[-1]
    _ranker_db(tmp_path, [(target, at0.timestamp() + 60, at0.timestamp() + 60, None)])
    calls = []

    def fake_model(_system, user, **_kwargs):
        calls.append(json.loads(user)["opportunity_id"])
        return {"text": "fixture_only", "session_id": "s", "result_id": "r"}

    for _ in range(3):
        J.run_once(conn, lambda: nullcontext(None), call_model=fake_model, system_prompt="fixture",
                   worker="fixture", now_fn=lambda: at0 + timedelta(minutes=2), calls_per_hour=100,
                   record_fn=lambda *_a, **_k: {"action": "SKIP", "reason": "fixture_only"},
                   shortlist_fn=J.ranker_gate(tmp_path))
    conn.close()
    assert calls == [target]                                   # once: claimed on the first pass


def test_an_unopenable_ranker_db_reads_pending_never_a_crash(tmp_path):
    (tmp_path / "ranker" / "candidates.db").mkdir(parents=True)  # the path exists; SQLite cannot open it
    gate = J.ranker_gate(tmp_path)
    assert gate.states(["desk:U:unreadable"]) == {"desk:U:unreadable": "pending"}
    assert gate.decided(0.0) == set()


def test_a_u_cohort_frozen_under_another_selection_rule_is_never_judged(databases):
    """Gamma/Alpha: a cohort whose policy names no select_version, or another one, refuses before any
    claim or call; the same tickets ungated (no ranker) are unchanged."""
    for version in (None, "desk-judge-select-v0"):
        calls, clock = [], Clock()
        policy = {**POLICY, "judge_p_admit": 1.0, **({"select_version": version} if version else {})}
        cohort = api().provision_cohort(*databases, producer="U", policy=policy, prompt_version="desk-judge-v6",
                                        created_at=AT)
        with pytest.raises(ValueError, match="select_version"):
            feed(databases, cohort, clock, 0 if version is None else 1, T0 + timedelta(minutes=1),
                 call_model=model_returning(enter, calls=calls), calls_per_hour=100,
                 shortlist_fn=FakeGate({"desk:U:t000": "shortlisted", "desk:U:t001": "shortlisted"}))
        assert calls == []
    assert databases[0].execute("SELECT COUNT(*) FROM desk_judge_calls").fetchone()[0] == 0


def test_a_called_ticket_leaves_a_prequote_hint_before_the_model_runs(databases, monkeypatch):
    ticket = ready_ttl(databases)
    calls, clock, seen = [], Clock(), []
    inner = model_returning(enter, calls=calls, clock=clock)

    def model(system, user, **kw):        # the hint must exist WHILE the model thinks
        seen.extend(store.live_prequote_hints(databases[0], now=clock().timestamp(), limit=5))
        return inner(system, user, **kw)
    J.run_once(databases[0], lambda: nullcontext(databases[1]), call_model=model, system_prompt="SYS",
               worker="w1", now_fn=clock, calls_per_hour=100)
    t = store.get_ticket(databases[0], ticket)
    assert [(h[0], h[1], h[2]) for h in seen] == [(ticket, t["network_id"], t["mint"])]
    assert store.live_prequote_hints(databases[0], now=clock().timestamp(), limit=5) == []   # cleared on return


def test_the_exact_sheet_the_model_saw_is_recorded_with_its_hash(databases):
    import base64
    import hashlib
    import zlib
    ticket = ready_ttl(databases)
    seen, clock = [], Clock()

    def model(system, user, **kw):
        seen.append(user)
        return model_returning(enter, calls=[], clock=clock)(system, user, **kw)
    J.run_once(databases[0], lambda: nullcontext(databases[1]), call_model=model, system_prompt="SYS", worker="w1",
               now_fn=clock, calls_per_hour=100)
    row = databases[0].execute("SELECT result_json FROM desk_judge_calls WHERE ticket_id=?", (ticket,)).fetchone()
    result = json.loads(row[0])
    text = zlib.decompress(base64.b64decode(result["sheet_zb64"])).decode()
    assert text == seen[0] and result["sheet_sha256"] == hashlib.sha256(seen[0].encode()).hexdigest()


@pytest.mark.parametrize("basis,worker,flat", [("subscription_flat", "judge-1158101", True),
                                               ("subscription_flat", "dead", False),
                                               ("unknown", "judge-1158101", False)])
def test_a_lost_call_on_the_flat_subscription_seat_is_priced_flat(databases, basis, worker, flat):
    """Operator 2026-10-23 (cost gate held on desk-judge:desk:U:a732ab0639efca0a7d8b92a4 after a deploy cut a call):
    a lost call is $0 with flat provenance ONLY when the cohort froze subscription_flat and the claimer was the JUDGE
    worker; anything else stays desk:unpriced and the gate holds."""
    from sniper.persistence import db_memecoin as ledger
    cohort = api().provision_cohort(*databases, producer="U", policy={**POLICY, "judge_p_admit": 1.0,
                                    "select_version": J.SELECT_VERSION, "model_billing_basis": basis},
                                    prompt_version="desk-judge-v6", created_at=AT)
    ticket = api().ingest_candidate(databases[0], candidate(), cohort_id=cohort, now_iso=AT)["ticket_id"]
    api().evaluate_candidate(databases[0], ticket, evidence=evidence(), policy=policy(), now_iso=AT)
    store.claim_judge_call(databases[0], ticket_id=ticket, call_id="c-x", worker=worker, claimed_at=AT)
    J.recover(databases[0], lambda: nullcontext(databases[1]), now_fn=Clock())
    row = ledger.get_desk_cohort_cost(databases[1], cohort, "desk-judge:" + ticket)
    if flat:
        assert row["usd"] == 0 and row["provenance"].startswith("model:claude_sdk:subscription_flat:")
        assert ledger._desk_zero_model(row["provenance"])
    else:
        assert row["usd"] is None and row["provenance"] == "desk:unpriced"


def test_a_lost_call_closes_its_claim_and_an_old_orphan_claim_is_closed(databases, monkeypatch):
    ticket = ready_ttl(databases)
    store.claim_judge_call(databases[0], ticket_id=ticket, call_id="c-x", worker="dead", claimed_at=AT)
    real = J._close_lost_claim
    monkeypatch.setattr(J, "_close_lost_claim", lambda *a: None)       # the old release: vet recorded, claim left open
    J.recover(databases[0], lambda: nullcontext(databases[1]), now_fn=Clock())
    assert databases[0].execute("SELECT state FROM desk_judge_calls").fetchone()[0] == "claimed"
    monkeypatch.setattr(J, "_close_lost_claim", real)
    assert J.close_orphan_claims(databases[0]) == ["c-x"]
    row = databases[0].execute("SELECT state, json_extract(result_json,'$.call_error') FROM desk_judge_calls").fetchone()
    assert tuple(row) == ("responded", "lost") and J.close_orphan_claims(databases[0]) == []


def test_recover_closes_the_lost_claim_in_the_same_pass(databases):
    ticket = ready_ttl(databases)
    store.claim_judge_call(databases[0], ticket_id=ticket, call_id="c-y", worker="dead", claimed_at=AT)
    J.recover(databases[0], lambda: nullcontext(databases[1]), now_fn=Clock())
    assert databases[0].execute("SELECT state FROM desk_judge_calls").fetchone()[0] == "responded"


def test_every_call_stores_the_exact_request_bytes(databases):
    """Alpha 2026-10-23: replay provenance. The sheet bytes ride on each call; the system prompt bytes are stored once."""
    import base64
    import hashlib
    import zlib
    ready_ttl(databases)
    calls, clock = [], Clock()
    J.run_once(databases[0], lambda: nullcontext(databases[1]), call_model=model_returning(enter, calls=calls, clock=clock),
               system_prompt="sys v23 bytes", worker="judge-1", now_fn=clock, calls_per_hour=40)
    res = json.loads(databases[0].execute("SELECT result_json FROM desk_judge_calls").fetchone()[0])
    assert res["system_sha256"] == hashlib.sha256(b"sys v23 bytes").hexdigest()
    sheet = zlib.decompress(base64.b64decode(res["sheet_zb64"])).decode()
    assert json.loads(sheet) == calls[0] and hashlib.sha256(sheet.encode()).hexdigest() == res["sheet_sha256"]
    rows = databases[0].execute("SELECT sha256, prompt_version, text FROM desk_judge_prompts").fetchall()
    assert [tuple(r) for r in rows] == [(res["system_sha256"], "desk-judge-v6", "sys v23 bytes")]


def test_sigterm_drain_claims_nothing_new(databases):
    """Operator 2026-10-23: on SIGTERM the worker finishes the call in flight and claims no further ticket."""
    ticket = ready_ttl(databases)
    calls, clock = [], Clock()
    out = J.run_once(databases[0], lambda: nullcontext(databases[1]),
                     call_model=model_returning(lambda s: "", calls=calls, clock=clock), system_prompt="sys",
                     worker="judge-1", now_fn=clock, calls_per_hour=40, stop_fn=lambda: True)
    assert out == [] and calls == []
    assert databases[0].execute("SELECT COUNT(*) FROM desk_judge_calls").fetchone()[0] == 0


def test_sigterm_mid_call_records_the_call_and_claims_no_next_ticket(databases):
    """Codex eCat RC on 91218c703: SIGTERM lands DURING the first call. That call's response is saved and recorded;
    the second waiting ticket is never claimed."""
    first = ready_ttl(databases)
    other = candidate()
    other["ticket_id"], other["mint"] = "desk:U:fixture2", other["mint"][:-1] + ("2" if other["mint"][-1] != "2" else "3")
    cohort = databases[0].execute("SELECT cohort_id FROM desk_ticket_heads").fetchone()[0]
    second = api().ingest_candidate(databases[0], other, cohort_id=cohort, now_iso=AT)["ticket_id"]
    api().evaluate_candidate(databases[0], second, evidence=evidence(other), policy=policy(), now_iso=AT)
    assert len(store.list_awaiting_judge(databases[0], limit=10)) == 2
    stopping, calls, clock = {"flag": False}, [], Clock()
    inner = model_returning(enter, calls=calls, clock=clock)

    def call(system, user, **kw):
        stopping["flag"] = True                     # SIGTERM arrives while the provider call is in flight
        return inner(system, user, **kw)
    out = J.run_once(databases[0], lambda: nullcontext(databases[1]), call_model=call, system_prompt="sys",
                     worker="judge-1", now_fn=clock, calls_per_hour=40, stop_fn=lambda: stopping["flag"])
    assert len(calls) == 1 and [r["ticket_id"] for r in out] == [calls[0]["opportunity_id"]]
    rows = databases[0].execute("SELECT ticket_id, state FROM desk_judge_calls").fetchall()
    assert len(rows) == 1 and rows[0][1] != "claimed"
    assert {first, second} - {rows[0][0]} and len({first, second} - {rows[0][0]}) == 1


def test_watchdog_drain_grace_covers_the_largest_valid_deadline():
    """Codex eCat RC on 91218c703: a 15 s grace cut a valid call under MEMECOIN_DESK_JUDGE_DEADLINE_S=20 (allowed up to 60)."""
    import re
    from pathlib import Path
    from sniper.services import memecoin_desk_contract as contract
    src = (Path(__file__).resolve().parents[1] / "scripts" / "memecoin_desk_judge_watchdog.sh").read_text()
    grace = int(re.search(r"^DESK_DRAIN_GRACE_S=(\d+)$", src, re.M).group(1))
    assert "$((DESK_DRAIN_GRACE_S * 4))" in src            # the wait loop really uses it (0.25 s steps)
    assert grace >= contract.JUDGE_DEADLINE_MAX_S + 10


@pytest.mark.parametrize("echo,action", [("claude-opus-5-5", "ENTER"), ("claude-haiku-5-5", "SKIP")])
def test_an_opus_cohort_accepts_only_an_opus_answer(databases, echo, action):
    cohort = api().provision_cohort(*databases, producer="U", policy={**POLICY, "judge_p_admit": 1.0, "select_version": J.SELECT_VERSION,
                                    "judge_model": "claude-opus-5-5"}, prompt_version="desk-judge-v6", created_at=AT)
    ticket = api().ingest_candidate(databases[0], candidate(), cohort_id=cohort, now_iso=AT)
    api().evaluate_candidate(databases[0], ticket["ticket_id"], evidence=evidence(), policy=policy(), now_iso=AT)
    calls, clock = [], Clock()
    out = J.run_once(databases[0], lambda: nullcontext(databases[1]),
                     call_model=model_returning(enter, calls=calls, clock=clock, echo=echo), system_prompt="sys",
                     worker="judge-1", now_fn=clock, calls_per_hour=40)
    assert out[0]["action"] == action


@pytest.mark.parametrize("basis,worker,flat", [("subscription_flat", "judge-2930128", True),
                                               ("subscription_flat", "dead", False),
                                               ("unknown", "judge-2930128", False)])
def test_a_timed_out_call_on_the_flat_subscription_seat_is_priced_flat(databases, basis, worker, flat):
    """Operator 2026-10-26 (DESK-COST-GATE page, desk-judge:desk:U:a49c64c07f816ee5aaed0d06: Opus hit the 12 s deadline,
    no SDK receipt): a timed-out call is $0 with flat provenance ONLY on the flat seat claimed by the JUDGE worker;
    anything else keeps the unknown-cost marker and the gate holds."""
    from sniper.persistence import db_memecoin as ledger
    cohort = api().provision_cohort(*databases, producer="U", policy={**POLICY, "judge_p_admit": 1.0,
                                    "select_version": J.SELECT_VERSION, "model_billing_basis": basis},
                                    prompt_version="desk-judge-v6", created_at=AT)
    ticket = api().ingest_candidate(databases[0], candidate(), cohort_id=cohort, now_iso=AT)["ticket_id"]
    api().evaluate_candidate(databases[0], ticket, evidence=evidence(), policy=policy(), now_iso=AT)
    store.claim_judge_call(databases[0], ticket_id=ticket, call_id="c-t", worker=worker, claimed_at=AT)
    result = {"call_error": "timeout", "exception": "TimeoutError", "result_id": None, "session_id": None,
              "model_echo": None, "billing_basis": basis, "deadline_s": 12.0}
    call = {"ticket_id": ticket, "call_id": "c-t", "worker": worker, "claimed_at": AT, "result": result,
            "call_started_at": AT, "response_at": AT, "elapsed_ms": 12007.6}
    if not flat:              # unchanged path: no receipt identity, the proof refuses (the caller records model-invalid)
        with pytest.raises(ValueError, match="model_cost_receipt_invalid"):
            J._record(databases[0], lambda: nullcontext(databases[1]), call, Clock())
        return
    J._record(databases[0], lambda: nullcontext(databases[1]), call, Clock())
    row = ledger.get_desk_cohort_cost(databases[1], cohort, "desk-judge:" + ticket)
    assert row["usd"] == 0 and ledger._desk_zero_model(row["provenance"])
