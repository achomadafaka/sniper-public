"""JUDGE flow with the as-of-decision refresh: one snapshot feeds the sheet, the call and the vet inputs; a stale
tape is a no-call SKIP; nothing falls back to emission facts."""
from __future__ import annotations

import copy
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sniper.persistence import db_memecoin_desk as store
from sniper.services import memecoin_desk_asof as A
from sniper.services import memecoin_desk_contract as contract
from sniper.services import memecoin_desk_judge as J
from test_memecoin_desk_contract import rehash
from test_memecoin_desk_judge import Clock, databases, enter, model_returning, ready_ttl  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
FRESH_MCAP = 52_000.0


def refreshed(candidate, ts, mcap=FRESH_MCAP):
    c = copy.deepcopy(candidate)
    at = datetime.fromtimestamp(ts, timezone.utc)
    c["emitted_at"] = at.isoformat()
    c["snapshot"]["emission_at"] = at.timestamp()
    row = c["snapshot"]["fields"]["mcap_usd"]
    row["value"], row["known_at"] = mcap, at.timestamp()
    return rehash(c)


def run(databases, ticket_model, asof_fn, clock):
    return J.run_once(databases[0], lambda: nullcontext(databases[1]), call_model=ticket_model, system_prompt="SYS",
                      worker="w1", now_fn=clock, calls_per_hour=100, asof_fn=asof_fn)


def vet(databases, ticket):
    return [s for s in store.get_ticket(databases[0], ticket)["stages"] if s["stage"] == "vet"][0]


def test_the_model_sees_the_refreshed_sheet_and_the_vet_inputs_keep_the_same_snapshot(databases):
    ticket = ready_ttl(databases)
    calls, clock = [], Clock()
    run(databases, model_returning(enter, calls=calls, clock=clock), lambda c, ts: (refreshed(c, ts), {"v": "t"}), clock)
    assert calls[0]["mcap_usd"] == FRESH_MCAP                              # the model's sheet
    v = vet(databases, ticket)
    assert v["inputs"]["sheet"]["mcap_usd"] == FRESH_MCAP                  # the recorded sheet: the same snapshot
    asof = v["inputs"]["asof"]
    seen = asof["candidate"]["snapshot"]["fields"]["mcap_usd"]["value"]
    assert seen == FRESH_MCAP == calls[0]["mcap_usd"] == v["inputs"]["sheet"]["mcap_usd"]     # sheet price == ref
    head = store.get_ticket(databases[0], ticket)
    assert asof["emission_snapshot_hash"] == head["snapshot_hash"] != asof["candidate"]["snapshot"]["hash"]
    assert v["inputs"]["snapshot_hash"] == head["snapshot_hash"]           # the ticket identity is unchanged
    assert v["verdict"] == "ENTER"


def test_a_stale_snapshot_is_a_no_call_skip_and_never_emission_facts(databases):
    ticket = ready_ttl(databases)
    calls, clock = [], Clock()

    def stale(c, ts):
        raise A.StaleSnapshot("tape_stale")
    run(databases, model_returning(enter, calls=calls, clock=clock), stale, clock)
    assert calls == []                                                     # no model call, no cost
    v = vet(databases, ticket)
    assert v["verdict"] == "SKIP" and "asof" not in v["inputs"]
    calls_row = store.list_judge_calls_to_record(databases[0], limit=10) or []
    saved = databases[0].execute("SELECT result_json FROM desk_judge_calls").fetchone()[0]
    assert "stale_snapshot" in saved and "tape_stale" in saved
    assert store.get_admission(databases[0], ticket_id=ticket, arm="F")["decision"] == "not_selected"


def test_without_an_asof_function_the_emission_sheet_is_used_as_before(databases):
    ticket = ready_ttl(databases)
    calls, clock = [], Clock()
    run(databases, model_returning(enter, calls=calls, clock=clock), None, clock)
    assert calls[0]["mcap_usd"] != FRESH_MCAP and "asof" not in vet(databases, ticket)["inputs"]


def test_a_refreshed_candidate_for_another_coin_is_refused_when_recorded(databases):
    ticket = ready_ttl(databases)
    clock = Clock()

    def other_coin(c, ts):
        r = refreshed(c, ts)
        r["band_entry_slot"] = c["band_entry_slot"] + 1                       # a valid but different identity
        return r, {}
    with pytest.raises(ValueError, match="asof_candidate"):
        run(databases, model_returning(enter, calls=[], clock=clock), other_coin, clock)


def test_a_refresh_dated_after_the_call_is_refused(databases):
    ready_ttl(databases)
    clock = Clock()
    with pytest.raises(ValueError, match="asof_candidate"):
        run(databases, model_returning(enter, calls=[], clock=clock), lambda c, ts: (refreshed(c, ts + 3600), {}), clock)


def test_the_worker_wires_the_refresh_on_by_default_with_a_kill_knob():
    text = (ROOT / "scripts" / "memecoin_desk_judge_worker.py").read_text()
    assert "asof_fn=asof_fn" in text and 'MEMECOIN_DESK_JUDGE_ASOF", "1"' in text and "asof_mod.refresh" in text
