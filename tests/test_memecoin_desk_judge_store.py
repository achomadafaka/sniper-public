"""Desk JUDGE invocation claims: list, claim once, save the result, recover without re-asking."""
from __future__ import annotations

import pytest

from sniper.persistence import db_memecoin_desk as store
from test_memecoin_desk_pipeline import databases, ready, decide, RESPONSE, AT  # noqa: F401

LATER = "2026-10-21T12:00:05+00:00"


def test_awaiting_lists_hard_filter_pass_without_vet_or_claim(databases):
    _, ticket = ready(databases)
    rows = store.list_awaiting_judge(databases[0], limit=10)
    assert [r["ticket_id"] for r in rows] == [ticket]
    assert rows[0]["candidate"]["ticket_id"] and rows[0]["cohort_id"]


def test_claim_is_once_and_hides_the_ticket_from_the_listing(databases):
    _, ticket = ready(databases)
    assert store.claim_judge_call(databases[0], ticket_id=ticket, call_id="c1", worker="w1", claimed_at=AT) is True
    assert store.claim_judge_call(databases[0], ticket_id=ticket, call_id="c2", worker="w2", claimed_at=AT) is False
    assert store.list_awaiting_judge(databases[0], limit=10) == []


def test_saved_response_is_immutable_and_listed_for_recording(databases):
    _, ticket = ready(databases)
    store.claim_judge_call(databases[0], ticket_id=ticket, call_id="c1", worker="w1", claimed_at=AT)
    result = {"text": "{}", "is_error": False, "session_id": "s", "result_id": "r", "usage": {}, "call_error": None}
    store.save_judge_response(databases[0], call_id="c1", call_started_at=AT, response_at=RESPONSE,
                              elapsed_ms=1000.0, result=result)
    with pytest.raises(ValueError, match="judge_call_state"):
        store.save_judge_response(databases[0], call_id="c1", call_started_at=AT, response_at=RESPONSE,
                                  elapsed_ms=1000.0, result=result)
    pending = store.list_judge_calls_to_record(databases[0], limit=10)
    assert [(p["ticket_id"], p["state"], p["result"]["result_id"]) for p in pending] == [(ticket, "responded", "r")]


def test_recorded_vet_closes_the_call(databases):
    _, ticket = ready(databases)
    store.claim_judge_call(databases[0], ticket_id=ticket, call_id="c1", worker="w1", claimed_at=AT)
    store.save_judge_response(databases[0], call_id="c1", call_started_at=AT, response_at=RESPONSE,
                              elapsed_ms=1000.0, result={"text": "x", "call_error": None})
    decide(databases, ticket)
    assert store.list_judge_calls_to_record(databases[0], limit=10) == []


def test_claim_without_response_is_listed_as_lost_never_reclaimable(databases):
    _, ticket = ready(databases)
    store.claim_judge_call(databases[0], ticket_id=ticket, call_id="c1", worker="w1", claimed_at=AT)
    pending = store.list_judge_calls_to_record(databases[0], limit=10)
    assert [(p["ticket_id"], p["state"]) for p in pending] == [(ticket, "claimed")]
    assert store.claim_judge_call(databases[0], ticket_id=ticket, call_id="c9", worker="w1", claimed_at=LATER) is False


def test_calls_since_counts_claims_for_the_budget(databases):
    _, ticket = ready(databases)
    store.claim_judge_call(databases[0], ticket_id=ticket, call_id="c1", worker="w1", claimed_at=AT)
    assert store.judge_calls_since(databases[0], since_iso="2026-10-21T11:00:00+00:00") == 1
    assert store.judge_calls_since(databases[0], since_iso=LATER) == 0


def test_awaiting_newest_first_orders_by_hard_filter_time_on_a_real_store(databases):
    from test_memecoin_desk_contract import b58encode, candidate, evidence, policy
    from test_memecoin_desk_pipeline import api, provision
    cohort = provision(databases)
    ids = []
    for i, at in enumerate(("2026-10-21T12:00:00+00:00", "2026-10-21T12:00:02+00:00", "2026-10-21T12:00:01+00:00")):
        c = dict(candidate(), ticket_id="desk:U:fixture-%d" % i,                  # a distinct coin per ticket
                 mint=b58encode(bytes([i + 1]) * 32), pool=b58encode(bytes([i + 101]) * 32))
        t = api().ingest_candidate(databases[0], c, cohort_id=cohort, now_iso=at)
        api().evaluate_candidate(databases[0], t["ticket_id"], evidence=evidence(c), policy=policy(), now_iso=at)
        ids.append(t["ticket_id"])
    oldest = [r["ticket_id"] for r in store.list_awaiting_judge(databases[0], limit=10)]
    newest = [r["ticket_id"] for r in store.list_awaiting_judge(databases[0], limit=10, newest_first=True)]
    assert oldest == [ids[0], ids[2], ids[1]] and newest == [ids[1], ids[2], ids[0]]
    assert [r["ticket_id"] for r in store.list_awaiting_judge(databases[0], limit=1, newest_first=True)] == [ids[1]]
