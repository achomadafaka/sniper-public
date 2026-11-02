"""Amendment 10: a ticket that gets NO model call still keeps its decision-moment context (as-of snapshot, chart, cluster/holder
facts) so a JUDGE-independent reference outcome can be computed from the tape later."""
from contextlib import nullcontext

import pytest

import test_desk_structure as TS
from sniper.persistence import db_memecoin_desk as store
from sniper.services import memecoin_desk_asof as A
from sniper.services import memecoin_desk_contract as contract
from sniper.services import memecoin_desk_judge as J
from sniper.services import memecoin_desk_structure as S
from test_memecoin_desk_asof_flow import refreshed
from test_memecoin_desk_judge import Clock, FakeGate, databases, enter, model_returning, ready_ttl  # noqa: F401

STRUCT = S.structure(TS.BASE, as_of=TS.T, cut_slot=TS.CUT, bounds=TS.BOUNDS)
SOFT = contract.soft_facts({})


def run(databases, calls, **kw):
    clock = kw.pop("clock", None) or Clock()
    args = dict(call_model=model_returning(enter, calls=calls, clock=clock), system_prompt="SYS", worker="w", now_fn=clock,
                calls_per_hour=100, asof_fn=lambda c, ts: (refreshed(c, ts), {"v": "t"}),
                soft_fn=lambda t: SOFT, structure_fn=lambda t, c, ts: STRUCT)
    args.update(kw)
    return J.run_once(databases[0], lambda: nullcontext(databases[1]), **args)


def vet(databases, ticket):
    return [s for s in store.get_ticket(databases[0], ticket)["stages"] if s["stage"] == "vet"][0]


def assert_context(v):
    assert v["inputs"]["asof"]["candidate"]["snapshot"]["hash"] and v["inputs"]["asof"]["meta"] == {"v": "t"}
    assert v["inputs"]["sheet"]["structure"] == STRUCT and v["inputs"]["sheet"]["evidence"] == SOFT


def test_budget_skipped_ticket_keeps_asof_structure_and_soft(databases):
    ticket = ready_ttl(databases)
    calls = []
    run(databases, calls, admit_fn=lambda t: False)
    v = vet(databases, ticket)
    assert calls == [] and v["verdict"] == "SKIP" and v["reason"] == "judge_budget_skipped"
    assert_context(v)


def test_budget_exhausted_ticket_keeps_the_context(databases):
    ticket = ready_ttl(databases)
    calls = []
    run(databases, calls, calls_per_hour=0)
    v = vet(databases, ticket)
    assert calls == [] and v["reason"] == "judge_budget_exhausted"
    assert_context(v)


def test_ranker_cut_ticket_keeps_the_context(databases):
    ticket = ready_ttl(databases)
    calls = []
    opportunity = store.get_ticket(databases[0], ticket)["opportunity_id"]
    cohort = store.get_cohort(databases[0], store.get_ticket(databases[0], ticket)["cohort_id"])
    if cohort["policy"].get("select_version") != J.SELECT_VERSION:
        pytest.skip("fixture cohort has no select_version; ranker_cut path is covered by the budget paths")
    run(databases, calls, shortlist_fn=FakeGate({opportunity: "cut"}))
    v = vet(databases, ticket)
    assert calls == [] and v["reason"] == "judge_ranker_cut"
    assert_context(v)


def test_stale_snapshot_ticket_keeps_the_chart_and_facts_but_no_snapshot(databases):
    ticket = ready_ttl(databases)
    calls = []

    def stale(c, ts):
        raise A.StaleSnapshot("tape_stale")
    run(databases, calls, asof_fn=stale)
    v = vet(databases, ticket)
    assert calls == [] and v["verdict"] == "SKIP"
    saved = databases[0].execute("SELECT result_json FROM desk_judge_calls").fetchone()[0]
    assert "stale_snapshot" in saved
    assert "asof" not in v["inputs"] and v["inputs"]["sheet"]["structure"] == STRUCT      # no fake snapshot, the chart/facts are kept


def test_a_failing_context_component_never_breaks_the_skip(databases):
    ticket = ready_ttl(databases)
    calls = []

    def boom(t):
        raise RuntimeError("cluster db locked")
    run(databases, calls, admit_fn=lambda t: False, soft_fn=boom)
    v = vet(databases, ticket)
    assert v["verdict"] == "SKIP" and v["reason"] == "judge_budget_skipped"        # still recorded; the failed part is simply absent


def test_the_model_called_path_is_unchanged(databases):
    ticket = ready_ttl(databases)
    calls = []
    run(databases, calls)
    assert len(calls) == 1 and vet(databases, ticket)["inputs"]["sheet"]["structure"] == STRUCT


def test_no_verdict_price_preflight_keeps_the_chart_and_facts(databases):
    from test_memecoin_desk_contract import rehash
    ticket = ready_ttl(databases)
    calls = []

    def unpriceable(c, ts):
        out = refreshed(c, ts)
        row = out["snapshot"]["fields"]["liq_usd"]
        row["value"], row["completeness"] = None, "missing"
        return rehash(out), {"v": "t"}
    run(databases, calls, asof_fn=unpriceable)
    head = store.get_ticket(databases[0], ticket)
    pre = [st for st in head["stages"] if st["stage"] == "vet"][0]
    assert calls == [] and pre["inputs"]["preflight_reason"] == "no_verdict_price"
    assert pre["inputs"]["structure"] == STRUCT and pre["inputs"]["soft"] == SOFT


def test_a_failed_context_read_is_marked_not_silently_absent(databases):
    ticket = ready_ttl(databases)

    def boom(t):
        raise RuntimeError("cluster db locked")
    run(databases, [], admit_fn=lambda t: False, soft_fn=boom)
    assert vet(databases, ticket)["inputs"]["context_error"] == "RuntimeError"


def test_a_clean_context_read_carries_no_error_marker(databases):
    ticket = ready_ttl(databases)
    run(databases, [], admit_fn=lambda t: False)
    assert "context_error" not in vet(databases, ticket)["inputs"]
